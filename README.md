# 🚀 ROS 2 App Launcher

A **graphical launcher for ROS 2** that makes it simple to discover, run, and manage executables and launch files in your ROS 2 workspaces.  
No more memorizing long `ros2 run` or `ros2 launch` commands — just point it to your workspace and click!

---

## ✨ Features

- 🔍 **Automatic Workspace Scanning**
  - Detects ROS 2 packages under a given root folder (`--root`).
  - Finds both **source-tree packages** and **installed packages**.

- ⚙️ **Executables Management**
  - Lists executables available in each package.
  - Run them with `ros2 run <pkg> <exe>` directly from the GUI.
  - Add extra arguments in the provided input field.

- 🚀 **Launch Files Management**
  - Detects `.launch.py`, `.launch.xml`, `.launch.yaml`, and files ending with `_launch.*`.
  - Works for both **source** (`./launch/`) and **installed** (`share/<pkg>/launch/`) packages.
  - Automatically parses and prompts for **launch arguments** (Python, XML, YAML).
  - Provides a dialog box where you can override defaults before launching.

- 📝 **Custom Launch**
  - Enter either a **path** or a `<package> <launch_file>` spec.
  - Supports additional arguments.

- 🖥️ **Handy Shortcuts**
  - Launch RViz2 with one click.
  - Launch `rqt_graph`.
  - Launch `rqt` (plugins).

- 🎨 **Desktop Integration**
  - Installs as a **desktop app** with its own icon (`kros.png`).
  - Launchable from your system’s application menu.
  - Search **"ROS 2 App Launcher"** in your desktop environment.

---

## 📦 Requirements

- **ROS 2** (tested with **Jazzy**, compatible with Humble/Foxy).
- **Python 3.9+**
- **Tkinter** for GUI:
  ```bash
  sudo apt install python3-tk

## Run from source
python3 ros2_app_launcher.py --root ~/my_ros2_ws
