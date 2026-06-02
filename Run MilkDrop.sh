#!/bin/bash
# MilkDrop-style visualizer on the projector. Double-click -> "Execute in
# Terminal". The GL window takes the keyboard:
#   n / ] / space = next preset   p / [ = previous   a = auto-cycle
#   f = feedback resolution        Esc = quit

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_milkdrop.log"

show_dialog() {
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
if ! ./venv/bin/python -c "import moderngl" >/dev/null 2>&1; then
  echo "[milkdrop] installing moderngl into the venv (one-time)..." | tee -a "$LOG"
  ./venv/bin/pip install moderngl 2>&1 | tee -a "$LOG"
fi

date '+[milkdrop] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"
./venv/bin/python milkdrop.py --display 1 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

if [ "$EXIT" -ne 0 ]; then
  show_dialog error "MilkDrop errored (exit $EXIT)" \
    "Log: $LOG\n\n$(tail -20 "$LOG" 2>/dev/null)"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
