#!/bin/bash
# pi-paint VJ — bulk transcode the clip + overlay library to HEVC 720p.
# Double-click in the file manager and choose "Execute".
#
# Why: Pi 5 has no H.264 hardware decode (the block was removed by
# the Pi Foundation). HEVC at canvas resolution gets hardware decode
# via v4l2slh265dec, freeing the CPU. Transcoding once = smooth
# playback forever.
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

# Returns 0 if the file is already HEVC at exactly 1280x720 — the
# canonical "no transcode needed" state.
is_already_processed() {
  local f="$1"
  local info
  info=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height \
    -of csv=p=0 "$f" 2>/dev/null)
  [ "$info" = "hevc,1280,720" ]
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
    tmp_out="$dir/.${name}.tmp.mp4"

    {
      echo "[VJ] === transcoding $f ==="
      ffmpeg -y -nostdin -loglevel warning \
        -i "$f" \
        -vf scale=1280:720 \
        -c:v libx265 -preset fast -crf 23 \
        -an \
        "$tmp_out" 2>&1
      echo "[VJ] === ffmpeg exit $? ==="
    } >> "$LOG"

    if [ ! -s "$tmp_out" ]; then
      echo "[VJ] ERROR: empty output for $f — leaving original in place" >> "$LOG"
      rm -f "$tmp_out"
      continue
    fi

    # Atomic-ish swap: original aside, temp into place under the
    # original filename. The engine doesn't care about renames as
    # long as the file's contents are valid — keeping the name
    # stable means any references (favourites, saved state, etc.)
    # keep working.
    mv "$f" "$orig_dir/$name"
    mv "$tmp_out" "$f"
    echo "[VJ] ok: $f (original moved to $orig_dir/$name)" >> "$LOG"
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
