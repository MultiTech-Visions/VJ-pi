#!/bin/bash
# pi-paint VJ — dual display launcher (main use case).
# Double-click in the file manager and choose "Execute".
#
# Control HUD     → display 0 (your small screen)
# Projector output → display 1 (fullscreen)
#
# If your displays are swapped, edit OUTPUT_DISPLAY and CONTROL_DISPLAY below.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  zenity --error --text="Setup hasn't been run yet.\n\nDouble-click setup.sh first." 2>/dev/null \
    || (echo "ERROR: venv not found — please run setup.sh first." && read -p "Press Enter to close...")
  exit 1
fi

./venv/bin/python main.py \
  --fullscreen \
  --output-display "$OUTPUT_DISPLAY" \
  --control \
  --control-display "$CONTROL_DISPLAY" \
  --control-size "$CONTROL_SIZE"
