#!/bin/bash
# pi-paint VJ — decode-path benchmark.
#
# Answers ONE question: does HEVC hardware decode (the Pi 5's only HW decoder)
# beat today's H.264 software decode once the frame is back in CPU memory as a
# BGR numpy frame the FX pipeline can use? If it does, we have CPU headroom to
# raise the base canvas to 1080p (or feed 4K detail) while keeping live FX.
#
# Non-destructive: builds short test clips under bench_assets/ and probes
# several decode paths. Touches nothing in the live app. Results (FPS + CPU%)
# are written to vj_last_bench.log and shown in a dialog at the end.
#
# Usage: double-click, or:  ./Benchmark\ Decode.sh [SOURCE_VIDEO]
#   SOURCE_VIDEO defaults to the first clip found in assets/4k/_originals,
#   then assets/clips, then assets/4k. A higher-detail source gives a fairer
#   read on the 1080p/4K paths.
# Env: BENCH_WITH_4K=1 to also build + test a 4K HEVC clip (slow to encode).

cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG="$ROOT/vj_last_bench.log"
VENV_PY="$ROOT/venv/bin/python"
SYS_PY="/usr/bin/python3"
[ -x "$SYS_PY" ] || SYS_PY="$(command -v python3)"
OUTDIR="$ROOT/bench_assets"

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

: >"$LOG"
exec > >(tee -a "$LOG") 2>&1
date '+[bench] start: %Y-%m-%d %H:%M:%S'
git -C "$ROOT" log --oneline -1 2>/dev/null

if [ ! -x "$VENV_PY" ]; then
  show_info "VJ-pi: setup needed" \
    "venv missing — run setup.sh first.\n\nLog: $LOG"
  exit 1
fi

# ── pick a source clip ───────────────────────────────────────────────────
SRC="$1"
if [ -z "$SRC" ]; then
  for d in "assets/4k/_originals" "assets/clips" "assets/4k"; do
    SRC=$(find "$d" -maxdepth 1 -type f \
            \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.m4v' \) \
            ! -name '_*' 2>/dev/null | sort | head -n1)
    [ -n "$SRC" ] && break
  done
fi
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  show_info "Benchmark: no source clip" \
    "No source video found in assets/. Drop a clip in assets/clips/ (or pass one as an argument) and re-run.\n\nLog: $LOG"
  exit 1
fi
echo "[bench] source clip: $SRC"

# ── build matched test clips ─────────────────────────────────────────────
PREP_ARGS=(prep --source "$SRC" --outdir "$OUTDIR")
[ "${BENCH_WITH_4K:-0}" = "1" ] && PREP_ARGS+=(--with-4k)
echo "[bench] === prep (transcoding 15s test clips; HEVC encode is slow) ==="
"$VENV_PY" bench_decode.py "${PREP_ARGS[@]}" || {
  show_info "Benchmark: prep failed" "ffmpeg transcode failed — see $LOG"; exit 1; }

# ── run the matrix ───────────────────────────────────────────────────────
echo "[bench] === baselines: OpenCV / H.264 software decode → numpy ==="
[ -f "$OUTDIR/h264_720p.mp4" ]  && "$VENV_PY" bench_decode.py opencv "$OUTDIR/h264_720p.mp4"  --label "opencv h264 720p (TODAY)"
[ -f "$OUTDIR/h264_1080p.mp4" ] && "$VENV_PY" bench_decode.py opencv "$OUTDIR/h264_1080p.mp4" --label "opencv h264 1080p (naive)"

echo "[bench] === ffmpeg -hwaccel drm: HEVC hardware decode → numpy ==="
[ -f "$OUTDIR/h265_1080p.mp4" ] && "$VENV_PY" bench_decode.py ffmpeg "$OUTDIR/h265_1080p.mp4" --label "ffmpeg-drm h265 1080p"
[ -f "$OUTDIR/h265_2160p.mp4" ] && "$VENV_PY" bench_decode.py ffmpeg "$OUTDIR/h265_2160p.mp4" --label "ffmpeg-drm h265 4K"

echo "[bench] === GStreamer v4l2slh265dec → BGR appsink → numpy (system python) ==="
if [ -n "$SYS_PY" ]; then
  for conv in videoconvert gl pisp; do
    [ -f "$OUTDIR/h265_1080p.mp4" ] && "$SYS_PY" bench_decode.py gst "$OUTDIR/h265_1080p.mp4" --conv "$conv" --label "gst-$conv h265 1080p"
    [ -f "$OUTDIR/h265_2160p.mp4" ] && "$SYS_PY" bench_decode.py gst "$OUTDIR/h265_2160p.mp4" --conv "$conv" --label "gst-$conv h265 4K"
  done
else
  echo "[bench] no system python3 found — skipping GStreamer paths"
fi

echo "[bench] === summary (floor = 13 fps) ==="
grep '^RESULT' "$LOG" || echo "[bench] no RESULT lines — every path failed; see log above"
date '+[bench] end: %Y-%m-%d %H:%M:%S'

SUMMARY=$(grep '^RESULT' "$LOG" | sed 's/^RESULT //')
show_info "Benchmark complete" \
  "Decode-path results (floor = 13 fps):\n\n${SUMMARY:-No results — see log.}\n\nFull log: $LOG"
