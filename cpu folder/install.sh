#!/usr/bin/env bash
# Installer for CPU Control v3.5 - Linux Mint / XFCE
set -euo pipefail

INSTALL_DIR="/opt/cpu-control"
DESKTOP_DIR="/usr/share/applications"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== CPU Control v3.5 installer =="

if [[ $EUID -ne 0 ]]; then
    echo "This step needs sudo to install into $INSTALL_DIR and register the app menu entry."
    exec sudo bash "$0" "$@"
fi

# Check for GTK bindings
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
    echo "Installing python3-gi (GTK3 bindings)..."
    apt-get update -y
    apt-get install -y python3-gi gir1.2-gtk-3.0 policykit-1
fi

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/cpu_ctrl_v3_5.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/cpu_ctrl_helper" "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/cpu_ctrl_v3_5.py"
chmod 755 "$INSTALL_DIR/cpu_ctrl_helper"

sed "s|Exec=.*|Exec=python3 $INSTALL_DIR/cpu_ctrl_v3_5.py|" \
    "$SCRIPT_DIR/cpu-control.desktop" > "$DESKTOP_DIR/cpu-control.desktop"
chmod 644 "$DESKTOP_DIR/cpu-control.desktop"

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "Done. Find 'CPU Control' in your XFCE application menu (System category),"
echo "or launch it directly with:  python3 $INSTALL_DIR/cpu_ctrl_v3_5.py"
