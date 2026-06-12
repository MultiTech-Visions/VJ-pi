#!/bin/bash
# pi-paint VJ — projectM (MilkDrop) diagnostic.
#
# Double-click this file in the file manager and choose "Execute" (it does
# NOT need a terminal or a password). It renders a sample of the installed
# MilkDrop presets OFFSCREEN — no projector, no HUD, no live display — one
# at a time and slowly, so it can't trigger the rapid-cycle freeze. When it
# finishes (about a minute) a dialog pops up with the result, and the full
# report is saved to vj_last_projectm_diag.log.
#
# Use this to find out whether the new visualizers actually render on this
# Pi's GPU, and which presets work, without risking the live show.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_projectm_diag.log"

if [ "${VJ_PM_DIAG_TEED:-0}" = "0" ]; then
    export VJ_PM_DIAG_TEED=1
    bash "$0" "$@" 2>&1 | tee "$LOG"
    EXIT=${PIPESTATUS[0]}
    if command -v zenity >/dev/null 2>&1; then
        if [ "$EXIT" -eq 0 ]; then
            zenity --info --width=760 --title="ProjectM diagnostic" \
              --text="Diagnostic finished.\n\nFull report: $LOG\n\nResult:\n\n$(grep -E '^\[diag\] (summary|VERDICT)' "$LOG" | sed 's/\[diag\] //')" 2>/dev/null
        else
            zenity --error --width=760 --title="ProjectM diagnostic failed (exit $EXIT)" \
              --text="It did not finish.\n\nFull log: $LOG\n\nLast lines:\n\n$(tail -20 "$LOG")" 2>/dev/null
        fi
    fi
    exit "$EXIT"
fi

date '+[VJ] projectM diagnostic start: %Y-%m-%d %H:%M:%S'
PY=./venv/bin/python
[ -x "$PY" ] || PY=python3
echo "[diag] python: $PY"
exec "$PY" projectm_diag.py
