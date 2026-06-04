#!/bin/bash
# pi-paint VJ — unified asset processor (HEVC, Pi-5 hardware-decode target).
#
# One double-click processes ALL three kinds of source media into the
# formats the app plays, skipping anything already done:
#
#   1. 2K clips     assets/clips/*           -> assets/clips_hevc/<name>.mp4
#                   (raw landscape video)       HEVC 2048x1152, the geometry the
#                                               Pi 5 GL decode path needs.
#
#   2. Portrait     assets/portrait/rotate/* -> assets/clips_hevc/<name>-landscape.mp4
#                   assets/portrait/crop/*       Three per-file modes, chosen by
#                   assets/portrait/*  (loose)   which subfolder you drop into:
#                     • rotate/  spin 90° so a sideways-shot clip fills the frame
#                     • crop/    fill 16:9 by cropping top & bottom (keep centre)
#                     • (loose)  blur-fill: whole frame centred, blurred sides
#
#   3. 4K cinematic assets/4k/*              -> assets/4k/processed/<name>.mp4
#                   (raw hi-res video)          HEVC <=3840x2160, for cinematic (N).
#
# Everything 2K/portrait is baked to EXACTLY 2048x1152 HEVC (hvc1/main/yuv420p)
# so it drops straight into assets/clips_hevc/ and plays via "Start VJ.sh".
#
# The Pi has NO hardware HEVC *encoder*, so this software-encodes — fine for a
# handful of field clips, but for a big library bake on a PC with the
# pc_clip_baker tool (way faster) and upload the finished clips instead.
#
# Scope to one kind with VJ_ONLY=clips|portrait|4k (the per-type launchers do
# this). Works double-clicked (zenity progress) or from a terminal.

set -uo pipefail
# This script lives in assets/ but operates on the repo root (paths below
# are like assets/clips, assets/clips_hevc, …), so step up one level.
cd "$(dirname "$0")/.."

LOG="$(pwd)/vj_last_process.log"
ONLY="${VJ_ONLY:-all}"

# 2K / portrait HEVC target — locked to the GL decode sweet spot. Don't change
# without re-baking everything (the player requires exactly this geometry).
HEVC_W=2048
HEVC_H=1152
FPS="${VJ_FPS:-30}"
CRF="${VJ_HEVC_CRF:-23}"
PRESET="${VJ_HEVC_PRESET:-fast}"     # libx265 speed/quality; "fast" suits the Pi

# 4K cinematic caps (mirror assets/Process 4K Assets.sh).
MAX4K_W="${VJ_4K_MAX_W:-3840}"
MAX4K_H="${VJ_4K_MAX_H:-2160}"
MAX4K_FPS="${VJ_4K_MAX_FPS:-30}"
CRF4K="${VJ_4K_CRF:-22}"
PRESET4K="${VJ_4K_PRESET:-veryfast}"

CLIPS_SRC="assets/clips"
HEVC_OUT="assets/clips_hevc"
P_ROTATE="assets/portrait/rotate"
P_CROP="assets/portrait/crop"
P_BLUR="assets/portrait"            # loose files at the top level
SRC4K="assets/4k"
OUT4K="assets/4k/processed"

PROGRESS_FD=""
ZENITY_PID=""

show_error() {
  local title="$1" body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null && return
  fi
  printf '%s\n\n%b\n' "$title" "$body" >&2
}
show_info() {
  local title="$1" body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --width=720 --title="$title" --text="$body" 2>/dev/null && return
  fi
  printf '%s\n\n%b\n' "$title" "$body"
}
log() { printf '%s\n' "$*"; printf '%s\n' "$*" >>"$LOG"; }
progress() {
  local pct="$1" msg="$2"
  log "[$pct%] $msg"
  [ -n "$PROGRESS_FD" ] && printf '%s\n# %s\n' "$pct" "$msg" >&"$PROGRESS_FD" 2>/dev/null || true
}
cleanup() {
  [ -n "$PROGRESS_FD" ] && { exec {PROGRESS_FD}>&- 2>/dev/null || true; }
  [ -n "$ZENITY_PID" ] && { wait "$ZENITY_PID" 2>/dev/null || true; }
}
trap cleanup EXIT

