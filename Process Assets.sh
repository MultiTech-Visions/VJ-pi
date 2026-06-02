#!/bin/bash
# pi-paint VJ — normalise ANY downloaded clip into something the Pi 5 can
# hardware-decode and the GPU rig (vj_gpu.py / play4k.py) can play:
#   H.265/HEVC, capped at 4K (3840x2160), no audio, MP4.
# Clips already in that form are remuxed (fast); everything else is
# transcoded to HEVC (slow on the Pi — see note). Originals are preserved.
#
# Double-click works: a zenity progress window is used when available.
# Terminal launch also works: the same progress is printed to stdout.
# Full ffmpeg output and high-level status go to vj_last_process.log.

set -uo pipefail
cd "$(dirname "$0")"

LOG="$(pwd)/vj_last_process.log"
MAX_W="${VJ_MAX_W:-3840}"     # cap output at 4K — downscale larger, never upscale
MAX_H="${VJ_MAX_H:-2160}"
PRESET="${VJ_FFMPEG_PRESET:-medium}"
CRF="${VJ_FFMPEG_CRF:-24}"    # HEVC CRF (~24 ≈ visually fine, reasonable size)
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
    --title="VJ-pi — Process Assets" \
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

process_file() {
  local f="$1"
  local idx="$2"
  local total="$3"
  local base stem dir codec width height audio ext_lower
  base=$(basename "$f")
  stem="${base%.*}"
  dir=$(dirname "$f")
  ext_lower=$(printf '%s' "${base##*.}" | tr '[:upper:]' '[:lower:]')

  progress $(( (idx - 1) * 100 / total )) "[$idx/$total] Inspecting $base"

  codec=$(probe_field "$f" codec_name)
  width=$(probe_field "$f" width)
  height=$(probe_field "$f" height)
  width=${width:-0}
  height=${height:-0}
  if has_audio "$f"; then audio=1; else audio=0; fi
  log "  codec=$codec size=${width}x${height} ext=$ext_lower audio=$audio"

  # Is it already HEVC, and does it already fit within the 4K cap?
  local is_hevc=0 fits=0
  [ "$codec" = "hevc" ] && is_hevc=1
  if [ "$width" -gt 0 ] && [ "$width" -le "$MAX_W" ] \
     && [ "$height" -gt 0 ] && [ "$height" -le "$MAX_H" ]; then
    fits=1
  fi

  # Already exactly what we want — leave it alone.
  if [ "$is_hevc" = 1 ] && [ "$fits" = 1 ] \
     && [ "$audio" = 0 ] && [ "$ext_lower" = "mp4" ]; then
    log "  skip: already HEVC <=4K mp4, no audio"
    return 0
  fi

  # HEVC + already small enough → just remux (fix container / strip audio).
  # No re-encode = fast. Otherwise transcode to HEVC (slow on the Pi).
  local mode
  if [ "$is_hevc" = 1 ] && [ "$fits" = 1 ]; then
    mode="remux"
  else
    mode="transcode"
  fi

  local backup_dir tmp_out out_file total_us
  backup_dir="$dir/_originals"
  tmp_out="$dir/.processing.$stem.$$.mp4"
  out_file="$dir/$stem.mp4"
  total_us=$(duration_us "$f")
  mkdir -p "$backup_dir"

  local -a vargs
  if [ "$mode" = "remux" ]; then
    log "  remux (no re-encode, fast) -> $out_file"
    vargs=( -c:v copy )
  else
    log "  transcode -> HEVC preset=$PRESET crf=$CRF (4K HEVC encode on the Pi is SLOW) -> $out_file"
    # Fit within MAX_WxMAX_H, preserve aspect, never upscale, even dims.
    vargs=( -vf "scale='min($MAX_W,iw)':'min($MAX_H,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
            -c:v libx265 -preset "$PRESET" -crf "$CRF" -pix_fmt yuv420p )
  fi

  {
    echo "[VJ] === ffmpeg ($mode) $f ==="
    ffmpeg -y -hide_banner -nostdin \
      -i "$f" \
      "${vargs[@]}" \
      -an -tag:v hvc1 -movflags +faststart \
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
          progress "$overall_pct" "[$idx/$total] ${mode} $base (${file_pct}%)"
        fi
        ;;
      progress=end)
        progress $(( idx * 100 / total )) "[$idx/$total] Finished $base"
        ;;
    esac
  done

  if [ ! -s "$tmp_out" ]; then
    log "  ERROR: empty output; leaving original in place"
    rm -f "$tmp_out"
    return 1
  fi

  mv -f "$f" "$backup_dir/$base"
  mv -f "$tmp_out" "$out_file"
  log "  ok: wrote $out_file ($mode); original -> $backup_dir/$base"
}

require_tool ffmpeg
require_tool ffprobe
if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx265'; then
  show_error "No HEVC encoder" \
    "Your ffmpeg has no libx265 (H.265) encoder, which this needs to make\nclips the Pi can hardware-decode.\n\nInstall a full ffmpeg:  sudo apt install ffmpeg\n\nLog: $LOG"
  exit 1
fi

: >"$LOG"
date '+[VJ] process start: %Y-%m-%d %H:%M:%S' >>"$LOG"
start_gui_progress

log "============================================================"
log "  pi-paint VJ — Asset Processor"
log "============================================================"
log "Target: H.265/HEVC mp4, <=${MAX_W}x${MAX_H}, no audio (Pi 5 hardware-decodable)"
log "Preset: $PRESET   CRF: $CRF"
log "Note: already-HEVC clips are remuxed (fast); others transcode to HEVC,"
log "      and 4K HEVC encoding on the Pi is SLOW (one-time per clip)."
log "Log: $LOG"

# Clean only our own partial outputs from killed runs.
find assets/clips -maxdepth 1 -type f -name '.processing.*.mp4' -delete 2>/dev/null || true

mapfile -d '' FILES < <(
  find assets/clips -maxdepth 1 -type f \
    \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' \
    -o -iname '*.webm' -o -iname '*.avi' -o -iname '*.gif' \
    -o -iname '*.m4v' -o -iname '*.wmv' -o -iname '*.flv' \
    -o -iname '*.ts' -o -iname '*.m2ts' -o -iname '*.mpg' \
    -o -iname '*.mpeg' -o -iname '*.3gp' -o -iname '*.ogv' \) \
    -not -name '.processing.*' \
    -print0 | sort -z
)

TOTAL=${#FILES[@]}
if [ "$TOTAL" -eq 0 ]; then
  progress 100 "No video files found in assets/clips"
  show_info "Process Assets" "No video files found in assets/clips.\n\nLog: $LOG"
  exit 0
fi

log "Found $TOTAL candidate file(s)"
errors=0
for i in "${!FILES[@]}"; do
  if ! process_file "${FILES[$i]}" "$((i + 1))" "$TOTAL"; then
    errors=$((errors + 1))
  fi
done

progress 100 "Done. Errors: $errors"
date '+[VJ] process end: %Y-%m-%d %H:%M:%S' >>"$LOG"
show_info "Process Assets — done" \
  "Processed $TOTAL file(s) -> H.265/HEVC mp4 (<=${MAX_W}x${MAX_H}, no audio).\nErrors: $errors\n\nOriginals are preserved in assets/clips/_originals/.\n\nFull log: $LOG"
