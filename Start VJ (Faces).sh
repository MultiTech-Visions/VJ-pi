#!/bin/bash
# pi-paint VJ — face-cloud launcher.
# Double-click in the file manager and choose "Execute".
#
# Exactly like "Start VJ.sh" but boots straight into the face point-cloud
# base layer (assets/faces/*.npz, captured with "Capture Face.sh"), so the
# projector shows a slowly-rotating scanned face the moment it opens.
# Every FX (F1-F8), hit (Z-B) and overlay works on it.
#
#   `         toggle the face cloud on/off any time (backtick, top-left)
#   , / .     previous / next face
#   ←→↑↓      turn the head left/right (←→) and tip it up/down (↑↓)
#   -/= [/]   switch back to clips / generators (turns the face off)
#
# If nothing shows, you haven't captured any faces yet — double-click
# "Capture Face.sh" first.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

# Render canvas resolution. Leave empty to use config.py's default.
RENDER_WIDTH=""
RENDER_HEIGHT=""

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
  echo "$title"
  echo "$body"
}

: >"$LOG"
date '+[VJ] faces launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup needed" \
    "Setup hasn't been run yet.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

if [ -z "$(ls assets/faces/*.npz 2>/dev/null)" ]; then
  show_error "VJ-pi: no faces captured yet" \
    "There are no faces in assets/faces/.\n\nDouble-click \"Capture Face.sh\" to scan some first, then run this again.\n\n(The app will still open — just press a clip/generator key to use it normally.)"
fi

ARGS=( --fullscreen --gpu-scale --control --control-display "$CONTROL_DISPLAY" \
       --control-size "$CONTROL_SIZE" --faces )
if [ -n "$RENDER_WIDTH" ] && [ -n "$RENDER_HEIGHT" ]; then
  ARGS+=( --width "$RENDER_WIDTH" --height "$RENDER_HEIGHT" )
fi
if [ ! -f vj_state.json ]; then
  ARGS+=( --output-display "$OUTPUT_DISPLAY" )
fi

./venv/bin/python main.py "${ARGS[@]}" >>"$LOG" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -40 "$LOG" 2>/dev/null)
  show_error "VJ-pi crashed (exit $EXIT)" \
    "main.py exited with status $EXIT.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
