#!/bin/bash
# pi-paint VJ — single-screen test mode.
# Both windows (output + control HUD) on display 0 as resizable windows.
# Use this when there's no projector connected, just to try things out.

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  zenity --error --text="Setup hasn't been run yet.\n\nDouble-click setup.sh first." 2>/dev/null \
    || (echo "ERROR: venv not found — please run setup.sh first." && read -p "Press Enter to close...")
  exit 1
fi

./venv/bin/python main.py \
  --output-display 0 \
  --control \
  --control-display 0 \
  --control-size "680x720"
