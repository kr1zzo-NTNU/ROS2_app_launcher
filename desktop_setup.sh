# Paths
SRC_DIR="$PWD"
APP_DIR="/opt/ros2-app-launcher"

# Install app files system-wide
sudo install -Dm755 "$SRC_DIR/ros2_app_launcher.py" "$APP_DIR/ros2_app_launcher.py"
sudo install -Dm644 "$SRC_DIR/kros.png"             "$APP_DIR/kros.png"

# Wrapper script (uses current user's $HOME at runtime)
sudo tee "$APP_DIR/run.sh" > /dev/null <<'EOSH'
#!/usr/bin/env bash
exec python3 /opt/ros2-app-launcher/ros2_app_launcher.py --root "$HOME"
EOSH
sudo chmod +x "$APP_DIR/run.sh"

# Desktop entry (no $HOME here → no quoting issues)
sudo tee /usr/share/applications/ros2-app-launcher.desktop > /dev/null <<'EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=ROS 2 App Launcher
Comment=Scan, run and launch ROS 2 packages
Exec=/opt/ros2-app-launcher/run.sh
Icon=/opt/ros2-app-launcher/kros.png
Terminal=false
Categories=Development;
StartupNotify=true
EOF

# Refresh and test
sudo desktop-file-validate /usr/share/applications/ros2-app-launcher.desktop || true
sudo update-desktop-database /usr/share/applications
gtk-launch ros2-app-launcher 2>/dev/null || true

