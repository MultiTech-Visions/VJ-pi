#!/bin/bash
# pi-paint VJ — 2K-CANVAS TEST launcher (diagnostic, not for shows).
# Double-click in the file manager and choose "Execute".
#
# Purpose: this is your normal route (software clip decode, NO --hevc) but
# with the render canvas forced to 2048x1152 instead of the usual 1280x720.
# It isolates ONE variable — canvas resolution — so we can tell whether the
# 2K HEVC route felt slow because of the 2K canvas (2.56x the pixels through
# the CPU compositor) or because of the HEVC decode plumbing.
#
# HOW TO READ THE RESULT: launch this, then watch the FPS readout on the
# Control HUD (and try a heavy moment — mapping / FX).
#   • If FPS tanks to roughly what the 2K HEVC route gave  -> the CANVAS
#     RESOLUTION is the cause. The HEVC plumbing was mostly innocent.
#   • If FPS stays close to your normal 1280x720 route      -> resolution
#     wasn't it; the HEVC decode plumbing was the real drag.
#
# Everything else is identical to Start VJ.sh. Clips still come from
# assets/clips/ (software-decoded) and are resized to the 2K canvas per
# frame, so expect a little extra resize cost on top of the pixel count.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

# The ONE difference from Start VJ.sh: force the 2K canvas.
RENDER_WIDTH=2048
RENDER_HEIGHT=1152

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_run_2ktest.log"

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
date '+[VJ] 2K canvas test launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup needed" \
    "Setup hasn't been run yet.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

# Only pass --output-display when no saved choice exists; that way the
# HUD picker's persistent selection always wins.
ARGS=( --fullscreen --gpu-scale --control --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )
if [ -n "$RENDER_WIDTH" ] && [ -n "$RENDER_HEIGHT" ]; then
  ARGS+=( --width "$RENDER_WIDTH" --height "$RENDER_HEIGHT" )
fi
if [ ! -f vj_state.json ]; then
  ARGS+=( --output-display "$OUTPUT_DISPLAY" )
fi

# ── projectM (MilkDrop) safety profile ───────────────────────────────
# Identical to Start VJ.sh so PM behaviour is the same in the test.
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
  show_error "VJ-pi 2K canvas test crashed (exit $EXIT)" \
    "main.py exited with status $EXIT.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
