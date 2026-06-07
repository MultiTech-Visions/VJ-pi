#!/bin/bash
# VJ-pi — show which USB cameras are detected.
# Double-click in the file manager and choose "Execute".
#
# Pops a dialog listing every /dev/videoN that actually delivers frames,
# so you can confirm the webcam is seen before a set. You normally don't
# need an index — the app auto-picks the first working camera when you
# press the "\" key. This is just for peace of mind / troubleshooting.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_cameras.log"

show_text() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    printf '%s' "$body" | zenity --text-info --width=560 --height=420 \
      --title="$title" 2>/dev/null
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
date '+[VJ] camera scan: %Y-%m-%d %H:%M:%S' >>"$LOG"

if [ ! -d "venv" ]; then
  show_text "VJ-pi: setup needed" \
    "Setup hasn't been run yet. Double-click setup.sh first."
  exit 1
fi

OUT=$(./venv/bin/python list_cameras.py 2>&1)
echo "$OUT" >>"$LOG"
show_text "VJ-pi — detected cameras" "$OUT"
