#!/bin/bash
# Spike C — GPU projection-mapping warp on the 4K GL texture.
# Double-click in the file manager and choose "Execute in Terminal".
#
# Decodes the 4K HEVC clip and WARPS it on the GPU (perspective/keystone),
# then measures fps. If this holds 30, mapping can be a GPU pass and the
# whole rig can go GPU-first — no separate cinematic mode needed.
#
# Needs tests/4k_hevc_test.mp4 (run "Make 4K Test Clip.sh" first).

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_spike_c.log"

# Pure GStreamer — system python3 (the one with gi), like the GPU worker.
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
date '+[spike-c] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"

"$PY" tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 --mode warp 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

HEAD=$(grep -E 'RESULT' "$LOG" 2>/dev/null)
TAIL=$(tail -20 "$LOG" 2>/dev/null)
if [ "$EXIT" -eq 0 ]; then
  show_dialog info "Spike C finished — send Sam this" \
    "GPU MAPPING WARP @ 4K:\n\n$HEAD\n\n---\nFull tail:\n$TAIL\n\nDid the warped video look smooth on screen?"
else
  show_dialog error "Spike C errored (exit $EXIT)" \
    "Log: $LOG\n\nLast lines:\n\n$TAIL"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
