#!/bin/bash
# Download the free Mantissa VJ loop library (CC0) into assets/clips/.
# Double-click in the file manager and choose "Execute".
#
# Source: https://mantissa.xyz/vj.html — the loops live at
#   https://ftp.mantissa.xyz/vj_loops/mantissa.xyz_loop_NNN.mp4   (NNN = 001..127)
# These are H.264 .mp4s, i.e. the format the regular "Start VJ.sh" plays,
# so they drop straight into assets/clips/ with no processing.
#
# Downloads run in PARALLEL (several at once) for speed — set how many with
# VJ_DL_JOBS (default 6).
#
# Re-runnable: it scans every loop you already have ANYWHERE under assets/
# (clips, clips_hevc, _originals, …) and skips those, and it resumes a
# partial download instead of starting over. Cancel the progress bar any
# time; half-finished files are left as .part and resume on the next run.

cd "$(dirname "$0")"
export LOG="$(pwd)/vj_last_download.log"

export BASE_URL="https://ftp.mantissa.xyz/vj_loops/"
export PREFIX="mantissa.xyz_loop_"
FIRST=1
LAST=127
export DEST="assets/clips"
JOBS="${VJ_DL_JOBS:-6}"

show_info() {  # title, body, [kind]
  local title="$1" body="$2" kind="${3:---info}"
  if command -v zenity >/dev/null 2>&1; then
    zenity "$kind" --width=640 --title="$title" --text="$body" 2>/dev/null
  elif command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
  else
    echo "$title"; echo "$body"
  fi
}

# One download, run by xargs in parallel. Prints exactly one result line
# (OK/FAIL) to stdout so the progress counter can tick; detail goes to $LOG.
dl_one() {
  local num="$1"
  local name="${PREFIX}${num}.mp4"
  local part="$DEST/.${name}.part"
  if curl -fsS --retry 3 --retry-delay 2 -C - -o "$part" "${BASE_URL}${name}" 2>>"$LOG"; then
    mv -f "$part" "$DEST/$name"
    echo "[download]   OK $name" >>"$LOG"
    echo OK
  else
    rm -f "$part"
    echo "[download]   FAILED $name" >>"$LOG"
    echo FAIL
  fi
}
export -f dl_one

: >"$LOG"
date '+[download] start: %Y-%m-%d %H:%M:%S' >>"$LOG"
echo "[download] parallel jobs: $JOBS" >>"$LOG"
mkdir -p "$DEST"

# Loop numbers already present anywhere under assets/ (avoid duplicates).
HAVE=" $(find assets -iname "${PREFIX}*" -printf '%f\n' 2>/dev/null \
        | grep -oE 'loop_[0-9]{3}' | grep -oE '[0-9]{3}' | sort -u | tr '\n' ' ')"
echo "[download] already have:${HAVE}" >>"$LOG"

# Build the to-do list (numbers we don't have and whose file isn't there).
TODO=()
for i in $(seq "$FIRST" "$LAST"); do
  num=$(printf '%03d' "$i")
  case "$HAVE" in *" $num "*) continue ;; esac
  [ -f "$DEST/${PREFIX}${num}.mp4" ] && continue
  TODO+=("$num")
done
export TOTAL=${#TODO[@]}
echo "[download] missing: $TOTAL loop(s)" >>"$LOG"

if [ "$TOTAL" -eq 0 ]; then
  show_info "Mantissa loops" \
    "You already have every loop ($FIRST–$LAST).\n\nNothing to download.\n\nLog: $LOG"
  exit 0
fi

RESULT=$(mktemp)   # final "ok fail" counts, carried out of the progress subshell

# Fan the to-do list out to JOBS parallel curls. Each finished file prints
# one OK/FAIL line; the counter turns that into a running percentage for
# zenity and tallies the totals. Cancelling zenity breaks the pipe, which
# stops xargs (partial files stay as .part and resume next run).
run() {
  printf '%s\n' "${TODO[@]}" \
    | xargs -P "$JOBS" -I {} bash -c 'dl_one "$@"' _ {} \
    | {
        done=0; ok=0; fail=0
        while read -r tag; do
          [ "$tag" = OK ] && ok=$((ok+1)) || fail=$((fail+1))
          done=$((done+1))
          echo $(( done * 100 / TOTAL ))
          echo "# Completed $done of $TOTAL  (downloaded $ok, failed $fail)"
        done
        echo "$ok $fail" >"$RESULT"
      }
}

if command -v zenity >/dev/null 2>&1; then
  run | zenity --progress --title="Downloading Mantissa loops ($JOBS at a time)" \
        --width=560 --text="Starting $TOTAL downloads…" --percentage=0 --auto-close 2>/dev/null
else
  run >/dev/null
fi

read -r OK FAIL <"$RESULT" 2>/dev/null; rm -f "$RESULT"
OK=${OK:-0}; FAIL=${FAIL:-0}
date '+[download] done: %Y-%m-%d %H:%M:%S' >>"$LOG"
echo "[download] downloaded=$OK failed=$FAIL of $TOTAL" >>"$LOG"

BODY="Downloaded: $OK\nFailed: $FAIL\nRequested this run: $TOTAL\n\nSaved to: $DEST/\n\nThey'll appear in Start VJ.sh on the next launch.\nRun this again any time to retry failures or grab new loops.\n\nLog: $LOG"
if [ "$FAIL" -gt 0 ]; then
  show_info "Mantissa loops — finished with $FAIL failure(s)" "$BODY" --warning
else
  show_info "Mantissa loops — done" "$BODY"
fi
