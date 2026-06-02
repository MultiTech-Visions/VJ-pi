#!/bin/bash
# Play 4K HEVC clips fullscreen on the projector. Double-click -> "Execute
# in Terminal" (interactive: type n / p / q + Enter in this window).
#
# Separate from the VJ app — cannot affect it. Clips must be H.265/HEVC.
# Run "Apply Fullscreen Rule.sh" once so it lands fullscreen on the projector.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_play4k.log"

PY=/usr/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3

# Default to assets/clips; pass a folder of 4K HEVC clips as $1 to override.
DIR="${1:-assets/clips}"

echo "[run] 4K player on: $DIR   (n=next  p=prev  q=quit)"
"$PY" play4k.py "$DIR" 2>&1 | tee "$LOG"