start_gui_progress() {
  command -v zenity >/dev/null 2>&1 || return
  local pipe
  pipe=$(mktemp -u /tmp/vj-processall.XXXXXX)
  mkfifo "$pipe" || return
  zenity --progress --title="VJ-pi — Process All Assets" \
    --text="Starting..." --percentage=0 --width=560 --auto-close --no-cancel \
    <"$pipe" 2>/dev/null &
  ZENITY_PID=$!
  exec {PROGRESS_FD}>"$pipe"
  rm -f "$pipe"
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 && return
  show_error "Missing tool: $1" "The tool '$1' is not installed.\n\nRun setup.sh first.\n\nLog: $LOG"
  exit 1
}
probe_field() {
  ffprobe -v error -select_streams v:0 -show_entries "stream=$2" \
    -of csv=p=0 "$1" 2>/dev/null | head -1
}
probe_fps() {
  local r; r=$(probe_field "$1" avg_frame_rate)
  awk -F/ -v r="$r" 'BEGIN { if (split(r,a,"/")==2 && a[2]>0) printf "%.3f", a[1]/a[2]; else print 0 }'
}
duration_us() {
  local d; d=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null)
  awk -v d="$d" 'BEGIN { if (d>0) printf "%d", d*1000000; else print 0 }'
}
# A finished 2K clip: HEVC at exactly the locked geometry.
is_done_2k() {
  [ -f "$1" ] || return 1
  [ "$(probe_field "$1" codec_name)" = "hevc" ] || return 1
  [ "$(probe_field "$1" width)" = "$HEVC_W" ] || return 1
  [ "$(probe_field "$1" height)" = "$HEVC_H" ] || return 1
}
# A finished 4K clip: HEVC within the caps.
is_done_4k() {
  [ -f "$1" ] || return 1
  local w h fps
  [ "$(probe_field "$1" codec_name)" = "hevc" ] || return 1
  w=$(probe_field "$1" width); h=$(probe_field "$1" height); fps=$(probe_fps "$1")
  [ "${w:-0}" -le "$MAX4K_W" ] || return 1
  [ "${h:-0}" -le "$MAX4K_H" ] || return 1
  awk -v f="$fps" -v m="$MAX4K_FPS" 'BEGIN { exit f<=m+0.01 ? 0 : 1 }'
}

choose_encoder() {
  ffmpeg -hide_banner -encoders 2>/dev/null \
    | awk '$1 ~ /^V/ && $2=="libx265"{f=1} END{exit f?0:1}' && { echo libx265; return; }
  ffmpeg -hide_banner -encoders 2>/dev/null \
    | awk '$1 ~ /^V/ && $2=="hevc"{f=1} END{exit f?0:1}' && { echo hevc; return; }
  return 1
}

# Build the filtergraph (ending in [v]) for a 2K/portrait category.
graph_for() {
  case "$1" in
    clips)   echo "[0:v]scale=${HEVC_W}:${HEVC_H}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=${HEVC_W}:${HEVC_H}:(ow-iw)/2:(oh-ih)/2,fps=${FPS},format=yuv420p[v]" ;;
    rotate)  echo "[0:v]transpose=1,scale=${HEVC_W}:${HEVC_H}:force_original_aspect_ratio=decrease,pad=${HEVC_W}:${HEVC_H}:(ow-iw)/2:(oh-ih)/2,fps=${FPS},format=yuv420p[v]" ;;
    crop)    echo "[0:v]scale=${HEVC_W}:${HEVC_H}:force_original_aspect_ratio=increase,crop=${HEVC_W}:${HEVC_H},fps=${FPS},format=yuv420p[v]" ;;
    blur)    echo "[0:v]fps=${FPS},split=2[bg][fg];[bg]scale=${HEVC_W}:${HEVC_H}:force_original_aspect_ratio=increase,crop=${HEVC_W}:${HEVC_H},gblur=sigma=32,eq=brightness=-0.10:saturation=0.75[bg2];[fg]scale=${HEVC_W}:${HEVC_H}:force_original_aspect_ratio=decrease[fg2];[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]" ;;
  esac
}

