#!/bin/bash
# pi-paint VJ — decode-path benchmark.
#
# Answers ONE question: does HEVC hardware decode (the Pi 5's only HW decoder)
# beat today's H.264 software decode once the frame is back in CPU memory as a
# BGR numpy frame the FX pipeline can use? If it does, we have CPU headroom to
# raise the base canvas to 1080p (or feed 4K detail) while keeping live FX.
#
# Uses your EXISTING clips — no transcoding:
#   * H.264 baseline  : first clip in assets/clips/      (today's path)
#   * HEVC HW decode  : first clip in assets/4k/processed/
# Override either by passing them:  ./Benchmark\ Decode.sh H264_CLIP HEVC_CLIP
#
# Non-destructive: reads clips, probes decode paths, writes FPS + CPU% to
# vj_last_bench.log and a dialog. Touches nothing in the live app.

cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG="$ROOT/vj_last_bench.log"
VENV_PY="$ROOT/venv/bin/python"
SYS_PY="/usr/bin/python3"
[ -x "$SYS_PY" ] || SYS_PY="$(command -v python3)"

show_info() {
  local title="$1" body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --width=720 --title="$title" --text="$body" 2>/dev/null
  elif command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
  else
    echo "$title"; echo "$body"
  fi
}

first_clip() {  # first non-underscore video in a dir
  find "$1" -maxdepth 1 -type f \
    \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.m4v' \) \
    ! -name '_*' 2>/dev/null | sort | head -n1
}

: >"$LOG"
exec > >(tee -a "$LOG") 2>&1
date '+[bench] start: %Y-%m-%d %H:%M:%S'
git -C "$ROOT" log --oneline -1 2>/dev/null

[ -x "$VENV_PY" ] || { show_info "VJ-pi: setup needed" "venv missing — run setup.sh first.\n\nLog: $LOG"; exit 1; }

H264="${1:-$(first_clip assets/clips)}"
HEVC="${2:-$(first_clip assets/4k/processed)}"
[ -z "$HEVC" ] && HEVC="$(first_clip assets/4k)"
echo "[bench] H.264 clip: ${H264:-<none found in assets/clips>}"
echo "[bench] HEVC  clip: ${HEVC:-<none found in assets/4k/processed>}"

if [ -z "$H264" ] && [ -z "$HEVC" ]; then
  show_info "Benchmark: no clips" \
    "No clips found in assets/clips/ or assets/4k/processed/. Pass paths as arguments.\n\nLog: $LOG"
  exit 1
fi

echo "[bench] === baseline: OpenCV / H.264 software decode → numpy (TODAY) ==="
[ -n "$H264" ] && "$VENV_PY" bench_decode.py opencv "$H264" --label "opencv h264 (TODAY)"

if [ -n "$HEVC" ]; then
  echo "[bench] === ffmpeg -hwaccel drm: HEVC hardware decode → numpy ==="
  "$VENV_PY" bench_decode.py ffmpeg "$HEVC" --label "ffmpeg-drm hevc"

  echo "[bench] === GStreamer v4l2slh265dec → BGR appsink → numpy (system python) ==="
  if [ -n "$SYS_PY" ]; then
    for conv in videoconvert gl pisp; do
      "$SYS_PY" bench_decode.py gst "$HEVC" --conv "$conv" --label "gst-$conv hevc"
    done
  else
    echo "[bench] no system python3 found — skipping GStreamer paths"
  fi
fi

echo "[bench] === summary (floor = 13 fps) ==="
grep '^RESULT' "$LOG" || echo "[bench] no RESULT lines — every path failed; see log above"
date '+[bench] end: %Y-%m-%d %H:%M:%S'

SUMMARY=$(grep '^RESULT' "$LOG" | sed 's/^RESULT //')
show_info "Benchmark complete" \
  "Decode-path results (floor = 13 fps):\n\n${SUMMARY:-No results — see log.}\n\nFull log: $LOG"
