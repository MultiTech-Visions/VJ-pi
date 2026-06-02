#!/bin/bash
# Spike E — MilkDrop-style feedback test. Opens a GL window on the
# projector running a ping-pong feedback loop; watch for flowing trails.
# Double-click -> "Execute in Terminal". Esc (or close the window) to quit.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_spike_e.log"

show_dialog() {  # $1=error|info $2=title $3=body
  if command -v zenity >/dev/null 2>&1; then
    zenity --"$1" --width=720 --title="$2" --text="$3" 2>/dev/null; return
  fi
  printf '%s\n\n%s\n' "$2" "$3"
}

if [ ! -d venv ]; then
  show_dialog error "VJ-pi: setup needed" "Run setup.sh first."
  read -p "Press Enter to close..."; exit 1
fi

: >"$LOG"
# moderngl lives in the venv; install it once if missing (no full setup re-run).
if ! ./venv/bin/python -c "import moderngl" >/dev/null 2>&1; then
  echo "[spike-e] installing moderngl into the venv (one-time)..." | tee -a "$LOG"
  ./venv/bin/pip install moderngl 2>&1 | tee -a "$LOG"
fi

date '+[spike-e] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"
# display 1 = projector (matches Start VJ.sh's OUTPUT_DISPLAY).
./venv/bin/python tests/spike_e_feedback.py --display 1 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

TAIL=$(grep -E 'fps|GL:|ERROR|Error|Traceback' "$LOG" 2>/dev/null | tail -12)
if [ "$EXIT" -eq 0 ]; then
  show_dialog info "Spike E finished — send Sam this" \
    "Did you see flowing TRAILS (not just a moving dot)? That's MilkDrop-style feedback working on V3D.\n\n$TAIL"
else
  show_dialog error "Spike E errored (exit $EXIT)" \
    "Log: $LOG\n\n$(tail -20 "$LOG" 2>/dev/null)"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
