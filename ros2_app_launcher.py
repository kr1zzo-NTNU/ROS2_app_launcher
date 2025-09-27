#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ROS 2 App Launcher (Tkinter)
- Scans a root folder for ROS 2 packages (by finding package.xml)
- Lists executables (via `ros2 pkg executables <pkg>`) and lets you pass ARGS
- Lists launch files (*.launch.{py,xml,yaml} AND *_launch.{py,xml,yaml}) from source AND installed package share dirs
- Lets you type custom launch specs and extra launch arguments
- Runs items in a new terminal with ROS auto-sourced (Jazzy by default)
- Handy buttons for RViz2, rqt_graph, and rqt

Fixes in this version:
* **Strict launch filter**: only matches files ending with `.launch.py|xml|yaml` **or** `_launch.py|xml|yaml`. No PNGs or other noise.
* **Installed launches work** even if this app wasn't started from a sourced shell. We synthesize an env by sourcing the setup files in a subshell and reading it back.
* **Subfolder launches** under `share/<pkg>/launch/**` run as `ros2 launch <pkg> sub/dir/file.launch.py`.
* **Args fields**: provide extra args for `ros2 run` and `ros2 launch`, plus a "Run Custom" launcher.
* **NEW**: Parses launch arguments (py/xml/yaml) and prompts for values before launching.
"""

import argparse
import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Optional YAML (best-effort parsing for .launch.yaml)
try:
    import yaml as _yaml  # pip install pyyaml
except Exception:
    _yaml = None

# ----------------------------- Config (edit to taste) ----------------------------
DEFAULT_ROS_SETUP = "/opt/ros/jazzy/setup.bash"   # change to your distro if needed
DEFAULT_WS_SETUP = ""                              # blank -> auto-detect upward install/setup.bash
TERMINALS = [
    ["gnome-terminal", "--", "bash", "-lc", "{CMD}; exec bash"],
    ["x-terminal-emulator", "-e", "bash", "-lc", "{CMD}; exec bash"],
    ["konsole", "-e", "bash", "-lc", "{CMD}; exec bash"],
    ["xterm", "-e", "bash", "-lc", "{CMD}; read -p 'Press enter to close...' && exit"],
    ["kitty", "bash", "-lc", "{CMD}; exec bash"],
    ["alacritty", "-e", "bash", "-lc", "{CMD}; exec bash"],
]
# ----------------------------------------------------------------------------------


@dataclass
class PkgInfo:
    name: str
    src_dir: Path


@dataclass
class LaunchItem:
    kind: str  # 'src' or 'inst'
    pkg: Optional[str]
    path: Path  # absolute file path
    rel_from_share_launch: Optional[Path] = None  # for installed items


# ----------------------------- Helper functions ----------------------------------

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def discover_packages(root: Path) -> Dict[str, PkgInfo]:
    """Find ROS 2 packages by locating package.xml files under root."""
    pkgs: Dict[str, PkgInfo] = {}
    for pkg_xml in root.rglob("package.xml"):
        try:
            tree = ET.parse(pkg_xml)
            name = tree.getroot().findtext("name")
            if name:
                pkgs[name] = PkgInfo(name=name, src_dir=pkg_xml.parent)
        except Exception:
            pass  # ignore malformed XML
    return dict(sorted(pkgs.items()))


def run_cmd_capture(args: List[str], env: Optional[dict] = None) -> Tuple[int, str]:
    try:
        out = subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, env=env)
        return 0, out
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output or ""


def list_executables(pkg: str, env: Optional[dict] = None) -> List[str]:
    """Use `ros2 pkg executables <pkg>` to list installed executables (filter out assets)."""
    code, out = run_cmd_capture(["ros2", "pkg", "executables", pkg], env=env)
    if code != 0:
        return []
    exes = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) >= 2 and parts[0] == pkg:
            exe = parts[1]
            if "." in exe:
                continue
            exes.append(exe)
    return sorted(set(exes))


# Strict matcher: *.launch.{py,xml,yaml} OR *_launch.{py,xml,yaml}
_LAUNCH_RE = re.compile(r"(?:^|/).*(?:\.launch|_launch)\.(?:py|xml|yaml)$")

def is_launch_file(p: Path) -> bool:
    if not p.is_file():
        return False
    if p.suffix.lower() == ".png":
        return False
    return bool(_LAUNCH_RE.search(str(p)))


def list_src_launch_files(src_dir: Path) -> List[Path]:
    """Find launch files ONLY in the package's ./launch folder (strict patterns)."""
    results: List[Path] = []
    ld = src_dir / "launch"
    if ld.is_dir():
        for p in ld.rglob("*"):
            if is_launch_file(p):
                results.append(p.resolve())
    # de-dup
    seen = set()
    uniq: List[Path] = []
    for p in results:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq


