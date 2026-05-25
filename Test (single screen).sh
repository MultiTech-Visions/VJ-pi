#!/bin/bash
# pi-paint VJ — single-screen test mode.
# Both windows (output + control HUD) on display 0 as resizable windows.
# Use this when there's no projector connected, just to try things out.
#
# Failure reporting matches Start VJ.sh: stdout/stderr go to
# vj_last_run.log and a GUI dialog pops on non-zero exit so silent
# "Execute" failures don't vanish into the void.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_run.log"

show_error() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --no-markup --text="$body" 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
    return
  fi
  for term in lxterminal xterm gnome-terminal mate-terminal x-terminal-emulator; do
    if command -v "$term" >/dev/null 2>&1; then
      "$term" -e bash -c "printf '%s\n\n%s\n\n' '$title' '$body'; read -p 'Press Enter to close...'"
      return
    fi
  done
}

show_log_dialog() {
  local title="$1"
  local logfile="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --text-info --title="$title" --filename="$logfile" \
      --width=900 --height=600 --no-wrap 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    xmessage -file "$logfile" -title "$title" 2>/dev/null
    return
  fi
  for term in lxterminal xterm gnome-terminal mate-terminal x-terminal-emulator; do
    if command -v "$term" >/dev/null 2>&1; then
      "$term" -e bash -c "less '$logfile'; read -p 'Press Enter to close...'"
      return
    fi
  done
}

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup missing" \
    "The Python virtualenv ./venv/ doesn't exist.\n\nDouble-click setup.sh first."
  exit 1
fi

: >"$LOG"
if ! ./venv/bin/python -c "import pygame, moderngl, numpy, cv2" >>"$LOG" 2>&1; then
  echo "[VJ] missing python deps — running pip install -r requirements.txt..." >>"$LOG"
  if ! ./venv/bin/pip install -r requirements.txt >>"$LOG" 2>&1; then
    show_log_dialog "VJ-pi: dependency install failed — $LOG" "$LOG"
    exit 1
  fi
fi

./venv/bin/python main.py \
  --output-display 0 \
  --control \
  --control-display 0 \
  --control-size "680x720" >>"$LOG" 2>&1
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
  show_log_dialog "VJ-pi crashed (exit $EXIT) — $LOG" "$LOG"
fi
exit "$EXIT"
