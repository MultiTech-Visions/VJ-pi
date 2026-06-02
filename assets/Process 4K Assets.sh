#!/bin/bash
# Convert raw cinematic clips in assets/4k/ into Pi 5 GPU-playable files.
# Output goes to assets/4k/processed/ as HEVC/H.265 MP4, <=3840x2160,
# <=30fps, no audio. Raw source files are left in place.

set -uo pipefail
cd "$(dirname "$0")"

if [ -d "4k" ]; then
  ROOT_DIR="$(cd .. && pwd)"
elif [ -d "assets/4k" ]; then
  ROOT_DIR="$(pwd)"
else
  ROOT_DIR="$(pwd)"
fi

SRC_DIR="$ROOT_DIR/assets/4k"
OUT_DIR="$SRC_DIR/processed"
LOG="$ROOT_DIR/vj_last_4k_process.log"
MAX_W="${VJ_4K_MAX_W:-3840}"
MAX_H="${VJ_4K_MAX_H:-2160}"
MAX_FPS="${VJ_4K_MAX_FPS:-30}"
CRF="${VJ_4K_CRF:-22}"
PRESET="${VJ_4K_PRESET:-veryfast}"

show_error() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    if zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null; then
      return
    fi
  fi
  printf '%s\n\n%b\n' "$title" "$body" >&2
}

show_info() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    if zenity --info --width=720 --title="$title" --text="$body" 2>/dev/null; then
      return
    fi
  fi
  printf '%s\n\n%b\n' "$title" "$body"
}

log() {
  printf '%s\n' "$*"
  printf '%s\n' "$*" >>"$LOG"
}

need_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    show_error "Missing $1" \
      "Install FFmpeg first by running setup.sh, then run this processor again.\n\nLog: $LOG"
    exit 1
  fi
}

probe_field() {
  ffprobe -v error -select_streams v:0 -show_entries "stream=$2" \
    -of csv=p=0 "$1" 2>/dev/null | head -1
}

probe_fps() {
  local r
  r=$(probe_field "$1" avg_frame_rate)
  awk -F/ -v r="$r" 'BEGIN {
    if (split(r, a, "/") == 2 && a[2] > 0) printf "%.3f", a[1] / a[2];
    else print 0
  }'
}

choose_encoder() {
  if ffmpeg -hide_banner -encoders 2>/dev/null \
      | awk '$1 ~ /^V/ && $2 == "libx265" { found=1 } END { exit found ? 0 : 1 }'; then
    printf '%s\n' "libx265"
    return
  fi
  if ffmpeg -hide_banner -encoders 2>/dev/null \
      | awk '$1 ~ /^V/ && $2 == "hevc" { found=1 } END { exit found ? 0 : 1 }'; then
    printf '%s\n' "hevc"
    return
  fi
  return 1
}

is_playable_hevc() {
  local f="$1"
  local codec width height fps
  codec=$(probe_field "$f" codec_name)
  width=$(probe_field "$f" width)
  height=$(probe_field "$f" height)
  fps=$(probe_fps "$f")
  [ "$codec" = "hevc" ] || return 1
  [ "${width:-0}" -le "$MAX_W" ] || return 1
  [ "${height:-0}" -le "$MAX_H" ] || return 1
  awk -v fps="$fps" -v max="$MAX_FPS" 'BEGIN { exit fps <= max + 0.01 ? 0 : 1 }'
}

mkdir -p "$OUT_DIR"
: >"$LOG"
log "[4k-process] start $(date '+%Y-%m-%d %H:%M:%S')"
log "Input:  $SRC_DIR"
log "Output: $OUT_DIR"

need_tool ffmpeg
need_tool ffprobe
ENCODER=$(choose_encoder) || {
  show_error "No HEVC encoder" \
    "FFmpeg is installed, but it does not expose libx265 or another HEVC encoder.\n\nLog: $LOG"
  exit 1
}
log "Encoder: $ENCODER"

count=0
processed=0
skipped=0
failed=0

while IFS= read -r -d '' src; do
  base=$(basename "$src")
  case "$base" in
    .*|_*) continue ;;
  esac
  count=$((count + 1))
  stem="${base%.*}"
  out="$OUT_DIR/$stem.mp4"

  if [ -f "$out" ] && is_playable_hevc "$out"; then
    log "skip: $base -> processed file already exists"
    skipped=$((skipped + 1))
    continue
  fi

  log "process: $base -> processed/$stem.mp4"
  tmp="$out.tmp.mp4"
  rm -f "$tmp"
  if is_playable_hevc "$src" && [ "${src##*.}" = "mp4" ]; then
    ffmpeg -hide_banner -y -i "$src" -map 0:v:0 -an -c:v copy \
      -movflags +faststart "$tmp" >>"$LOG" 2>&1
  else
    vf="scale='min(${MAX_W},iw)':'min(${MAX_H},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=${MAX_FPS},format=yuv420p"
    ffmpeg -hide_banner -y -i "$src" -map 0:v:0 -an \
      -vf "$vf" -c:v "$ENCODER" -preset "$PRESET" -crf "$CRF" \
      -tag:v hvc1 -movflags +faststart "$tmp" >>"$LOG" 2>&1
  fi

  if [ -f "$tmp" ]; then
    mv -f "$tmp" "$out"
    processed=$((processed + 1))
  else
    log "FAILED: $base"
    failed=$((failed + 1))
  fi
done < <(
  find "$SRC_DIR" "$OUT_DIR" -maxdepth 1 -type f \( \
    -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.m4v' \
    -o -iname '*.webm' -o -iname '*.avi' \
  \) -print0 2>/dev/null | sort -z
)

log "Done. found=$count processed=$processed skipped=$skipped failed=$failed"
if [ "$count" -eq 0 ]; then
  show_info "No 4K clips found" \
    "Drop raw video files into assets/4k/, then run Process 4K Assets.sh again."
elif [ "$failed" -gt 0 ]; then
  show_error "Some 4K clips failed" \
    "Some clips did not process. See: $LOG"
else
  show_info "4K processing complete" \
    "Processed clips are ready in assets/4k/processed/.\n\nPress N inside Start VJ to enter cinematic mode."
fi