def ament_prefix_path(env: Optional[dict] = None) -> List[Path]:
    """Return AMENT_PREFIX_PATH entries as Paths from given env (or os.environ)."""
    if env is None:
        env = os.environ
    raw = env.get("AMENT_PREFIX_PATH", "")
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


def get_env_after_source(ros_setup: str, ws_setup: str) -> dict:
    """Return an environment dict as if we sourced the provided setup files.
    Implementation avoids NUL bytes by asking a sourced Python to dump JSON.
    """
    files = []
    if ros_setup:
        files.append(shlex.quote(ros_setup))
    if ws_setup:
        files.append(shlex.quote(ws_setup))

    src_cmd = " && ".join([f"[ -f {f} ] && source {f}" for f in files])

    # Inline python snippet as one string
    py_inline = (
        "python3 - <<'PY'\n"
        "import os, json\n"
        "print(json.dumps(dict(os.environ)))\n"
        "PY"
    )

    if src_cmd:
        bash = f"bash -lc '{src_cmd}; {py_inline}'"
    else:
        bash = f"bash -lc '{py_inline}'"

    try:
        out = subprocess.check_output(bash, shell=True, text=True)
        import json
        env = json.loads(out)
        merged = os.environ.copy()
        merged.update({k: str(v) for k, v in env.items()})
        return merged
    except Exception:
        return os.environ.copy()


def pkg_prefix_via_ros2(pkg: str, env: Optional[dict]) -> Optional[Path]:
    code, out = run_cmd_capture(["ros2", "pkg", "prefix", pkg], env=env)
    if code == 0:
        p = out.strip().splitlines()[0].strip()
        if p:
            return Path(p)
    return None


def list_installed_launch_files(pkg: str, env: Optional[dict]) -> List[LaunchItem]:
    """Look in installed share directory for launch files. Preserve subdirs."""
    found: List[LaunchItem] = []
    prefixes: List[Path] = []
    prefixes.extend(ament_prefix_path(env))
    pp = pkg_prefix_via_ros2(pkg, env)
    if pp:
        prefixes.append(pp)
    seen: set[Path] = set()
    for prefix in prefixes:
        share = prefix / "share" / pkg / "launch"
        if not share.is_dir():
            continue
        for p in share.rglob("*"):
            if not is_launch_file(p):
                continue
            abs_p = p.resolve()
            if abs_p in seen:
                continue
            rel = abs_p.relative_to(share)
            found.append(LaunchItem(kind="inst", pkg=pkg, path=abs_p, rel_from_share_launch=rel))
            seen.add(abs_p)
    return found


def build_source_cmd(ros_setup: str, ws_setup: str) -> str:
    cmds = []
    if ros_setup:
        cmds.append(f"source {shlex.quote(ros_setup)}")
    if ws_setup:
        cmds.append(f"source {shlex.quote(ws_setup)}")
    return " && ".join(cmds) if cmds else ""


def open_in_terminal(command: str, cwd: Optional[Path] = None) -> bool:
    env = os.environ.copy()
    for template in TERMINALS:
        exe = template[0]
        if which(exe):
            cmdline = [s.replace("{CMD}", command) for s in template]
            try:
                subprocess.Popen(cmdline, cwd=str(cwd) if cwd else None, env=env)
                return True
            except Exception:
                continue
    return False


def autodetect_ws_setup(start: Path) -> Optional[str]:
    cur = start.resolve()
    for _ in range(10):
        candidate = cur / "install" / "setup.bash"
        if candidate.exists():
            return str(candidate)
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


