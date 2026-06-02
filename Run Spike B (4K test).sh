#!/bin/bash
# Spike B — 4K HEVC decode throughput test (THE decisive one).
# Double-click in the file manager and choose "Execute in Terminal".
#
# Needs tests/4k_hevc_test.mp4 first — if it's missing, double-click
# "Make 4K Test Clip.sh" once, then run this.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_spike_b.log"

# Spike B is pure GStreamer — run it with SYSTEM python3 (the one with
# `gi`), exactly like the GPU worker. The venv python doesn't have gi.
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
    "tests/4k_hevc_test.mp4 is missing.\n\nDouble-click 'Make 4K Test Clip.sh' first (one-time, a couple of minutes), then run this again."
  read -p "Press Enter to close..."; exit 1
fi

: >"$LOG"
date '+[spike-b] start: %Y-%m-%d %H:%M:%S' | tee -a "$LOG"

# 1) Pure decode (no CPU colour-convert) — the decoder's true ceiling.
echo "=== pure decode throughput ===" | tee -a "$LOG"
"$PY" tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 --mode decode 2>&1 | tee -a "$LOG"
EXIT=${PIPESTATUS[0]}

# 2) Production auto path: playbin3 picks the optimal decode->convert->sink
#    chain itself. Best single indicator of real-world 4K playback fps.
echo "=== on-screen 4K playback (playbin3 auto) ===" | tee -a "$LOG"
"$PY" tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 --mode playbin 2>&1 | tee -a "$LOG"

# 3) Explicit GPU vs CPU-convert sinks (~8s each), to see the fast path.
echo "=== on-screen 4K playback (explicit sink sweep) ===" | tee -a "$LOG"
"$PY" tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 --mode sweep 2>&1 | tee -a "$LOG"

# Pull out the lines that actually decide things.
HEAD=$(grep -E 'decoder plugged|RESULT' "$LOG" 2>/dev/null)
TAIL=$(tail -20 "$LOG" 2>/dev/null)
if [ "$EXIT" -eq 0 ]; then
  show_dialog info "Spike B finished — send Sam this" \
    "THE ANSWER:\n\n$HEAD\n\n---\nFull tail:\n$TAIL"
else
  show_dialog error "Spike B errored (exit $EXIT)" \
    "Log: $LOG\n\nLast lines:\n\n$TAIL"
fi
read -p "Press Enter to close this window..."
exit "$EXIT"
