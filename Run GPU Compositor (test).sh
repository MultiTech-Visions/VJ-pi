#!/bin/bash
# GPU compositor — Stage 1 of the rebuild. Plays the clip library on the
# GPU at full res, loops, switches clips. Double-click → "Execute in
# Terminal" (it's interactive — you type commands in the terminal).
#
# In the terminal window, type + Enter:
#   n = next clip   p = previous   f 0.6 = FX amount (0..1)   q = quit
#
# Uses assets/clips if it has videos, else falls back to the tests/ folder
# (the synthetic 4K HEVC clip). HEVC clips get the hardware decoder; H.264
# plays too but software-decoded (slow at 4K).

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_compositor.log"

PY=/usr/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3

# The compositor is HEVC-only for now, and the guaranteed-HEVC clip is the
# synthetic one in tests/. Point there for this milestone test. Once you've
# got 4K HEVC versions of your library in assets/clips, change DIR to
# "assets/clips" and clip-switching (n/p) lights up across the library.
DIR="tests"
if ls assets/clips/*.hevc.mp4 assets/clips/*_hevc.mp4 >/dev/null 2>&1; then
  DIR="assets/clips"
fi

echo "[run] starting GPU compositor on: $DIR"
echo "[run] type in this window:  n=next  p=prev  f 0.6=FX  q=quit"
# stdin stays the terminal (interactive); stdout/stderr also tee to the log.
"$PY" gpu_compositor.py "$DIR" 2>&1 | tee "$LOG"
