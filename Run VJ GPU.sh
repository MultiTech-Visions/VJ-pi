#!/bin/bash
# GPU-native foundation: one 4K HEVC clip mapped onto N warped surfaces,
# all on the GPU. Double-click -> "Execute in Terminal" (interactive:
# type n / p / g / q + Enter). Clips must be H.265/HEVC. Run
# "Apply Fullscreen Rule.sh" once so it lands fullscreen on the projector.
cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_vjgpu.log"
PY=/usr/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3
DIR="${1:-assets/clips}"
echo "[run] vj_gpu on: $DIR   (n=next  p=prev  g=grid 1/9/16  q=quit)"
"$PY" vj_gpu.py "$DIR" 2>&1 | tee "$LOG"
