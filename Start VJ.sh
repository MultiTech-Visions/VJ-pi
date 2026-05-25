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
# tail of the log so the operator can see what actually happened.

OUTPUT_DISPLAY=1
CONTROL_DISPLAY=0
CONTROL_SIZE="680x720"

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_run.log"

show_error() {
  # Short GUI error popup. --no-markup so any `<`/`&` in the body
  # don't trip zenity's Pango parser (which silently falls back to
  # "An error has occurred." when markup parsing fails).
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --no-markup --text="$body" 2>/dev/null
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

show_log_dialog() {
  # Scrollable log viewer. Uses zenity --text-info which feeds the
  # file straight through — no Pango parsing, so tracebacks with
  # `<module>` / `<frame>` tags render correctly instead of getting
  # eaten and replaced with "An error has occurred".
  local title="$1"
  local logfile="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --text-info --title="$title" --filename="$logfile" \
      --width=900 --height=600 --no-wrap 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    xmessage -file "$logfile" -title "$title" 2>/dev/null
    return
  fi
  for term in lxterminal xterm gnome-terminal mate-terminal x-terminal-emulator; do
    if command -v "$term" >/dev/null 2>&1; then
      "$term" -e bash -c "less '$logfile'; read -p 'Press Enter to close...'"
      return
    fi
  done
}

if [ ! -d "venv" ]; then
  show_error "VJ-pi: setup missing" \
    "The Python virtualenv ./venv/ doesn't exist.\n\nDouble-click setup.sh first."
  exit 1
fi

# Pre-flight: every release has the chance to add a new dependency
# (moderngl in the GPU rebuild was a recent one). If the venv can't
# import the essentials, auto-run pip install -r requirements.txt
# before launching so the operator doesn't get a ModuleNotFoundError
# flash that vanishes. Logged so we can see what happened if pip
# itself fails.
: >"$LOG"
if ! ./venv/bin/python -c "import pygame, moderngl, numpy, cv2" >>"$LOG" 2>&1; then
  echo "[VJ] missing python deps — running pip install -r requirements.txt..." >>"$LOG"
  if ! ./venv/bin/pip install -r requirements.txt >>"$LOG" 2>&1; then
    show_log_dialog "VJ-pi: dependency install failed — $LOG" "$LOG"
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
  show_log_dialog "VJ-pi crashed (exit $EXIT) — $LOG" "$LOG"
fi
exit "$EXIT"