# -------------------- Launch-argument parsing (py/xml/yaml) ----------------------

def _ast_to_str(n: ast.AST) -> str:
    if isinstance(n, ast.Constant):
        return str(n.value)
    try:
        return ast.unparse(n)  # py3.9+
    except Exception:
        return "<dynamic>"

def parse_launch_args_py(path: Path):
    """Parse *.launch.py looking for DeclareLaunchArgument calls."""
    specs = []
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))

        class V(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call):
                qname = ""
                if isinstance(node.func, ast.Name):
                    qname = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    parts = []
                    cur = node.func
                    while isinstance(cur, ast.Attribute):
                        parts.append(cur.attr)
                        cur = cur.value
                    if isinstance(cur, ast.Name):
                        parts.append(cur.id)
                    qname = ".".join(reversed(parts))
                if qname.endswith("DeclareLaunchArgument"):
                    info = {"name": None, "default": None, "description": None}
                    if node.args:
                        if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                            info["name"] = node.args[0].value
                        if len(node.args) >= 2:
                            info["default"] = _ast_to_str(node.args[1])
                        if len(node.args) >= 3:
                            info["description"] = _ast_to_str(node.args[2])
                    for kw in node.keywords or []:
                        if kw.arg == "name":
                            info["name"] = _ast_to_str(kw.value)
                        elif kw.arg in ("default_value", "default"):
                            info["default"] = _ast_to_str(kw.value)
                        elif kw.arg == "description":
                            info["description"] = _ast_to_str(kw.value)
                    if info["name"]:
                        specs.append(info)
                self.generic_visit(node)

        V().visit(tree)
    except Exception:
        pass
    return specs

def parse_launch_args_xml(path: Path):
    """Best-effort for ROS 2 XML frontend: <arg name=... default=... description=...>."""
    specs = []
    try:
        root = ET.parse(str(path)).getroot()
        for elem in root.iter():
            if elem.tag.lower().endswith("arg"):
                name = elem.attrib.get("name") or elem.attrib.get("from")
                default = elem.attrib.get("default")
                desc = elem.attrib.get("description") or ""
                if name:
                    specs.append({"name": name, "default": default, "description": desc})
    except Exception:
        pass
    return specs

def parse_launch_args_yaml(path: Path):
    """Very rough YAML support; looks for 'launch-arguments' or 'arguments'."""
    if _yaml is None:
        return []
    specs = []
    try:
        data = _yaml.safe_load(path.read_text(encoding="utf-8"))
        la = None
        if isinstance(data, dict):
            la = data.get("launch-arguments") or data.get("arguments")
        if isinstance(la, list):
            for item in la:
                if isinstance(item, dict):
                    name = item.get("name")
                    default = item.get("default")
                    desc = item.get("description", "")
                    if name:
                        specs.append({"name": name, "default": default, "description": desc})
        elif isinstance(la, dict):
            for name, default in la.items():
                specs.append({"name": str(name), "default": str(default), "description": ""})
    except Exception:
        pass
    return specs

def parse_launch_args_generic(path: Path):
    sfx = path.suffix.lower()
    if sfx == ".py":
        return parse_launch_args_py(path)
    if sfx == ".xml":
        return parse_launch_args_xml(path)
    if sfx in (".yaml", ".yml"):
        return parse_launch_args_yaml(path)
    return []


# ----------------------------- GUI (Tkinter) --------------------------------------
import tkinter as tk
from tkinter import ttk, messagebox

