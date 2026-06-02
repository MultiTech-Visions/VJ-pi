#!/bin/bash
# Spike D — live external control of a GPU FX pass on the 4K clip.
# Double-click in the file manager and choose "Execute in Terminal".
#
# Plays the 4K clip through a GPU effect whose zoom + colour-split is
# driven LIVE from Python. If the picture pulses, live FX control works on
# the GStreamer GL path — so the rebuild's FX layer doesn't need a custom
# GL engine. Needs tests/4k_hevc_test.mp4 ("Make 4K Test Clip.sh" first).

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_spike_d.log"

PY=/usr/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3

show_dialog() {  # $1=error|info $2=title $3=body
  if command -v zenity >/dev/null 2>&1; then
    zenity --"$1" --width=760 --title="$2" --text="$3" 2>/dev/null; return
  fi
  printf '%s\n\n%s\n' "$2" "$3"
}

if [ ! -f tests/4k_hevc_test.mp4 ]; then
  show_dialog error "No 4K test clip yet" \
    "tests/4k_hevc_test.mp4 is missing.\n\nDouble-click 'Make 4K Test Clip.sh' first, then run this."
  read -p "Press Enter to close..."; exit 1
fi

: >"$LOG"
date '+[spike-d] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"

"$PY" tests/spike_d_gpu_control.py tests/4k_hevc_test.mp4 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

HEAD=$(grep -E 'RESULT|glshader properties' "$LOG" 2>/dev/null)
TAIL=$(tail -16 "$LOG" 2>/dev/null)
if [ "$EXIT" -eq 0 ]; then
  show_dialog info "Spike D finished — send Sam this" \
    "LIVE GPU CONTROL:\n\n$HEAD\n\nDid the picture PULSE / zoom with a colour-split? (that's the live control working)\n\n---\n$TAIL"
else
  show_dialog error "Spike D errored (exit $EXIT)" \
    "Log: $LOG\n\nLast lines:\n\n$TAIL"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
