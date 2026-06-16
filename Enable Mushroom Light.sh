#!/bin/bash
# One-time setup for the BLE "mushroom" LED prop.
#
# The VJ venv doesn't include system packages, so the Bluetooth library
# (bleak) has to be installed into it once. Double-click this and choose
# "Execute". After it finishes, Start VJ.sh gains the mushroom feature:
# press P in the show to toggle control of the prop.
#
# Safe to run more than once (it just confirms bleak is present).

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_mushroom_setup.log"

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

: >"$LOG"
date '+[mushroom] setup start: %Y-%m-%d %H:%M:%S' >>"$LOG"

if [ ! -d "venv" ]; then
  show_msg error "Mushroom: setup needed" \
    "The VJ venv doesn't exist yet.\n\nDouble-click setup.sh first, then run this.\n\nLog: $LOG"
  exit 1
fi

echo "[mushroom] installing bleak into venv ..." >>"$LOG"
./venv/bin/python -m pip install "bleak>=0.22" >>"$LOG" 2>&1
RC=$?

if [ "$RC" -ne 0 ]; then
  TAIL=$(tail -25 "$LOG" 2>/dev/null)
  show_msg error "Mushroom: install failed (exit $RC)" \
    "Couldn't install bleak into the venv.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
  exit "$RC"
fi

# Verify it imports.
if ./venv/bin/python -c "import bleak" >>"$LOG" 2>&1; then
  show_msg info "Mushroom: ready" \
    "Bluetooth LED support is installed. \xf0\x9f\x8d\x84\n\nIn the show, press P to toggle mushroom control:\n  • ON  — the prop tracks the show's colour\n  • OFF — the prop runs its own built-in effect\n  • Blackout (Space) turns it off too.\n\nKeep the Pi within ~1 m of the prop's controller."
else
  show_msg error "Mushroom: verify failed" \
    "bleak installed but won't import. See log:\n\n$LOG"
  exit 1
fi
exit 0