class ArgDialog(tk.Toplevel):
    """Prompt for launch arguments (shows defaults; user may override)."""
    def __init__(self, parent, title: str, arg_specs):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.values = None  # set on OK

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Provide launch arguments (leave blank to use defaults):").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0,8)
        )

        self.entries = []
        for i, spec in enumerate(arg_specs, start=1):
            name = spec.get("name","")
            default = spec.get("default","")
            desc = spec.get("description","")
            ttk.Label(frm, text=name).grid(row=i, column=0, sticky="w", padx=(0,8))
            ent = ttk.Entry(frm, width=42)
            if default not in (None, "", "<dynamic>"):
                ent.insert(0, str(default))
            ent.grid(row=i, column=1, sticky="w")
            ttk.Label(frm, text=str(desc)[:80], foreground="#555").grid(row=i, column=2, sticky="w", padx=(8,0))
            self.entries.append((name, ent))

        btns = ttk.Frame(frm); btns.grid(row=len(arg_specs)+1, column=0, columnspan=3, pady=(10,0), sticky="e")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right", padx=4)
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right", padx=4)

        # --- safer mapping + grab order ---
        self.transient(parent)
        self.withdraw()
        self.update_idletasks()
        # center on parent
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            x, y = px + (pw - w)//2, py + (ph - h)//2
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass
        self.deiconify()
        self.wait_visibility()
        try:
            self.grab_set()
        except Exception:
            pass

        # Let Enter confirm; focus first entry
        self.bind("<Return>", lambda e: self._ok())
        if self.entries:
            self.entries[0][1].focus_set()

        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _ok(self):
        vals = {}
        for name, ent in self.entries:
            v = ent.get().strip()
            if v != "":
                vals[name] = v
        self.values = vals
        self.destroy()

    def _cancel(self):
        self.values = None
        self.destroy()


