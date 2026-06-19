#!/bin/bash
# Master software-brightness control for the projector + field monitor.
#
# Double-click in the file manager and choose "Execute". A small window
# opens with one slider per connected display. Drag to dim a screen whose
# own brightness buttons do nothing (the broken projector, the portable
# panel). Closing the window restores every screen to full brightness.
#
# Runs on the SYSTEM python3 (this is a desktop utility, not part of the VJ
# venv) and needs Tk. If Tk is missing it tells you to run
# "Setup Display Brightness.sh" once.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_brightness.log"

show_error() {
  local title="$1" body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null
  elif command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
  else
    echo "$title"; echo "$body"
  fi
}

PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3

: >"$LOG"
date '+[brightness] start: %Y-%m-%d %H:%M:%S' >>"$LOG"
echo "session=${XDG_SESSION_TYPE:-?} wayland=${WAYLAND_DISPLAY:-none} display=${DISPLAY:-none}" >>"$LOG"

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  show_error "Display Brightness: one-time setup needed" \
    "The graphics toolkit (python3-tk) isn't installed.\n\nDouble-click \"Setup Display Brightness.sh\" once, then run this again.\n\nLog: $LOG"
  exit 1
fi

"$PY" display_brightness.py >>"$LOG" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -25 "$LOG" 2>/dev/null)
  show_error "Display Brightness: couldn't start (exit $EXIT)" \
    "See log: $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
