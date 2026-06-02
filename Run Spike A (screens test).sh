#!/bin/bash
# Spike A — dual-screen V3D survival test.
# Double-click in the file manager and choose "Execute in Terminal".
#
# Opens a GPU output window on the projector + a software window on the
# operator screen for 60s. Watch: does either go BLACK or freeze?

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_spike_a.log"

OUTPUT_DISPLAY=1     # projector
CONTROL_DISPLAY=0    # operator screen

show_dialog() {  # $1=error|info $2=title $3=body
  if command -v zenity >/dev/null 2>&1; then
    zenity --"$1" --width=720 --title="$2" --text="$3" 2>/dev/null; return
  fi
  printf '%s\n\n%s\n' "$2" "$3"
}

if [ ! -d "venv" ]; then
  show_dialog error "VJ-pi: setup needed" "Run setup.sh first."
  read -p "Press Enter to close..."; exit 1
fi

: >"$LOG"
date '+[spike-a] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"

./venv/bin/python tests/spike_a_dualscreen.py \
    --output-display "$OUTPUT_DISPLAY" --control-display "$CONTROL_DISPLAY" \
    --fullscreen --seconds 60 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

TAIL=$(tail -25 "$LOG" 2>/dev/null)
if [ "$EXIT" -eq 0 ]; then
  show_dialog info "Spike A finished" \
    "Did either window go black/frozen?\n\nLast lines (send these to Sam):\n\n$TAIL"
else
  show_dialog error "Spike A errored (exit $EXIT)" \
    "Log: $LOG\n\nLast lines:\n\n$TAIL"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
