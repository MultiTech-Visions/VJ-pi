#!/bin/bash
# Add the labwc window rule that fullscreens the GPU compositor's video
# window onto the projector (HDMI-A-2). Safe: backs up rc.xml and validates
# the XML before/after. Double-click -> "Execute in Terminal".

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_fsrule.log"

PY=/usr/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3

"$PY" apply_fullscreen_rule.py 2>&1 | tee "$LOG"

echo
echo "Now double-click 'Run GPU Compositor (test).sh' — it should open"
echo "fullscreen on the projector this time."
read -p "Press Enter to close..."
