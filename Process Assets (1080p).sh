#!/bin/bash
# pi-paint VJ — re-process clips to 1080p for a "sweet spot" performance
# test, WITHOUT disturbing the pristine originals.
#
# How this differs from "Process Assets.sh" (the standard processor):
#   • It reads its SOURCES only from assets/clips/_originals/ — the pristine
#     vault that the standard processor populated. It writes the 1080p .mp4
#     results into assets/clips/.
#   • It NEVER moves, deletes, or overwrites anything in _originals. The
#     vault is treated as read-only, so your true originals can't be harmed
#     and you always downscale from the pristine source (never upscale from
#     an already-720p clip).
#
# Workflow:
#   1. Run "Process Assets.sh" once first (it builds _originals/).
#   2. Run this to swap the clips library to 1080p and test framerate.
#   3. Run "Process Assets.sh" again any time to go back to the config'd
#      resolution (it, too, re-derives from _originals safely).
#
# Target defaults to 1920x1080; override with VJ_TARGET_W / VJ_TARGET_H.

set -uo pipefail
cd "$(dirname "$0")"

LOG="$(pwd)/vj_last_process.log"
TARGET_W="${VJ_TARGET_W:-1920}"
TARGET_H="${VJ_TARGET_H:-1080}"
PRESET="${VJ_FFMPEG_PRESET:-medium}"
CRF="${VJ_FFMPEG_CRF:-22}"
CLIPS_DIR="assets/clips"
ORIG_DIR="assets/clips/_originals"
PROGRESS_FD=""
ZENITY_PID=""

show_error() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
    return
  fi
  printf '%s\n\n%s\n' "$title" "$body" >&2
}

show_info() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --width=720 --title="$title" --text="$body" 2>/dev/null
    return
  fi
  printf '%s\n\n%s\n' "$title" "$body"
}

log() {
  printf '%s\n' "$*"
  printf '%s\n' "$*" >>"$LOG"
}

progress() {
  local pct="$1"
  local msg="$2"
  log "[$pct%] $msg"
  if [ -n "$PROGRESS_FD" ]; then
    printf '%s\n# %s\n' "$pct" "$msg" >&"$PROGRESS_FD" 2>/dev/null || true
  fi
}

cleanup() {
  if [ -n "$PROGRESS_FD" ]; then
    exec {PROGRESS_FD}>&- 2>/dev/null || true
  fi
  if [ -n "$ZENITY_PID" ]; then
    wait "$ZENITY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_gui_progress() {
  if ! command -v zenity >/dev/null 2>&1; then
    return
  fi
  local pipe
  pipe=$(mktemp -u /tmp/vj-process-progress.XXXXXX)
  mkfifo "$pipe" || return
  zenity --progress \
    --title="VJ-pi — Process Assets (1080p)" \
    --text="Starting..." \
    --percentage=0 \
    --width=560 \
    --auto-close \
    --no-cancel <"$pipe" 2>/dev/null &
  ZENITY_PID=$!
  exec {PROGRESS_FD}>"$pipe"
  rm -f "$pipe"
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    show_error "Missing tool: $1" \
      "The tool '$1' is not installed.\n\nRun setup.sh first.\n\nLog: $LOG"
    exit 1
  fi
}

probe_field() {
  ffprobe -v error -select_streams v:0 -show_entries "stream=$2" \
    -of csv=p=0 "$1" 2>/dev/null | head -1
}

has_audio() {
  ffprobe -v error -select_streams a -show_entries stream=index \
    -of csv=p=0 "$1" 2>/dev/null | grep -q .
}

duration_us() {
  local d
  d=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null)
  awk -v d="$d" 'BEGIN { if (d > 0) printf "%d", d * 1000000; else print 0 }'
}

