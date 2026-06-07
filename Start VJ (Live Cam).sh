#!/bin/bash
# pi-paint VJ — live-webcam launcher.
# Double-click in the file manager and choose "Execute".
#
# Exactly like "Start VJ.sh" but boots straight into the live USB webcam
# as the base layer, so the projector shows the camera the moment it opens.
# Everything else is identical — every FX (F1-F8), hit (Z-B), overlay and
# projection-map works on the live feed.
#
#   \         toggle the live cam on/off any time
#   Shift+\   flip the selfie mirror
#   -/= [/]   switch back to clips / generators (turns the cam off)
#
# The webcam is auto-detected (first /dev/videoN that delivers frames).
# If it ever picks the wrong device, set CAMERA_DEVICE below to a number
# (run "List Cameras.sh" to see the choices).

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

# Leave empty to auto-detect the webcam; set to e.g. 0 or 2 to force one.
CAMERA_DEVICE=""

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
date '+[VJ] live-cam launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup needed" \
    "Setup hasn't been run yet.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

ARGS=( --fullscreen --gpu-scale --control --control-display "$CONTROL_DISPLAY" \
       --control-size "$CONTROL_SIZE" --camera )
if [ -n "$CAMERA_DEVICE" ]; then
  ARGS+=( --camera-device "$CAMERA_DEVICE" )
fi
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
