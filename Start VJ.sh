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
ARGS=( --fullscreen --control --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )
if [ ! -f vj_state.json ]; then
  ARGS+=( --output-display "$OUTPUT_DISPLAY" )
fi
# To force OUTPUT_DISPLAY every launch, comment the block above and
# uncomment this one:
# ARGS=( --fullscreen --output-display "$OUTPUT_DISPLAY" --control \
#        --control-display "$CONTROL_DISPLAY" --control-size "$CONTROL_SIZE" )

./venv/bin/python main.py "${ARGS[@]}" >>"$LOG" 2>&1
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -40 "$LOG" 2>/dev/null)
  show_error "VJ-pi crashed (exit $EXIT)" \
    "main.py exited with status $EXIT.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
