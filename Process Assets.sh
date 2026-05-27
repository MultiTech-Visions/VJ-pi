#!/bin/bash
# pi-paint VJ — bulk transcode the clip + overlay library to MJPEG 720p.
# Double-click in the file manager and choose "Execute".
#
# Why MJPEG and not HEVC: the obvious answer would be HEVC since
# Pi 5 has hardware HEVC decode (v4l2slh265dec). In practice the
# zero-copy DMABuf path from that decoder into glupload doesn't
# negotiate cleanly on Pi OS today — the workaround (videoconvert
# between decoder and glupload) eats ~60% CPU just shuffling YUV
# from DMABuf into system memory, and there are known buffer-
# format issues downstream. See [[research-findings-pi-vj]].
#
# MJPEG flips this: each frame is an independent JPEG. libjpeg-turbo
# decodes it directly to system memory with no negotiation drama,
# glupload picks it up cleanly, and the whole rendering path is
# happy. Measured on a Pi 5 with our 2-branch tee downstream:
#   HEVC 720p:   ~98% CPU (broken DMABuf path tax)
#   H.264 720p: ~207% CPU (no Pi 5 hw decode, software at 720p
#               is heavy on the ARM cores even with 4 of them)
#   MJPEG 720p:  ~19% CPU  ← winner
#
# Trade-off: MJPEG files are ~2× the size of HEVC at equivalent
# visual quality (5-second loop: ~35MB vs ~18MB). For a VJ clip
# library this is fine. Disk is cheap; CPU at showtime isn't.
#
# Behaviour:
#   * Walks assets/clips/ and assets/overlays/.
#   * For each .mp4 / .mov that ISN'T already HEVC at 1280x720,
#     transcodes via ffmpeg (libx265, fast preset, crf 23, no audio).
#   * Moves the original to assets/<dir>/.original/ before swapping
#     in the new file — NOTHING is deleted. The original stays around
#     in case the transcode is bad or you want it back.
#   * Idempotent: re-running skips files already at HEVC 720p.
#
# UI: zenity progress dialog with per-file progress while it runs.
# Errors and per-file ffmpeg output go to vj_last_process.log.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_process.log"

show_error() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
  fi
}

show_info() {
  local title="$1"
  local body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --width=720 --title="$title" --text="$body" 2>/dev/null
  fi
}

# Pre-flight: every tool we depend on
for tool in ffmpeg ffprobe zenity; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    show_error "Missing tool: $tool" \
      "The tool '$tool' isn't installed on this Pi.\n\nRun setup.sh first."
    exit 1
  fi
done

: > "$LOG"
date '+[VJ] process start: %Y-%m-%d %H:%M:%S' >> "$LOG"

# Collect every .mp4 / .mov from clips/ + overlays/.
# -maxdepth 1 keeps us from descending into .original/ on a re-run.
mapfile -t FILES < <(
  for dir in assets/clips assets/overlays; do
    [ -d "$dir" ] || continue
    find "$dir" -maxdepth 1 -type f \( -iname '*.mp4' -o -iname '*.mov' \) | sort
  done
)

TOTAL=${#FILES[@]}
if [ "$TOTAL" -eq 0 ]; then
  show_info "Nothing to process" \
    "No .mp4 or .mov files found in assets/clips/ or assets/overlays/.\n\nDrop your library in those folders, then double-click this again."
  exit 0
fi
echo "[VJ] found $TOTAL candidate file(s)" >> "$LOG"

# Returns 0 if the file is already MJPEG at exactly 1280x720 — the
# canonical "no transcode needed" state.
is_already_processed() {
  local f="$1"
  local info
  info=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height \
    -of csv=p=0 "$f" 2>/dev/null)
  [ "$info" = "mjpeg,1280,720" ]
}

# Drive zenity --progress by echoing percentage + "# comment" lines.
# The body of this subshell is the work loop; piping it to zenity
# means the GUI advances as the loop progresses.
(
  i=0
  for f in "${FILES[@]}"; do
    i=$((i + 1))
    pct=$(( (i - 1) * 100 / TOTAL ))
    name=$(basename "$f")
    dir=$(dirname "$f")

    echo "$pct"
    echo "# [$i/$TOTAL] Inspecting: $name"

    if is_already_processed "$f"; then
      echo "[VJ] skip (already HEVC 720p): $f" >> "$LOG"
      continue
    fi

    echo "# [$i/$TOTAL] Transcoding: $name (this can take a minute or two per clip)"

    orig_dir="$dir/.original"
    mkdir -p "$orig_dir"
    # Output container is .mov so the codec→container pairing is
    # unambiguous (mp4 + mjpeg is technically legal but some
    # players choke; .mov + mjpeg is the well-trodden path).
    base="${name%.*}"
    tmp_out="$dir/.${base}.tmp.mov"
    final_name="${base}.mov"

    {
      echo "[VJ] === transcoding $f ==="
      # -q:v 5 is a good MJPEG quality default (libjpeg-turbo
      #   scale of 1-31, lower is higher quality). 5 looks
      #   visually transparent on VJ-style loops while keeping
      #   file sizes ~2× HEVC at the same resolution.
      # -pix_fmt yuvj420p is the JPEG-native colour space; using
      #   yuv420p would force a needless extra colour-range
      #   conversion at decode time.
      ffmpeg -y -nostdin -loglevel warning \
        -i "$f" \
        -vf scale=1280:720 \
        -c:v mjpeg -q:v 5 -pix_fmt yuvj420p \
        -an \
        "$tmp_out" 2>&1
      echo "[VJ] === ffmpeg exit $? ==="
    } >> "$LOG"

    if [ ! -s "$tmp_out" ]; then
      echo "[VJ] ERROR: empty output for $f — leaving original in place" >> "$LOG"
      rm -f "$tmp_out"
      continue
    fi

    # Atomic-ish swap: original aside, temp into place. The
    # container is now .mov (MJPEG-in-.mp4 is technically legal
    # but flaky with some players), so the processed file gets a
    # new extension. Originals are kept in .original/ either way.
    mv "$f" "$orig_dir/$name"
    mv "$tmp_out" "$dir/$final_name"
    echo "[VJ] ok: $dir/$final_name (original moved to $orig_dir/$name)" >> "$LOG"
  done

  echo "100"
  echo "# Done."
) | zenity --progress \
    --title="VJ-pi — Process Assets" \
    --text="Starting…" \
    --percentage=0 \
    --width=520 \
    --auto-close \
    --no-cancel \
  2>/dev/null

date '+[VJ] process end: %Y-%m-%d %H:%M:%S' >> "$LOG"

# Tally results from the log so the summary dialog is honest about
# what actually happened (counts may differ from the loop's
# expectation if ffmpeg failed mid-way).
processed=$(grep -c "^\[VJ\] ok:" "$LOG" 2>/dev/null || echo 0)
skipped=$(grep -c "^\[VJ\] skip" "$LOG" 2>/dev/null || echo 0)
errors=$(grep -c "^\[VJ\] ERROR" "$LOG" 2>/dev/null || echo 0)

show_info "Process Assets — done" \
  "Transcoded: $processed\nSkipped (already HEVC 720p): $skipped\nErrors: $errors\n\nOriginals are preserved in assets/clips/.original/ and assets/overlays/.original/ — delete them by hand once you've confirmed the new versions look right.\n\nFull log: $LOG"
