#!/bin/bash
# pi-paint VJ — 2K HARDWARE-HEVC launcher.
# Double-click in the file manager and choose "Execute".
#
# Plays clips from assets/clips_hevc/ using the Pi 5's hardware HEVC decoder
# (out-of-process gl worker) on a 2048x1152 canvas — sharper than 1080p, with
# the live FX chain. Bake clips to 2048x1152 HEVC on your PC with the
# pc_clip_baker tool, then copy them into assets/clips_hevc/.
#
# Same dual-display behaviour as "Start VJ.sh": Control HUD on display 0,
# projector output fullscreen (display picked here on first launch, then from
# the HUD F11/F12 and saved to vj_state.json).

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

# Locked to the geometry the gl HEVC decode path requires. Don't change these
# without re-baking clips to match (clips_hevc must be 2048x1152).
RENDER_WIDTH=2048
RENDER_HEIGHT=1152

# Production projectM profile for the 2K HEVC show path. projectM shares the
# Pi 5 GPU with HEVC decode, SDL output scaling, and mapping, so the double-
# click launcher uses conservative defaults that prioritize long-run stability.
# These can still be overridden by the environment for one-off bench testing.
export VJ_PM_RENDER_MAX_W="${VJ_PM_RENDER_MAX_W:-640}"
export VJ_PM_MESH="${VJ_PM_MESH:-32x24}"
export VJ_PM_STREAM_FPS="${VJ_PM_STREAM_FPS:-24}"
export VJ_PM_COMPOSITE_FPS="${VJ_PM_COMPOSITE_FPS:-24}"
export VJ_PM_IN_MAPPING="${VJ_PM_IN_MAPPING:-0}"
export VJ_PM_SWITCH_MS="${VJ_PM_SWITCH_MS:-550}"
export VJ_PM_PRESENT_STALL_MS="${VJ_PM_PRESENT_STALL_MS:-220}"
export VJ_PM_PRESENT_STALLS="${VJ_PM_PRESENT_STALLS:-2}"
export VJ_PM_SAFETY_COOLDOWN_S="${VJ_PM_SAFETY_COOLDOWN_S:-8}"

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_run.log"

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

: >"$LOG"
date '+[VJ] HEVC launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup needed" \
    "Setup hasn't been run yet.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

# Warn (don't block) if there are no HEVC clips yet.
if ! ls assets/clips_hevc/*.mp4 >/dev/null 2>&1; then
  show_error "No HEVC clips yet" \
    "assets/clips_hevc/ has no .mp4 clips.\n\nBake clips to 2048x1152 HEVC on your PC with the pc_clip_baker tool, then copy them into assets/clips_hevc/ and run this again.\n\n(Starting anyway — the screen will be black until clips are added.)"
fi

ARGS=( --hevc --width "$RENDER_WIDTH" --height "$RENDER_HEIGHT" \
       --fullscreen --gpu-scale \
       --control --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )
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
