#!/bin/bash
# Installs clickable launcher icons on the Pi's desktop.
# Double-click in the file manager and choose "Execute in Terminal".

set -e
cd "$(dirname "$0")"
VJ_DIR="$(pwd)"
DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"

echo ""
echo "Installing VJ launcher icons to: $DESKTOP_DIR"
echo ""

mkdir -p "$DESKTOP_DIR"

write_launcher() {
  local name="$1"
  local script="$2"
  local comment="$3"
  local file="$DESKTOP_DIR/$name.desktop"
  cat > "$file" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$name
Comment=$comment
Exec=bash "$VJ_DIR/$script"
Icon=video-display
Path=$VJ_DIR
Terminal=false
Categories=AudioVideo;
StartupNotify=true
EOF
  chmod +x "$file"
  # Mark as trusted (Raspberry Pi OS / GNOME Files)
  gio set "$file" metadata::trusted true 2>/dev/null || true
  echo "  ✓ $name"
}

write_launcher "Start VJ"             "Start VJ.sh"              "Dual display: control HUD + projector output"
write_launcher "VJ Test (single)"     "Test (single screen).sh"  "Single screen test mode — both windows on primary"
write_launcher "Display Brightness"   "Display Brightness.sh"     "Software brightness sliders for the projector + field monitor"

echo ""
echo "Done. Look for the icons on your desktop."
echo "If they show a '?' icon, right-click and choose 'Allow Launching'."
echo ""
read -p "Press Enter to close..."
