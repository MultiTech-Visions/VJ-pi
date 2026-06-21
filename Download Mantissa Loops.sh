#!/bin/bash
# Download the free Mantissa VJ loop library (CC0) into assets/clips/.
# Double-click in the file manager and choose "Execute".
#
# Source: https://mantissa.xyz/vj.html — the loops live at
#   https://ftp.mantissa.xyz/vj_loops/mantissa.xyz_loop_NNN.mp4   (NNN = 001..127)
# These are H.264 .mp4s, i.e. the format the regular "Start VJ.sh" plays,
# so they drop straight into assets/clips/ with no processing.
#
# Re-runnable: it scans every loop you already have ANYWHERE under assets/
# (clips, clips_hevc, _originals, …) and skips those, and it resumes a
# partial download instead of starting over. Run it again any time to pick
# up loops added later or finish an interrupted batch.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_download.log"

BASE_URL="https://ftp.mantissa.xyz/vj_loops/"
PREFIX="mantissa.xyz_loop_"
FIRST=1
LAST=127
DEST="assets/clips"

show_info() {  # title, body, [--error]
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
TOTAL=${#TODO[@]}
echo "[download] missing: $TOTAL loop(s)" >>"$LOG"

if [ "$TOTAL" -eq 0 ]; then
  show_info "Mantissa loops" \
    "You already have every loop ($FIRST–$LAST).\n\nNothing to download.\n\nLog: $LOG"
  exit 0
fi

# RESULT file carries counts back out of the progress subshell.
RESULT=$(mktemp)
echo "0 0" >"$RESULT"   # ok failed

run() {   # emits zenity --progress protocol on stdout; detail goes to $LOG
  local done=0 ok=0 failed=0
  for num in "${TODO[@]}"; do
    local name="${PREFIX}${num}.mp4"
    local url="${BASE_URL}${name}"
    local part="$DEST/.${name}.part"
    echo "# Downloading loop $num  ($((done+1)) of $TOTAL)…"
    echo "[download] GET $url" >>"$LOG"
    if curl -fL --retry 3 --retry-delay 2 -C - -o "$part" "$url" >>"$LOG" 2>&1; then
      mv -f "$part" "$DEST/$name"
      ok=$((ok+1))
      echo "[download]   OK -> $DEST/$name" >>"$LOG"
    else
      failed=$((failed+1))
      rm -f "$part"
      echo "[download]   FAILED $name" >>"$LOG"
    fi
    done=$((done+1))
    echo $(( done * 100 / TOTAL ))
  done
  echo "$ok $failed" >"$RESULT"
}

if command -v zenity >/dev/null 2>&1; then
  run | zenity --progress --title="Downloading Mantissa loops" \
        --width=520 --text="Starting…" --percentage=0 --auto-close 2>/dev/null
  # If the user hit Cancel, zenity closes the pipe; partials are .part and
  # resume next run, so nothing is corrupted.
else
  run
fi

read -r OK FAILED <"$RESULT"; rm -f "$RESULT"
date '+[download] done: %Y-%m-%d %H:%M:%S' >>"$LOG"
echo "[download] downloaded=$OK failed=$FAILED of $TOTAL" >>"$LOG"

BODY="Downloaded: $OK\nFailed: $FAILED\nRequested this run: $TOTAL\n\nSaved to: $DEST/\n\nThey'll appear in Start VJ.sh on the next launch.\nRun this again any time to retry failures or grab new loops.\n\nLog: $LOG"
if [ "$FAILED" -gt 0 ]; then
  show_info "Mantissa loops — finished with $FAILED failure(s)" "$BODY" --warning
else
  show_info "Mantissa loops — done" "$BODY"
fi
