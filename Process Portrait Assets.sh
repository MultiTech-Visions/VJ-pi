#!/bin/bash
# Convert portrait/vertical clips into 16:9 landscape clips.
# Drop source files into assets/portrait/. Output goes to
# assets/portrait/landscape/. The conversion preserves the full portrait
# frame in the center and fills the side space with a blurred copy.

set -uo pipefail
cd "$(dirname "$0")"

SRC_DIR="assets/portrait"
OUT_DIR="assets/portrait/landscape"
LOG="$(pwd)/vj_last_portrait_process.log"
TARGET_W="${VJ_PORTRAIT_W:-1920}"
TARGET_H="${VJ_PORTRAIT_H:-1080}"
FPS="${VJ_PORTRAIT_FPS:-30}"
CRF="${VJ_PORTRAIT_CRF:-22}"
PRESET="${VJ_PORTRAIT_PRESET:-veryfast}"

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

choose_encoder() {
  if ffmpeg -hide_banner -encoders 2>/dev/null \
      | awk '$1 ~ /^V/ && $2 == "libx264" { found=1 } END { exit found ? 0 : 1 }'; then
    printf '%s\n' "libx264"
    return
  fi
  if ffmpeg -hide_banner -encoders 2>/dev/null \
      | awk '$1 ~ /^V/ && $2 == "h264" { found=1 } END { exit found ? 0 : 1 }'; then
    printf '%s\n' "h264"
    return
  fi
  return 1
}

mkdir -p "$OUT_DIR"
: >"$LOG"
log "[portrait-process] start $(date '+%Y-%m-%d %H:%M:%S')"
log "Input:  $(pwd)/$SRC_DIR"
log "Output: $(pwd)/$OUT_DIR"
log "Target: ${TARGET_W}x${TARGET_H}, ${FPS}fps, H.264 MP4"

need_tool ffmpeg
ENCODER=$(choose_encoder) || {
  show_error "No H.264 encoder" \
    "FFmpeg is installed, but it does not expose libx264 or another H.264 encoder.\n\nLog: $LOG"
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
  out="$OUT_DIR/$stem-landscape.mp4"
  if [ -f "$out" ]; then
    log "skip: $base -> landscape output already exists"
    skipped=$((skipped + 1))
    continue
  fi

  log "process: $base -> portrait/landscape/$stem-landscape.mp4"
  tmp="$out.tmp.mp4"
  rm -f "$tmp"

  filter="[0:v]fps=${FPS},split=2[bg][fg];"
  filter+="[bg]scale=${TARGET_W}:${TARGET_H}:force_original_aspect_ratio=increase,"
  filter+="crop=${TARGET_W}:${TARGET_H},gblur=sigma=32,eq=brightness=-0.10:saturation=0.75[bg2];"
  filter+="[fg]scale=${TARGET_W}:${TARGET_H}:force_original_aspect_ratio=decrease[fg2];"
  filter+="[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p"

  if ffmpeg -hide_banner -y -i "$src" -map 0:v:0 -an \
      -filter_complex "$filter" \
      -c:v "$ENCODER" -preset "$PRESET" -crf "$CRF" \
      -movflags +faststart "$tmp" >>"$LOG" 2>&1; then
    mv -f "$tmp" "$out"
    processed=$((processed + 1))
  else
    rm -f "$tmp"
    log "FAILED: $base"
    failed=$((failed + 1))
  fi
done < <(
  find "$SRC_DIR" -maxdepth 1 -type f \( \
    -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.m4v' \
    -o -iname '*.webm' -o -iname '*.avi' \
  \) -print0 | sort -z
)

log "Done. found=$count processed=$processed skipped=$skipped failed=$failed"
if [ "$count" -eq 0 ]; then
  show_info "No portrait clips found" \
    "Drop portrait/vertical video files into assets/portrait/, then run Process Portrait Assets.sh again."
elif [ "$failed" -gt 0 ]; then
  show_error "Some portrait clips failed" \
    "Some clips did not process. See: $LOG"
else
  show_info "Portrait processing complete" \
    "Landscape outputs are ready in assets/portrait/landscape/.\n\nMove them into assets/clips/ for the regular VJ app, or assets/4k/ for cinematic processing."
fi
