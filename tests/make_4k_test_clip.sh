#!/usr/bin/env bash
# Make a synthetic 4K (3840x2160) H.265/HEVC test clip for Spike B.
#
# The Pi 5 has no hardware *encoder*, so this encodes in software (slow,
# one-time — a couple of minutes). We only need it once; Spike B then
# decodes it on the HARDWARE HEVC decoder, which is the thing we're
# actually measuring.
#
# A moving 'ball' pattern is used on purpose: motion makes any decode
# judder or dropped frames visible to the eye, not just to the counter.
#
# Usage:
#   ./tests/make_4k_test_clip.sh                 # 20s synthetic 4K HEVC
#   ./tests/make_4k_test_clip.sh path/to/in.mp4  # transcode an existing
#                                                # clip up to 4K HEVC
#
# Output: tests/4k_hevc_test.mp4
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="tests/4k_hevc_test.mp4"
SECONDS_LEN=20
W=3840
H=2160
FPS=30

have() { command -v "$1" >/dev/null 2>&1; }

if [[ $# -ge 1 && -f "$1" ]]; then
  # --- Transcode an existing clip to 4K HEVC -----------------------------
  IN="$1"
  echo "[make-4k] transcoding $IN → $OUT (3840x2160 HEVC, software encode)…"
  if have ffmpeg; then
    ffmpeg -y -i "$IN" -vf "scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2" \
      -c:v libx265 -preset medium -crf 24 -an "$OUT"
  else
    echo "[make-4k] ffmpeg not found; install it or use the synthetic path." >&2
    exit 1
  fi
else
  # --- Generate a synthetic 4K HEVC clip ---------------------------------
  echo "[make-4k] generating ${SECONDS_LEN}s synthetic 4K HEVC → $OUT (software encode, be patient)…"
  if have ffmpeg; then
    # testsrc2 = moving gradient + bouncing elements; high spatial detail.
    ffmpeg -y -f lavfi -i "testsrc2=size=${W}x${H}:rate=${FPS}:duration=${SECONDS_LEN}" \
      -c:v libx265 -preset medium -crf 24 -an "$OUT"
  elif have gst-launch-1.0; then
    NUM=$(( SECONDS_LEN * FPS ))
    gst-launch-1.0 -e videotestsrc num-buffers=$NUM pattern=ball \
      ! "video/x-raw,width=${W},height=${H},framerate=${FPS}/1" \
      ! x265enc ! h265parse ! mp4mux ! filesink location="$OUT"
  else
    echo "[make-4k] need ffmpeg (libx265) or gst-launch-1.0 (x265enc). Install one." >&2
    exit 1
  fi
fi

echo "[make-4k] wrote $OUT"
ls -lh "$OUT"
echo "[make-4k] now run:  ./venv/bin/python tests/spike_b_4k_decode.py --clip $OUT --fullscreen --output-display 1"
