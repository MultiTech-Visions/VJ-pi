#!/bin/bash
# pi-paint VJ — dual display launcher (main use case).
# Double-click in the file manager and choose "Execute".
#
# Control HUD     → display 0 (your small screen) by default
# Projector output → display 1 (fullscreen) by default on first launch
#
# After the first launch you can pick the output display from the
# Control HUD (F11 to cycle, F12 to apply, or click the buttons). That
# choice is saved to vj_state.json and reused on every subsequent
# launch — so this script's OUTPUT_DISPLAY only matters until you've
# applied a pick once. To force a different display on the next launch,
# either re-apply via the HUD or delete vj_state.json.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

# Render canvas resolution. Leave empty to use config.py's default (1280x720).
# If you override this, run Process Assets.sh again so clips play at canvas
# res (no per-frame resize).
# GPU scaling (below) presents the canvas to the projector either way.
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
date '+[VJ] launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup needed" \
    "Setup hasn't been run yet.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

# Only pass --output-display when no saved choice exists; that way the
# HUD picker's persistent selection always wins. Uncomment the second
# block instead if you want this script to override the saved state.
ARGS=( --fullscreen --gpu-scale --control --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )
if [ -n "$RENDER_WIDTH" ] && [ -n "$RENDER_HEIGHT" ]; then
  ARGS+=( --width "$RENDER_WIDTH" --height "$RENDER_HEIGHT" )
fi
if [ ! -f vj_state.json ]; then
  ARGS+=( --output-display "$OUTPUT_DISPLAY" )
fi
# To force OUTPUT_DISPLAY every launch, comment the block above and
# uncomment this one:
# ARGS=( --fullscreen --output-display "$OUTPUT_DISPLAY" --control \
#        --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )

# ── projectM (MilkDrop) safety profile ───────────────────────────────
# Live projectM inside a mapping scene is the heaviest GPU path on this
# Pi's V3D — it can stall SDL present and wedge the graphics. This keeps
# PM in mapping ENABLED (VJ_PM_IN_MAPPING=1) but bounds its GPU cost:
# small render width, coarse mesh, ONE shared low frame budget divided
# across all visible PM boxes, throttled preset switches, and a present-
# stall watchdog that briefly cools PM if the display starts blocking.
# Without these, Start VJ.sh ran PM in mapping at the heaviest defaults
# (896px / 48x32 / 30fps) — the documented crash path. All overridable.
export VJ_PM_IN_MAPPING="${VJ_PM_IN_MAPPING:-1}"
# Lowered render res to lift projectM fps: at 640x360 each MilkDrop frame
# was ~140-200ms on V3D (=5-7fps), which is the GPU shader cost, not the
# 18fps ceiling. 480x270 cuts the pixels ~44%; pm is upscaled/warped into
# boxes anyway so the resolution drop is invisible. Mesh coarsened too.
export VJ_PM_RENDER_MAX_W="${VJ_PM_RENDER_MAX_W:-480}"
export VJ_PM_MESH="${VJ_PM_MESH:-24x16}"
export VJ_PM_STREAM_FPS="${VJ_PM_STREAM_FPS:-18}"
export VJ_PM_COMPOSITE_FPS="${VJ_PM_COMPOSITE_FPS:-18}"
export VJ_PM_SWITCH_MS="${VJ_PM_SWITCH_MS:-550}"
export VJ_PM_PRESENT_STALL_MS="${VJ_PM_PRESENT_STALL_MS:-220}"
export VJ_PM_PRESENT_STALLS="${VJ_PM_PRESENT_STALLS:-2}"
export VJ_PM_SAFETY_COOLDOWN_S="${VJ_PM_SAFETY_COOLDOWN_S:-8}"

# Temporary: trace per-group mapping autopilot into vj_last_run.log so we
# can see whether the timer is firing. Set to 0 to silence.
export VJ_DEBUG_AUTOPILOT="${VJ_DEBUG_AUTOPILOT:-1}"

./venv/bin/python main.py "${ARGS[@]}" >>"$LOG" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -40 "$LOG" 2>/dev/null)
  show_error "VJ-pi crashed (exit $EXIT)" \
    "main.py exited with status $EXIT.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
