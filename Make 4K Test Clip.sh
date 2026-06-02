#!/bin/bash
# Make a 4K HEVC test clip for the 4K decode spike.
# Double-click in the file manager and choose "Execute in Terminal".
#
# One-time, software encode — takes a couple of minutes. Writes
# tests/4k_hevc_test.mp4. Run this BEFORE "Run Spike B (4K test).sh".

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_make4k.log"

show_dialog() {  # $1=type(error|info) $2=title $3=body
  if command -v zenity >/dev/null 2>&1; then
    zenity --"$1" --width=720 --title="$2" --text="$3" 2>/dev/null; return
  fi
  printf '%s\n\n%s\n' "$2" "$3"
}

: >"$LOG"
date '+[make4k] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"

bash tests/make_4k_test_clip.sh 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

if [ "$EXIT" -eq 0 ] && [ -f tests/4k_hevc_test.mp4 ]; then
  SIZE=$(ls -lh tests/4k_hevc_test.mp4 | awk '{print $5}')
  show_dialog info "4K test clip ready" \
    "Made tests/4k_hevc_test.mp4 ($SIZE).\n\nNext: double-click 'Run Spike B (4K test).sh'."
else
  TAIL=$(tail -30 "$LOG" 2>/dev/null)
  show_dialog error "Couldn't make the 4K clip (exit $EXIT)" \
    "Log: $LOG\n\nLast lines:\n\n$TAIL"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