class Ros2App(tk.Tk):
    def __init__(self, root_path: Path, ros_setup: str, ws_setup: str):
        super().__init__()
        self.title("ROS 2 App Launcher")
        self.geometry("1240x860")

        self.root_path = root_path
        self.ros_setup = ros_setup
        self.ws_setup = ws_setup or autodetect_ws_setup(root_path) or ""
        self.source_prefix = build_source_cmd(self.ros_setup, self.ws_setup)

        # Create a synthetic env as if we had sourced the setups
        self.scanned_env = get_env_after_source(self.ros_setup, self.ws_setup)

        # Data
        self.pkgs: Dict[str, PkgInfo] = {}
        self.pkg_by_name: Dict[str, PkgInfo] = {}
        self.node_to_pkg: Dict[str, str] = {}  # tree node id -> package name
        self.selected_pkg: Optional[PkgInfo] = None

        # map: listbox index -> ("src", Path) OR ("inst", LaunchItem)
        self.launch_map: Dict[int, Tuple[str, object]] = {}

        # args vars
        self.exec_args_var = tk.StringVar()
        self.launch_args_var = tk.StringVar()
        self.custom_launch_var = tk.StringVar()

        self._build_ui()
        self._load_packages()

    # -------------------- UI --------------------
    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text=f"Workspace root: {self.root_path}").pack(side="left")
        ttk.Button(top, text="Refresh", command=self._load_packages).pack(side="left", padx=6)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # Left: workspace tree (folders -> packages)
        left = ttk.Frame(body, padding=4)
        body.add(left, weight=1)
        ttk.Label(left, text="Workspace / Packages").pack(anchor="w")
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.tag_configure("pkg", font=(None, 10, "bold"))

        # Right: executables & launch files
        right = ttk.Frame(body, padding=4)
        body.add(right, weight=2)

        ex_frame = ttk.LabelFrame(right, text="Executables (ros2 run)", padding=6)
        ex_frame.pack(fill="x", expand=False, pady=(0, 6))
        self.exe_list = tk.Listbox(ex_frame, height=10, exportselection=False)
        self.exe_list.pack(fill="x", expand=False)

        ex_args_row = ttk.Frame(ex_frame)
        ex_args_row.pack(fill="x", pady=(6, 0))
        ttk.Label(ex_args_row, text="Args:").pack(side="left")
        ttk.Entry(ex_args_row, textvariable=self.exec_args_var).pack(side="left", fill="x", expand=True)

        ln_frame = ttk.LabelFrame(right, text="Launch files (ros2 launch)", padding=6)
        ln_frame.pack(fill="both", expand=True)
        self.launch_list = tk.Listbox(ln_frame, height=18, exportselection=False)
        self.launch_list.pack(fill="both", expand=True)
        self.launch_list.bind("<Double-Button-1>", self._on_launch_double)

        ln_args_row = ttk.Frame(ln_frame)
        ln_args_row.pack(fill="x", pady=(6, 0))
        ttk.Label(ln_args_row, text="Launch args (e.g., use_sim_time:=true foo:=bar):").pack(side="left")
        ttk.Entry(ln_args_row, textvariable=self.launch_args_var).pack(side="left", fill="x", expand=True)

        custom_row = ttk.Frame(ln_frame)
        custom_row.pack(fill="x", pady=(6, 0))
        ttk.Label(custom_row, text="Custom launch (path or '<pkg> <relpath>'): ").pack(side="left")
        ttk.Entry(custom_row, textvariable=self.custom_launch_var).pack(side="left", fill="x", expand=True)
        ttk.Button(custom_row, text="Run Custom", command=self._run_custom_launch).pack(side="left", padx=6)

        # Bottom buttons
        btns = ttk.Frame(self, padding=(8, 0, 8, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="Run Executable", command=self._run_executable).pack(side="left", padx=4)
        ttk.Button(btns, text="Run Launch", command=self._run_launch).pack(side="left", padx=4)
        ttk.Button(btns, text="Open RViz2", command=lambda: self._run_tool("rviz2")).pack(side="left", padx=18)
        ttk.Button(btns, text="rqt_graph", command=lambda: self._run_tool("ros2 run rqt_graph rqt_graph")).pack(side="left", padx=4)
        ttk.Button(btns, text="rqt (plugins)", command=lambda: self._run_tool("rqt")).pack(side="left", padx=4)

        # Status
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", padx=8, pady=(0,8))

    # -------------------- Data --------------------
    def _set_status(self, msg: str):
        self.status.set(msg)
        self.update_idletasks()

    def _load_packages(self):
        self._set_status("Scanning for packages...")
        self.pkgs = discover_packages(self.root_path)
        self.pkg_by_name = dict(self.pkgs)
        self._rebuild_tree()
        self._clear_details()
        self._set_status(f"Found {len(self.pkgs)} package(s).")

    def _clear_details(self):
        self.exe_list.delete(0, "end")
        self.launch_list.delete(0, "end")
        self.launch_map.clear()
        self.selected_pkg = None

    def _rebuild_tree(self):
        # Build a folder tree from package src dirs
        self.tree.delete(*self.tree.get_children(""))
        root_abs = self.root_path.resolve()
        root_id = self.tree.insert("", "end", text=str(root_abs), open=True)

        # Map of directory Path -> tree node id
        dir_node: Dict[Path, str] = {root_abs: root_id}

        # Sort packages by their source directory
        packages_sorted = sorted(self.pkgs.values(), key=lambda p: str(p.src_dir.resolve()))

        for info in packages_sorted:
            pkg_dir = info.src_dir.resolve()
            # Walk from root_abs to pkg_dir creating intermediate folder nodes
            try:
                rel = pkg_dir.relative_to(root_abs)
                current = root_abs
                parent_id = root_id
                for part in rel.parts[:-1]:
                    current = current / part
                    if current not in dir_node:
                        dir_node[current] = self.tree.insert(parent_id, "end", text=part, open=True)
                    parent_id = dir_node[current]
            except ValueError:
                # pkg outside the root; attach directly under root
                parent_id = root_id

            # Add package node (bold)
            pkg_id = self.tree.insert(parent_id, "end", text=info.name, tags=("pkg",))
            self.node_to_pkg[pkg_id] = info.name

    # -------------------- Selection & populate --------------------
    def _on_tree_select(self, _ev=None):
        sel = self.tree.selection()
        if not sel:
            return
        node = sel[0]
        pkg_name = self.node_to_pkg.get(node)
        if not pkg_name:
            # directory node selected -> clear details
            self._clear_details()
            return
        info = self.pkg_by_name.get(pkg_name)
        if not info:
            self._clear_details()
            return
        self.selected_pkg = info
        self._populate_pkg_details(info)

    def _populate_pkg_details(self, info: PkgInfo):
        """Fill the UI lists for the selected package: executables and launch files."""
        self._set_status(f"Loading details for {info.name}...")

        # Executables
        self.exe_list.delete(0, "end")
        exes = list_executables(info.name, env=self.scanned_env)
        for e in exes:
            self.exe_list.insert("end", e)

        # Launch files (only show filename; keep mapping)
        self.launch_list.delete(0, "end")
        self.launch_map.clear()

        # Source-tree
        src_launches = list_src_launch_files(info.src_dir)
        for lf in src_launches:
            idx = self.launch_list.size()
            self.launch_list.insert("end", f"[src] {lf.name}")
            self.launch_map[idx] = ("src", lf)

        # Installed (preserve subdirectories)
        inst_launches = list_installed_launch_files(info.name, env=self.scanned_env)
        for li in inst_launches:
            idx = self.launch_list.size()
            # show just filename (or use li.rel_from_share_launch.as_posix() to show subpath)
            self.launch_list.insert("end", f"[inst] {li.path.name}")
            self.launch_map[idx] = ("inst", li)

        self._set_status(
            f"{info.name}: {len(exes)} executables, {len(src_launches) + len(inst_launches)} launch files"
        )

    # -------------------- Run helpers --------------------
    def _prefixed(self, cmd: str, workdir: Optional[Path] = None) -> str:
        pieces = []
        if self.source_prefix:
            pieces.append(self.source_prefix)
        if workdir:
            pieces.append(f"cd '{workdir}'")
        pieces.append(cmd)
        return " && ".join(pieces)

    def _on_launch_double(self, ev):
        # Ensure the item under the mouse is selected, then defer launch a tick
        try:
            idx = self.launch_list.nearest(ev.y)
            if idx is not None and idx >= 0:
                self.launch_list.selection_clear(0, "end")
                self.launch_list.selection_set(idx)
                self.launch_list.activate(idx)
        except Exception:
            pass
        self.after(1, self._run_launch)

    def _run_tool(self, cmd: str):
        full = self._prefixed(cmd)
        if not open_in_terminal(full):
            messagebox.showerror("Error", "Could not open a terminal to run the command.")
        else:
            self._set_status(f"Started: {cmd}")

    def _run_executable(self):
        if not self.selected_pkg:
            messagebox.showwarning("No package selected", "Select a package first.")
            return
        sel = self.exe_list.curselection()
        if not sel:
            messagebox.showwarning("No executable selected", "Select an executable to run.")
            return
        exe = self.exe_list.get(sel[0])
        extra = self.exec_args_var.get().strip()
        cmd = f"ros2 run {self.selected_pkg.name} {exe}"
        if extra:
            cmd += f" {extra}"
        full = self._prefixed(cmd)
        if not open_in_terminal(full):
            messagebox.showerror("Error", "Could not open a terminal to run the executable.")
        else:
            self._set_status(f"Running: {cmd}")

    def _run_launch(self):
        """Run the selected launch file (parses args, prompts for values, YAML-aware quoting)."""
        if not self.selected_pkg:
            messagebox.showwarning("No package selected", "Select a package first.")
            return

        sel = self.launch_list.curselection()
        if not sel:
            messagebox.showwarning("No launch selected", "Select a launch file to run.")
            return

        idx = sel[0]
        if idx not in self.launch_map:
            messagebox.showerror("Error", "Internal mapping for the selected launch file is missing.")
            return

        tag, obj = self.launch_map[idx]

        if tag == "inst":
            li: LaunchItem = obj  # type: ignore
            path = li.path.resolve()
            rel_display = li.rel_from_share_launch.as_posix() if li.rel_from_share_launch else path.name
            workdir = path.parent
            arg_specs = parse_launch_args_generic(path)
        else:
            path: Path = obj  # type: ignore
            path = path.resolve()
            rel_display = path.name
            workdir = path.parent
            arg_specs = parse_launch_args_generic(path)

        # ---- prompt for values if the file declares arguments
        user_args = {}
        if arg_specs:
            dlg = ArgDialog(self, f"Launch arguments: {rel_display}", arg_specs)
            self.wait_window(dlg)
            if dlg.values is None:
                self._set_status("Launch cancelled.")
                return
            user_args = dlg.values  # dict: name -> str

        # ---- YAML-aware formatting (avoid quoting lists/numbers/bools/dicts)
        import re
        def _looks_yaml_scalar_or_collection(s: str) -> bool:
            s = s.strip()
            if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
                return True
            if s.lower() in ("true", "false", "null", "~"):
                return True
            if re.fullmatch(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?", s):
                return True
            return False

        def _fmt_arg(k: str, v: str) -> str:
            v = v.strip()
            # Respect user's own quotes
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                return f"{k}:={v}"
            # Only quote plain strings; pass YAML-ish values as-is
            return f"{k}:={v}" if _looks_yaml_scalar_or_collection(v) else f"{k}:={shlex.quote(v)}"

        arg_pairs = [_fmt_arg(k, v) for k, v in user_args.items()]

        # Also append anything typed in the free-form "Launch args" field (if present)
        extra_text = ""
        if hasattr(self, "launch_args_var") and hasattr(self.launch_args_var, "get"):
            extra_text = self.launch_args_var.get().strip()
        extra_parts = shlex.split(extra_text) if extra_text else []

        # ---- build command
        if tag == "inst":
            rel_sub = li.rel_from_share_launch.as_posix() if li.rel_from_share_launch else path.name  # type: ignore
            cmd_elems = ["ros2", "launch", self.selected_pkg.name, rel_sub] + arg_pairs + extra_parts
        else:
            cmd_elems = ["ros2", "launch", str(path)] + arg_pairs + extra_parts

        cmd = " ".join(shlex.quote(x) for x in cmd_elems)
        full = self._prefixed(cmd, workdir=workdir)
        if not open_in_terminal(full, cwd=workdir):
            messagebox.showerror("Error", f"Could not open a terminal to run:\n{cmd}")
            return

        self._set_status(f"Launching: {cmd}")

    def _run_custom_launch(self):
        spec = self.custom_launch_var.get().strip()
        if not spec:
            messagebox.showwarning("No spec", "Enter a path or '<pkg> <relpath>'.")
            return
        extra = self.launch_args_var.get().strip()

        p = Path(spec).expanduser()
        if p.exists():
            cmd = f"ros2 launch '{p}'"
            if extra:
                cmd += f" {extra}"
            full = self._prefixed(cmd)
            ok = open_in_terminal(full, cwd=p.parent)
        else:
            parts = spec.split(maxsplit=1)
            if len(parts) != 2:
                messagebox.showerror("Bad spec", "Use a file path or '<pkg> <relpath>'.")
                return
            pkg, rel = parts[0], parts[1]
            cmd = f"ros2 launch {pkg} {rel}"
            if extra:
                cmd += f" {extra}"
            full = self._prefixed(cmd)
            ok = open_in_terminal(full)
        if not ok:
            messagebox.showerror("Error", "Could not open a terminal to run the custom launch.")
        else:
            self._set_status(f"Launching: {spec}")


# ----------------------------- CLI / entrypoint -----------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="ROS 2 App Launcher (Tkinter)")
    ap.add_argument("--root", default=str(Path.cwd()), help="Root folder to scan for package.xml files (default: CWD)")
    ap.add_argument("--ros-setup", default=DEFAULT_ROS_SETUP, help="Path to ROS distro setup.bash (empty to skip)")
    ap.add_argument("--ws-setup", default=DEFAULT_WS_SETUP, help="Path to workspace install/setup.bash (empty => auto-detect upward)")
    return ap.parse_args()


def main():
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    ros_setup = args.ros_setup.strip()
    ws_setup = args.ws_setup.strip()
    if ws_setup:
        ws_setup = str(Path(ws_setup).expanduser())
    if ros_setup and not Path(ros_setup).exists():
        print(f"[WARN] ROS setup not found: {ros_setup}", file=sys.stderr)
    if ws_setup and not Path(ws_setup).exists():
        print(f"[WARN] Workspace setup not found: {ws_setup}", file=sys.stderr)

    try:
        import tkinter  # noqa: F401
    except Exception as e:
        print("Tkinter is required. On Ubuntu: sudo apt install python3-tk", file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

    app = Ros2App(root, ros_setup, ws_setup)
    app.mainloop()


if __name__ == "__main__":
    main()
