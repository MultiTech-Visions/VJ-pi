#!/bin/bash
# pi-paint VJ — dual display launcher (main use case).
# Double-click in the file manager and choose "Execute".
#
# Control HUD     → display 0 (your small screen) by default
# Projector output → display 1 (fullscreen) by default on first launch
#
# After the first launch you can pick the output display from the
# Control HUD (F11 to cycle, F12 to apply, or click the buttons). That
# choice is saved to vj_state.json and reused on every subsequent
# launch — so this script's OUTPUT_DISPLAY only matters until you've
# applied a pick once. To force a different display on the next launch,
# either re-apply via the HUD or delete vj_state.json.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  zenity --error --text="Setup hasn't been run yet.\n\nDouble-click setup.sh first." 2>/dev/null \
    || (echo "ERROR: venv not found — please run setup.sh first." && read -p "Press Enter to close...")
  exit 1
fi

# Only pass --output-display when no saved choice exists; that way the
# HUD picker's persistent selection always wins. Uncomment the second
# block instead if you want this script to override the saved state.
ARGS=( --fullscreen --control --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )
if [ ! -f vj_state.json ]; then
  ARGS+=( --output-display "$OUTPUT_DISPLAY" )
fi
# To force OUTPUT_DISPLAY every launch, comment the block above and
# uncomment this one:
# ARGS=( --fullscreen --output-display "$OUTPUT_DISPLAY" --control \
#        --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )

./venv/bin/python main.py "${ARGS[@]}"
