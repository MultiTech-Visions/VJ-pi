#!/bin/bash
# One-time setup for the Display Brightness tool.
#
# Double-click and choose "Execute in Terminal" (it needs your password for
# 'sudo apt install'). It installs python3-tk, the small graphics toolkit
# the brightness window uses. Safe to run more than once — it skips the
# install if Tk is already there.
#
# Most Pi desktops already have python3-tk (Thonny uses it), so you may
# never need this — "Display Brightness.sh" will tell you if you do.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_brightness_setup.log"

show_msg() {
  local kind="$1" title="$2" body="$3"
  if command -v zenity >/dev/null 2>&1; then
    zenity --"$kind" --width=640 --title="$title" --text="$body" 2>/dev/null
  elif command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
  else
    echo "$title"; echo "$body"
  fi
}

PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3

: >"$LOG"
date '+[brightness-setup] start: %Y-%m-%d %H:%M:%S' >>"$LOG"

if "$PY" -c "import tkinter" >/dev/null 2>&1; then
  show_msg info "Display Brightness: already set up" \
    "python3-tk is already installed. You can run \"Display Brightness.sh\" now."
  exit 0
fi

echo "Installing python3-tk (you'll be asked for your password)..."
echo "Log: $LOG"
sudo apt-get update 2>&1 | tee -a "$LOG"
sudo apt-get install -y python3-tk 2>&1 | tee -a "$LOG"

if "$PY" -c "import tkinter" >/dev/null 2>&1; then
  show_msg info "Display Brightness: ready" \
    "python3-tk is installed. Double-click \"Display Brightness.sh\" to open the brightness window."
else
  TAIL=$(tail -25 "$LOG" 2>/dev/null)
  show_msg error "Display Brightness: install failed" \
    "Couldn't install python3-tk.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
  read -p "Press Enter to close..."
  exit 1
fi
read -p "Press Enter to close..."
exit 0
