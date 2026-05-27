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
#
# Failure reporting: when the file manager launches this with "Execute"
# (not "Execute in Terminal") there's no console for errors to go to,
# so a crash on startup would just disappear silently. We tee
# everything into vj_last_run.log and, if Python exits non-zero, pop
# a GUI dialog (zenity → xmessage → terminal fallback) showing the
# tail of the log. Tail the log live with:  tail -f vj_last_run.log

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_run.log"

show_error() {
  # Best-effort GUI error dialog: zenity → xmessage → any terminal
  # emulator we can find. Gives up silently if none are available
  # (the log file is still on disk for the operator to tail).
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

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup missing" \
    "The Python virtualenv ./venv/ doesn't exist.\n\nDouble-click setup.sh first."
  exit 1
fi

# Truncate the log up front so a fresh run is never mistaken for a
# stale one — every line below this point belongs to *this* invocation.
: >"$LOG"
date '+[VJ] launch start: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

# Pre-flight: every release has the chance to add a new dependency
# (moderngl was the most recent). If the venv can't import the
# essentials, auto-run pip install -r requirements.txt before
# launching so the operator doesn't get a ModuleNotFoundError flash
# that vanishes. Logged so we can see what happened if pip itself
# fails.
if ! ./venv/bin/python -c "import pygame, moderngl, numpy, cv2" >>"$LOG" 2>&1; then
  echo "[VJ] missing python deps — running pip install -r requirements.txt..." >>"$LOG"
  if ! ./venv/bin/pip install -r requirements.txt >>"$LOG" 2>&1; then
    show_error "VJ-pi: dependency install failed" \
      "pip install -r requirements.txt failed. Full log:\n\n$LOG\n\nLast lines:\n\n$(tail -30 "$LOG")"
    exit 1
  fi
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
    "main.py exited with status $EXIT.\n\nFull log:  $LOG\n\nLast lines:\n\n$TAIL"
fi
exit "$EXIT"
