#!/bin/bash
# pi-paint VJ — single-screen test mode.
# Both windows (output + control HUD) on display 0 as resizable windows.
# Use this when there's no projector connected, just to try things out.
#
# Same log-tee + GUI-error-on-crash pattern as Start VJ.sh.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_run.log"

show_error() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null
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

: >"$LOG"
date '+[VJ] launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if ! python3 -c "
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, Gst
" >>"$LOG" 2>&1; then
  show_error "VJ-pi: missing dependencies" \
    "The GTK3 / GStreamer Python bindings aren't installed.\n\nRun setup.sh first.\n\nLog: $LOG\n\nLast lines:\n\n$(tail -20 "$LOG")"
  exit 1
fi

# --single-screen tells the app to place both windows on display 0
# and skip the projector-fullscreen path. Flag is consumed by main.py
# in a later phase; phase 1 just respects whichever monitor the WM
# lands on.
python3 main.py --single-screen "$@" >>"$LOG" 2>&1
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -40 "$LOG" 2>/dev/null)
  show_error "VJ-pi crashed (exit $EXIT)" \
    "main.py exited with status $EXIT.\n\nFull log:  $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