# Run one ffmpeg, streaming its progress into the overall bar.
# args: src out idx total label  ffmpeg-args...
run_ffmpeg() {
  local src="$1" out="$2" idx="$3" total="$4" label="$5"; shift 5
  local base total_us tmp
  base=$(basename "$src")
  tmp="$(dirname "$out")/.processing.$(basename "${out%.*}").$$.mp4"
  total_us=$(duration_us "$src")
  rm -f "$tmp"
  {
    echo "[VJ] === ffmpeg $src -> $out ==="
    ffmpeg -y -hide_banner -nostdin -i "$src" "$@" \
      -progress pipe:1 -nostats "$tmp" 2>&1
    echo "[VJ] === ffmpeg exit ${PIPESTATUS[0]} ==="
  } | while IFS= read -r line; do
    printf '%s\n' "$line" >>"$LOG"
    case "$line" in
      out_time_us=*)
        if [ "$total_us" -gt 0 ]; then
          local ou fp op
          ou=${line#out_time_us=}
          [ -z "$ou" ] || [ "$ou" = "N/A" ] && ou=0
          fp=$(( ou * 100 / total_us )); [ "$fp" -gt 100 ] && fp=100
          op=$(( ((idx-1)*100 + fp) / total ))
          progress "$op" "[$idx/$total] $label $base (${fp}%)"
        fi ;;
    esac
  done
  if [ -s "$tmp" ]; then
    mv -f "$tmp" "$out"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# ── gather work ───────────────────────────────────────────────────────
require_tool ffmpeg
require_tool ffprobe

: >"$LOG"
date '+[VJ] process-all start: %Y-%m-%d %H:%M:%S' >>"$LOG"
start_gui_progress
log "============================================================"
log "  pi-paint VJ — Process All Assets (HEVC)"
log "============================================================"
log "Scope: $ONLY   2K/portrait -> ${HEVC_W}x${HEVC_H} HEVC   Log: $LOG"

mkdir -p "$HEVC_OUT" "$OUT4K" "$P_ROTATE" "$P_CROP"

ENCODER=$(choose_encoder) || {
  show_error "No HEVC encoder" \
    "FFmpeg has no libx265/HEVC encoder.\n\nRe-run setup.sh.\n\nLog: $LOG"
  exit 1
}
log "Encoder: $ENCODER  (preset=$PRESET crf=$CRF)"

# Parallel arrays describing every unit of work.
CATS=(); SRCS=(); OUTS=()
find_into() {  # category srcdir outdir suffix
  local cat="$1" dir="$2" odir="$3" suf="$4" f base stem out
  [ -d "$dir" ] || return
  while IFS= read -r -d '' f; do
    base=$(basename "$f")
    case "$base" in .*|_*) continue ;; esac
    stem="${base%.*}"
    out="$odir/$stem$suf.mp4"
    CATS+=("$cat"); SRCS+=("$f"); OUTS+=("$out")
  done < <(find "$dir" -maxdepth 1 -type f \( \
      -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.m4v' \
      -o -iname '*.webm' -o -iname '*.avi' -o -iname '*.wmv' -o -iname '*.flv' \
      -o -iname '*.gif' \) -print0 2>/dev/null | sort -z)
}

if [ "$ONLY" = "all" ] || [ "$ONLY" = "clips" ]; then
  find_into clips  "$CLIPS_SRC" "$HEVC_OUT" ""
fi
if [ "$ONLY" = "all" ] || [ "$ONLY" = "portrait" ]; then
  find_into rotate "$P_ROTATE"  "$HEVC_OUT" "-landscape"
  find_into crop   "$P_CROP"    "$HEVC_OUT" "-landscape"
  find_into blur   "$P_BLUR"    "$HEVC_OUT" "-landscape"
fi
if [ "$ONLY" = "all" ] || [ "$ONLY" = "4k" ]; then
  find_into 4k     "$SRC4K"     "$OUT4K"    ""
fi

# Clean stale partials from any killed run.
find "$HEVC_OUT" "$OUT4K" -maxdepth 1 -type f -name '.processing.*.mp4' -delete 2>/dev/null || true

TOTAL=${#SRCS[@]}
if [ "$TOTAL" -eq 0 ]; then
  progress 100 "Nothing to process"
  show_info "Process All Assets" \
    "No source videos found.\n\nDrop raw video into:\n • assets/clips/ (2K)\n • assets/portrait/rotate|crop/ or loose in assets/portrait/\n • assets/4k/ (cinematic)\n\nLog: $LOG"
  exit 0
fi
log "Found $TOTAL source file(s)"

# ── process ───────────────────────────────────────────────────────────
processed=0; skipped=0; failed=0
for i in "${!SRCS[@]}"; do
  cat="${CATS[$i]}"; src="${SRCS[$i]}"; out="${OUTS[$i]}"
  idx=$((i+1)); base=$(basename "$src")
  progress $(( i*100/TOTAL )) "[$idx/$TOTAL] Inspecting $base"

  if [ "$cat" = "4k" ]; then
    if is_done_4k "$out"; then
      log "skip: $base (4K already processed)"; skipped=$((skipped+1)); continue
    fi
    src_ext="${src##*.}"; src_ext="${src_ext,,}"
    if is_done_4k "$src" && [ "$src_ext" = "mp4" ]; then
      log "4k copy: $base (already Pi-playable HEVC)"
      if run_ffmpeg "$src" "$out" "$idx" "$TOTAL" "Copying 4K" \
           -map 0:v:0 -an -c:v copy -movflags +faststart; then
        processed=$((processed+1)); else failed=$((failed+1)); log "FAILED: $base"; fi
    else
      vf="scale='min(${MAX4K_W},iw)':'min(${MAX4K_H},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=${MAX4K_FPS},format=yuv420p"
      if run_ffmpeg "$src" "$out" "$idx" "$TOTAL" "Transcoding 4K" \
           -map 0:v:0 -an -vf "$vf" -c:v "$ENCODER" -preset "$PRESET4K" \
           -crf "$CRF4K" -tag:v hvc1 -movflags +faststart; then
        processed=$((processed+1)); else failed=$((failed+1)); log "FAILED: $base"; fi
    fi
    continue
  fi

  # 2K / portrait HEVC
  if is_done_2k "$out"; then
    log "skip: $base ($cat already baked to ${HEVC_W}x${HEVC_H})"; skipped=$((skipped+1)); continue
  fi
  graph=$(graph_for "$cat")
  encargs=( -map "[v]" -an -c:v "$ENCODER" -preset "$PRESET" -crf "$CRF" \
            -profile:v main -tag:v hvc1 -movflags +faststart )
  [ "$ENCODER" = "libx265" ] && encargs+=( -x265-params log-level=error )
  if run_ffmpeg "$src" "$out" "$idx" "$TOTAL" "Baking $cat" \
       -filter_complex "$graph" "${encargs[@]}"; then
    log "ok: $base -> $out"; processed=$((processed+1))
  else
    log "FAILED: $base"; failed=$((failed+1))
  fi
done

progress 100 "Done. processed=$processed skipped=$skipped failed=$failed"
date '+[VJ] process-all end: %Y-%m-%d %H:%M:%S' >>"$LOG"

SUMMARY="Sources found: $TOTAL\nProcessed: $processed   Skipped (already done): $skipped   Failed: $failed\n\n2K & portrait clips are in assets/clips_hevc/ — launch with \"Start VJ.sh\".\n4K clips are in assets/4k/processed/ — press N in the app for cinematic mode.\n\nFull log: $LOG"
if [ "${VJ_NO_DIALOG:-0}" = "1" ]; then
  log "$SUMMARY"
elif [ "$failed" -gt 0 ]; then
  show_error "Process All — finished with errors" "$SUMMARY"
else
  show_info "Process All Assets — done" "$SUMMARY"
fi
