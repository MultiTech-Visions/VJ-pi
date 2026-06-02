#!/bin/bash
# One-time helper for cinematic 4K mode: fullscreen the GStreamer GL
# video window on the projector under labwc/Wayland.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_fsrule.log"

PY=/usr/bin/python3
if [ ! -x "$PY" ]; then
  PY=python3
fi

"$PY" apply_fullscreen_rule.py 2>&1 | tee "$LOG"

echo
echo "Now start the VJ app and press N. The cinematic video window should"
echo "move to the projector and go fullscreen."
read -p "Press Enter to close..."
