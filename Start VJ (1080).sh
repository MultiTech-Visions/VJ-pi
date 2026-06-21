#!/bin/bash
# pi-paint VJ — 1080p canvas launcher.
# Double-click in the file manager and choose "Execute".
#
# Same as Start VJ.sh (software clip decode, NO --hevc) but with the render
# canvas at 1920x1080 instead of the default 1280x720. The balanced choice:
# noticeably sharper than 720p, ~2.25x the compositor cost. Sits between
# Start VJ.sh (720p, most headroom) and Start VJ (2K).sh (most detail).
#
# If you set the projector to native 1080p, this is the sweet spot: the GPU
# presents the canvas 1:1 with no upscale softness and no wasted pixels.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="fullscreen"

# The ONE difference from Start VJ.sh: the 1080p canvas.
RENDER_WIDTH=1920
RENDER_HEIGHT=1080

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
date '+[VJ] 1080 canvas launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup needed" \
    "Setup hasn't been run yet.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

# Only pass --output-display when no saved choice exists; the HUD picker's
# persistent selection always wins.
ARGS=( --fullscreen --gpu-scale --control --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )
if [ -n "$RENDER_WIDTH" ] && [ -n "$RENDER_HEIGHT" ]; then
  ARGS+=( --width "$RENDER_WIDTH" --height "$RENDER_HEIGHT" )
fi
if [ ! -f vj_state.json ]; then
  ARGS+=( --output-display "$OUTPUT_DISPLAY" )
fi

# ── projectM (MilkDrop) safety profile ───────────────────────────────
# Identical to Start VJ.sh so PM behaviour is the same.
export VJ_PM_IN_MAPPING="${VJ_PM_IN_MAPPING:-1}"
export VJ_PM_RENDER_MAX_W="${VJ_PM_RENDER_MAX_W:-480}"
export VJ_PM_MESH="${VJ_PM_MESH:-24x16}"
export VJ_PM_STREAM_FPS="${VJ_PM_STREAM_FPS:-18}"
export VJ_PM_COMPOSITE_FPS="${VJ_PM_COMPOSITE_FPS:-18}"
export VJ_PM_SWITCH_MS="${VJ_PM_SWITCH_MS:-550}"
export VJ_PM_PRESENT_STALL_MS="${VJ_PM_PRESENT_STALL_MS:-220}"
export VJ_PM_PRESENT_STALLS="${VJ_PM_PRESENT_STALLS:-2}"
export VJ_PM_SAFETY_COOLDOWN_S="${VJ_PM_SAFETY_COOLDOWN_S:-8}"

./venv/bin/python main.py "${ARGS[@]}" >>"$LOG" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -40 "$LOG" 2>/dev/null)
  show_error "VJ-pi 1080 canvas crashed (exit $EXIT)" \
    "main.py exited with status $EXIT.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
