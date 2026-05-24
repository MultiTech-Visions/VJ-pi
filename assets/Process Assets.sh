#!/bin/bash
# pi-paint VJ — Asset processor.
#
# Normalises every video in ./clips/ and ./overlays/ to the projector's
# resolution (read from ../config.py), H.264, no audio. Originals are
# moved to a sibling `_originals/` folder so nothing gets destroyed.
#
# Re-running is safe: files that are already normalised are skipped.
#
# Double-click in the file manager and choose "Execute in Terminal".

set -uo pipefail
cd "$(dirname "$0")"

# ── Target resolution (read from ../config.py so it tracks any edits) ──
CONFIG_PY="../config.py"
TARGET_W=$(grep -E '^\s*width:\s*int\s*=' "$CONFIG_PY" 2>/dev/null | head -1 | grep -oE '[0-9]+' | head -1)
TARGET_H=$(grep -E '^\s*height:\s*int\s*=' "$CONFIG_PY" 2>/dev/null | head -1 | grep -oE '[0-9]+' | head -1)
TARGET_W=${TARGET_W:-854}
TARGET_H=${TARGET_H:-480}

# ffmpeg preset: "medium" is a reasonable Pi-friendly balance. Bump to
# "slow" if you're prepping a big library on a desktop.
PRESET="${VJ_FFMPEG_PRESET:-medium}"
CRF="${VJ_FFMPEG_CRF:-22}"

die() {
  echo "ERROR: $*" >&2
  read -rp "Press Enter to close..."
  exit 1
}

command -v ffmpeg  >/dev/null 2>&1 || die "ffmpeg not found. Run ../setup.sh first."
command -v ffprobe >/dev/null 2>&1 || die "ffprobe not found. Run ../setup.sh first."

# ── Per-file processing ───────────────────────────────────────────────

# $1: input path
# Echoes "codec|width|height|hasAudio" (hasAudio = 0 or 1).
probe_file() {
  local f="$1"
  local codec width height has_audio
  codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$f" 2>/dev/null)
  width=$(ffprobe -v error -select_streams v:0 -show_entries stream=width  -of csv=p=0 "$f" 2>/dev/null)
  height=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$f" 2>/dev/null)
  if ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$f" 2>/dev/null | grep -q .; then
    has_audio=1
  else
    has_audio=0
  fi
  echo "${codec}|${width}|${height}|${has_audio}"
}

# $1: input file
# $2: mode ("clip" or "overlay")
process_file() {
  local in_file="$1"
  local mode="$2"
  local dir base stem ext_lower probe codec width height has_audio
  dir=$(dirname "$in_file")
  base=$(basename "$in_file")
  stem="${base%.*}"
  ext_lower=$(printf '%s' "${base##*.}" | tr '[:upper:]' '[:lower:]')

  probe=$(probe_file "$in_file")
  IFS='|' read -r codec width height has_audio <<<"$probe"
  if [ -z "$codec" ]; then
    echo "  ⚠ couldn't probe — skipping"
    return
  fi
  printf "  %s  %sx%s  %s  audio=%s\n" \
    "$codec" "$width" "$height" "$ext_lower" "$has_audio"

  # Already normalised?
  if [ "$codec" = "h264" ] \
     && [ "$width" = "$TARGET_W" ] && [ "$height" = "$TARGET_H" ] \
     && [ "$has_audio" = "0" ] && [ "$ext_lower" = "mp4" ]; then
    echo "  ✓ already normalised, skipping"
    return
  fi

  # Choose scaling strategy by mode:
  #   clips     → fill the frame (scale + centre-crop; no black bars)
  #   overlays  → fit + pad with black (screen-blend makes black invisible
  #               so padding is free; cropping might cut off bright FX)
  local vf
  if [ "$mode" = "clip" ]; then
    vf="scale=${TARGET_W}:${TARGET_H}:force_original_aspect_ratio=increase,crop=${TARGET_W}:${TARGET_H}"
  else
    vf="scale=${TARGET_W}:${TARGET_H}:force_original_aspect_ratio=decrease,pad=${TARGET_W}:${TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black"
  fi

  local out_file="$dir/$stem.mp4"
  local tmp_out="$dir/.processing.$stem.$$.mp4"
  local backup_dir="$dir/_originals"
  mkdir -p "$backup_dir"

  echo "  → re-encoding (preset=$PRESET, crf=$CRF)..."
  if ! ffmpeg -y -hide_banner -loglevel error -nostdin \
        -i "$in_file" \
        -vf "$vf" \
        -c:v libx264 -preset "$PRESET" -crf "$CRF" \
        -pix_fmt yuv420p -movflags +faststart -an \
        "$tmp_out" </dev/null; then
    rm -f "$tmp_out"
    echo "  ✗ ffmpeg failed"
    return
  fi

  # Move original out of the way, then promote the new file. Same-name
  # case (foo.mp4 → foo.mp4) is handled by always moving the original
  # to _originals/ first.
  mv -f "$in_file" "$backup_dir/$base"
  mv -f "$tmp_out" "$out_file"
  echo "  ✓ wrote $(basename "$out_file"); original → _originals/"
}

# $1: directory (clips or overlays)
# $2: mode
process_dir() {
  local dir="$1"
  local mode="$2"
  if [ ! -d "$dir" ]; then
    echo "  (no $dir/ directory)"
    return
  fi

  # Sweep partial files from any previous killed run.
  find "$dir" -maxdepth 1 -type f -name '.processing.*' -delete 2>/dev/null

  local found=0
  while IFS= read -r -d '' f; do
    found=1
    echo "→ $f"
    process_file "$f" "$mode"
  done < <(find "$dir" -maxdepth 1 -type f \
            \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' \
            -o -iname '*.webm' -o -iname '*.avi' -o -iname '*.gif' \
            -o -iname '*.m4v' -o -iname '*.wmv' -o -iname '*.flv' \) \
            -not -name '.processing.*' \
            -print0)

  if [ "$found" = "0" ]; then
    echo "  (no video files in $dir/)"
  fi
}

# ── Main ──────────────────────────────────────────────────────────────

echo "============================================================"
echo "  pi-paint VJ — Asset Processor"
echo "============================================================"
echo ""
echo "  Target  : ${TARGET_W}x${TARGET_H} H.264 (no audio)"
echo "  Preset  : $PRESET   CRF: $CRF"
echo "  Backups : <dir>/_originals/"
echo ""

echo "[1/2] clips/"
process_dir "clips" "clip"
echo ""
echo "[2/2] overlays/"
process_dir "overlays" "overlay"
echo ""
echo "Done."
# Only pause when run interactively (double-click from file manager).
# Pipelines / CI / "echo | bash" runs skip the pause.
if [ -t 0 ]; then
  read -rp "Press Enter to close..."
fi
