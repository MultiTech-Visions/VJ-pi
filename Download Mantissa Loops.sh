#!/bin/bash
# Download the free Mantissa VJ loop library (CC0) into assets/clips/.
# Double-click in the file manager and choose "Execute".
#
# Source: https://mantissa.xyz/vj.html — the loops live at
#   https://ftp.mantissa.xyz/vj_loops/mantissa.xyz_loop_NNN.mp4   (NNN = 001..127)
# These are H.264 .mp4s, i.e. the format the regular "Start VJ.sh" plays,
# so they drop straight into assets/clips/ with no processing.
#
# Downloads run in PARALLEL. With `yad` installed you get one live progress
# bar per parallel slot plus an overall bar, in a single window. Set how
# many run at once with VJ_DL_JOBS (default 6). Without yad it falls back to
# a single zenity bar.
#
# Re-runnable: it scans every loop you already have ANYWHERE under assets/
# (clips, clips_hevc, _originals, …) and skips those, and it resumes a
# partial download instead of starting over. Cancel the window any time;
# half-finished files are left as .part and resume on the next run.

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

# ── shared state for the workers ─────────────────────────────────────────
WORK=$(mktemp -d)
QUEUE="$WORK/queue"; LOCK="$WORK/qlock"; CLOCK="$WORK/clock"
COUNT="$WORK/count"; OKF="$WORK/ok"; FAILF="$WORK/fail"
printf '%s\n' "${TODO[@]}" >"$QUEUE"
: >"$LOCK"; : >"$CLOCK"; echo 0 >"$COUNT"; echo 0 >"$OKF"; echo 0 >"$FAILF"
export QUEUE LOCK CLOCK COUNT OKF FAILF
[ "$JOBS" -gt "$TOTAL" ] && JOBS="$TOTAL"
export OVERALL=$((JOBS + 1))

# Pop the next loop number off the shared queue (flock-guarded).
pop() {
  exec 9>>"$LOCK"; flock 9
  local n; n=$(head -n1 "$QUEUE")
  [ -n "$n" ] && sed -i '1d' "$QUEUE"
  flock -u 9
  printf '%s' "$n"
}
export -f pop

# One worker = one progress bar (index $1). Pulls files off the queue until
# it's empty, streaming "bar:percent" / "bar:#text" lines to FD 3 (the yad
# multi-progress pipe). Percent comes from the .part size vs Content-Length.
worker() {
  local s="$1" num name part url clen sz pct res dn
  while :; do
    num=$(pop); [ -z "$num" ] && break
    name="${PREFIX}${num}.mp4"; part="$DEST/.${name}.part"; url="${BASE_URL}${name}"
    clen=$(curl -fsSI "$url" 2>>"$LOG" | awk 'tolower($1)=="content-length:"{print $2}' | tr -d '\r\n')
    echo "$s:0" >&3; echo "$s:#loop $num — starting…" >&3
    curl -fsS --retry 3 --retry-delay 2 -C - -o "$part" "$url" 2>>"$LOG" &
    local cpid=$!
    while kill -0 "$cpid" 2>/dev/null; do
      sz=$(stat -c%s "$part" 2>/dev/null || echo 0)
      if [ -n "$clen" ] && [ "$clen" -gt 0 ]; then
        pct=$(( sz * 100 / clen )); [ "$pct" -gt 99 ] && pct=99
        echo "$s:$pct" >&3
        echo "$s:#loop $num   $(( sz/1048576 ))/$(( clen/1048576 )) MB" >&3
      else
        echo "$s:#loop $num   $(( sz/1048576 )) MB" >&3
      fi
      sleep 0.4
    done
    if wait "$cpid"; then mv -f "$part" "$DEST/$name"; res=OK; else rm -f "$part"; res=FAIL; fi
    echo "$s:100" >&3
    # Tally + overall bar (flock-guarded).
    exec 8>>"$CLOCK"; flock 8
    dn=$(( $(cat "$COUNT") + 1 )); echo "$dn" >"$COUNT"
    if [ "$res" = OK ]; then echo $(( $(cat "$OKF") + 1 )) >"$OKF"
    else echo $(( $(cat "$FAILF") + 1 )) >"$FAILF"; fi
    flock -u 8
    echo "$OVERALL:$(( dn * 100 / TOTAL ))" >&3
    echo "$OVERALL:#Completed $dn of $TOTAL" >&3
    echo "[download]   $res $name" >>"$LOG"
  done
  echo "$s:100" >&3; echo "$s:#done" >&3
}
export -f worker

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

if command -v yad >/dev/null 2>&1; then
  # ── multi-bar GUI: one bar per slot + an overall bar ──────────────────
  PIPE="$WORK/pipe"; mkfifo "$PIPE"
  BARS=()
  for s in $(seq 1 "$JOBS"); do BARS+=( --bar="Slot $s":NORM ); done
  BARS+=( --bar="OVERALL  (0 of $TOTAL)":NORM )
  yad --multi-progress --title="Downloading Mantissa loops" \
      --text="Fetching $TOTAL loops, $JOBS at a time…" \
      --width=620 --auto-close --auto-kill --button="Cancel:1" \
      "${BARS[@]}" <"$PIPE" &
  YPID=$!
  exec 3>"$PIPE"     # unblocks yad's open; workers inherit FD 3
  WPIDS=()
  for s in $(seq 1 "$JOBS"); do worker "$s" & WPIDS+=("$!"); done
  wait "$YPID" 2>/dev/null     # returns when all bars hit 100% or user cancels
  # Stop any stragglers (covers the Cancel case).
  kill "${WPIDS[@]}" 2>/dev/null
  pkill -P $$ curl 2>/dev/null
  exec 3>&-
  rm -f "$DEST"/.${PREFIX}*.part 2>/dev/null
else
  # ── fallback: single zenity bar ───────────────────────────────────────
  exec 3>/dev/null
  run() {
    for s in $(seq 1 "$JOBS"); do worker "$s" & done
    wait
  }
  ( run ) &
  RPID=$!
  ( while kill -0 "$RPID" 2>/dev/null; do
      dn=$(cat "$COUNT" 2>/dev/null || echo 0)
      echo $(( dn * 100 / TOTAL )); echo "# Completed $dn of $TOTAL"
      sleep 0.5
    done; echo 100 ) \
    | zenity --progress --title="Downloading Mantissa loops ($JOBS at a time)" \
        --width=560 --text="Starting $TOTAL downloads…" --percentage=0 --auto-close 2>/dev/null
  kill "$RPID" 2>/dev/null; pkill -P $$ curl 2>/dev/null
  rm -f "$DEST"/.${PREFIX}*.part 2>/dev/null
fi

OK=$(cat "$OKF" 2>/dev/null || echo 0); FAIL=$(cat "$FAILF" 2>/dev/null || echo 0)
date '+[download] done: %Y-%m-%d %H:%M:%S' >>"$LOG"
echo "[download] downloaded=$OK failed=$FAIL of $TOTAL" >>"$LOG"

BODY="Downloaded: $OK\nFailed: $FAIL\nRequested this run: $TOTAL\n\nSaved to: $DEST/\n\nThey'll appear in Start VJ.sh on the next launch.\nRun this again any time to retry failures or grab new loops.\n\nLog: $LOG"
if [ "$FAIL" -gt 0 ]; then
  show_info "Mantissa loops — finished with $FAIL failure(s)" "$BODY" --warning
else
  show_info "Mantissa loops — done" "$BODY"
fi