# Process one PRISTINE source from _originals into a 1080p mp4 in clips/.
# Reads only from _originals; writes only into clips/. _originals untouched.
process_file() {
  local src="$1"
  local idx="$2"
  local total="$3"
  local base stem out_file tmp_out vf total_us
  base=$(basename "$src")
  stem="${base%.*}"
  out_file="$CLIPS_DIR/$stem.mp4"
  tmp_out="$CLIPS_DIR/.processing.$stem.$$.mp4"

  progress $(( (idx - 1) * 100 / total )) "[$idx/$total] Inspecting $base"

  # Skip if the clips/ output is already this resolution (fast re-runs).
  if [ -f "$out_file" ]; then
    local ocodec ow oh oaud
    ocodec=$(probe_field "$out_file" codec_name)
    ow=$(probe_field "$out_file" width)
    oh=$(probe_field "$out_file" height)
    if has_audio "$out_file"; then oaud=1; else oaud=0; fi
    if [ "$ocodec" = "h264" ] && [ "$ow" = "$TARGET_W" ] \
       && [ "$oh" = "$TARGET_H" ] && [ "$oaud" = "0" ]; then
      log "  skip: $out_file already ${TARGET_W}x${TARGET_H}"
      return 0
    fi
  fi

  vf="scale=${TARGET_W}:${TARGET_H}:force_original_aspect_ratio=increase,crop=${TARGET_W}:${TARGET_H}"
  total_us=$(duration_us "$src")

  log "  transcode (from vault): preset=$PRESET crf=$CRF -> $out_file"
  {
    echo "[VJ] === ffmpeg (1080p) $src ==="
    ffmpeg -y -hide_banner -nostdin \
      -i "$src" \
      -vf "$vf" \
      -c:v libx264 -preset "$PRESET" -crf "$CRF" \
      -pix_fmt yuv420p -movflags +faststart -an \
      -progress pipe:1 -stats_period 1 \
      "$tmp_out" 2>&1
    echo "[VJ] === ffmpeg exit ${PIPESTATUS[0]} ==="
  } | while IFS= read -r line; do
    printf '%s\n' "$line" >>"$LOG"
    case "$line" in
      out_time_ms=*)
        if [ "$total_us" -gt 0 ]; then
          local out_us file_pct overall_pct
          out_us=${line#out_time_ms=}
          file_pct=$(( out_us * 100 / total_us ))
          [ "$file_pct" -gt 100 ] && file_pct=100
          overall_pct=$(( ((idx - 1) * 100 + file_pct) / total ))
          progress "$overall_pct" "[$idx/$total] Transcoding $base (${file_pct}%)"
        fi
        ;;
      progress=end)
        progress $(( idx * 100 / total )) "[$idx/$total] Finished ffmpeg for $base"
        ;;
    esac
  done

  if [ ! -s "$tmp_out" ]; then
    log "  ERROR: empty output; leaving $out_file as-is"
    rm -f "$tmp_out"
    return 1
  fi

  # Only ever write into clips/ — _originals is never touched.
  mv -f "$tmp_out" "$out_file"
  log "  ok: wrote $out_file (source preserved in $ORIG_DIR/$base)"
}

require_tool ffmpeg
require_tool ffprobe

: >"$LOG"
date '+[VJ] process(1080p) start: %Y-%m-%d %H:%M:%S' >>"$LOG"

if [ ! -d "$ORIG_DIR" ]; then
  show_error "No originals to process" \
    "$ORIG_DIR does not exist yet.\n\nRun \"Process Assets.sh\" once first — it\nbuilds the pristine originals vault that this\n1080p processor reads from.\n\nLog: $LOG"
  exit 1
fi

start_gui_progress

log "============================================================"
log "  pi-paint VJ — Asset Processor (1080p sweet-spot test)"
log "============================================================"
log "Target: ${TARGET_W}x${TARGET_H} H.264 mp4, no audio"
log "Source: $ORIG_DIR (read-only vault)   ->   $CLIPS_DIR"
log "Preset: $PRESET   CRF: $CRF"
log "Log: $LOG"

# Clean only our own partial outputs from killed runs.
find "$CLIPS_DIR" -maxdepth 1 -type f -name '.processing.*.mp4' -delete 2>/dev/null || true

mapfile -d '' FILES < <(
  find "$ORIG_DIR" -maxdepth 1 -type f \
    \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' \
    -o -iname '*.webm' -o -iname '*.avi' -o -iname '*.gif' \
    -o -iname '*.m4v' -o -iname '*.wmv' -o -iname '*.flv' \) \
    -print0 | sort -z
)

TOTAL=${#FILES[@]}
if [ "$TOTAL" -eq 0 ]; then
  progress 100 "No originals found in $ORIG_DIR"
  show_info "Process Assets (1080p)" \
    "No source videos found in $ORIG_DIR.\n\nRun \"Process Assets.sh\" first to populate the\noriginals vault, then re-run this.\n\nLog: $LOG"
  exit 0
fi

log "Found $TOTAL original(s) in the vault"
errors=0
for i in "${!FILES[@]}"; do
  if ! process_file "${FILES[$i]}" "$((i + 1))" "$TOTAL"; then
    errors=$((errors + 1))
  fi
done

progress 100 "Done. Errors: $errors"
date '+[VJ] process(1080p) end: %Y-%m-%d %H:%M:%S' >>"$LOG"
show_info "Process Assets (1080p) — done" \
  "Re-encoded $TOTAL clip(s) to ${TARGET_W}x${TARGET_H} into $CLIPS_DIR.\nErrors: $errors\n\nOriginals in $ORIG_DIR were NOT touched.\n\nTo go back to the standard resolution, run\n\"Process Assets.sh\".\n\nFull log: $LOG"
