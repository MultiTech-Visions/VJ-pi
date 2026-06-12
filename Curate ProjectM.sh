#!/bin/bash
# pi-paint VJ — projectM (MilkDrop) curation.
#
# Double-click and choose "Execute". There are ~8000 presets installed and
# most are near-black duds. This scores a pool of them OFFSCREEN for actual
# visual activity (brightness + motion) and writes the liveliest ones to
# projectm_playlist.txt, which the cycle then uses instead of the random
# sample. A progress bar shows while it runs; a summary pops up at the end,
# and the full report is saved to vj_last_projectm_curate.log.
#
# Pick "Software" for a run that can never freeze the display (slower), or
# "GPU" for a faster run. Re-run any time to re-curate. After it finishes,
# relaunch the show (Start VJ.sh) to pick up the new playlist.
#
# Tune with env vars if launched from a terminal: VJ_PM_CURATE_POOL (how many
# to score, default 1000), VJ_PM_CURATE_KEEP (how many to keep, default 70).

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_projectm_curate.log"
PY=./venv/bin/python
[ -x "$PY" ] || PY=python3

if command -v zenity >/dev/null 2>&1; then
    CHOICE=$(zenity --list --radiolist --width=560 --height=260 \
      --title="Curate ProjectM presets" \
      --text="Scores ~8000 presets offscreen and keeps the liveliest. Renderer:" \
      --column="" --column="Renderer" --column="Notes" \
      TRUE  "Software (safe)" "Cannot freeze the display. Slower (~30+ min)." \
      FALSE "GPU (fast)"      "Much faster (~5-10 min). Tiny freeze risk." \
      --hide-column=2 2>/dev/null)
    case "$CHOICE" in
        Software*) export VJ_PM_SOFTWARE=1 ;;
        GPU*)      : ;;
        *)         exit 0 ;;   # cancelled
    esac
fi

date '+[VJ] projectM curation start: %Y-%m-%d %H:%M:%S' > "$LOG"
echo "[curate] renderer: ${VJ_PM_SOFTWARE:+software}" >> "$LOG"
echo "[curate] renderer: ${VJ_PM_SOFTWARE:-gpu/v3d}"   >> "$LOG"

if command -v zenity >/dev/null 2>&1; then
    "$PY" projectm_curate.py 2>> "$LOG" \
      | zenity --progress --title="Curating presets" \
          --text="Scoring MilkDrop presets…" --percentage=0 \
          --auto-close --width=580 2>/dev/null
    EXIT=${PIPESTATUS[0]}
    zenity --info --width=760 --title="ProjectM curation done" \
      --text="$(tail -22 "$LOG")" 2>/dev/null
else
    "$PY" projectm_curate.py 2>> "$LOG"
    EXIT=$?
    tail -22 "$LOG"
fi
exit "${EXIT:-0}"
