#!/bin/bash
# One-shot: find the app-id/title of the GPU compositor's video window, so
# we can write a labwc rule that fullscreens it on the projector.
# Double-click -> "Execute in Terminal" (it may ask for your password to
# install the small 'lswt' tool).

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_windowid.log"

{
  # Make sure we have a tool that can list open Wayland windows.
  TOOL=""
  for t in lswt wlrctl; do
    command -v "$t" >/dev/null 2>&1 && TOOL="$t" && break
  done
  if [ -z "$TOOL" ]; then
    echo "[winid] installing lswt ..."
    sudo apt-get install -y lswt && TOOL=lswt
  fi
  if [ -z "$TOOL" ]; then
    echo "[winid] couldn't get lswt; trying: sudo apt-get install -y wlrctl"
    sudo apt-get install -y wlrctl && TOOL=wlrctl
  fi
  if [ -z "$TOOL" ]; then
    echo "[winid] no window-list tool available. Tell Sam."
    exit 1
  fi
  echo "[winid] using $TOOL"

  echo "[winid] launching compositor (test clip) in background ..."
  /usr/bin/python3 gpu_compositor.py tests >/dev/null 2>&1 &
  CP=$!
  sleep 6

  echo "=================== OPEN WINDOWS ==================="
  if [ "$TOOL" = "lswt" ]; then
    lswt 2>&1
  else
    wlrctl toplevel list 2>&1
  fi
  echo "==================================================="

  kill "$CP" 2>/dev/null
  wait "$CP" 2>/dev/null
  echo "[winid] done — look for the entry that's the video window"
  echo "        (note its app-id / app_id and title) and send it to Sam."
} 2>&1 | tee "$LOG"

read -p "Press Enter to close..."
