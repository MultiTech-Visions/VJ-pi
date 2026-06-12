#!/bin/bash
# pi-paint VJ — one-time projectM (MilkDrop) setup.
# Double-click this file in the file manager and choose "Execute in Terminal"
# (it needs your password once, for 'sudo apt install' — same as setup.sh).
#
# Installs build tools, builds libprojectM v4 from source with GLES enabled
# (Debian only packages the old v2/v3 API), and downloads a MilkDrop preset
# pack pre-filtered to hold >=24fps on a Pi 5, plus the shared texture pack.
# Everything lands inside this folder (vendor/ + assets/), no system install.
#
# Safe to re-run — finished steps are skipped. The build takes a while on a
# Pi 5 (roughly 15-40 minutes); let it sit. When it's done, just launch with
# "Start VJ.sh" as usual: the new visuals appear in the [ and ] generator
# cycle after the existing ones.

set -e
cd "$(dirname "$0")"

LOG="$(pwd)/vj_last_projectm_setup.log"
if [ "${VJ_PM_SETUP_TEED:-0}" = "0" ]; then
    export VJ_PM_SETUP_TEED=1
    bash "$0" "$@" 2>&1 | tee "$LOG"
    EXIT=${PIPESTATUS[0]}
    if [ "$EXIT" -ne 0 ] && command -v zenity >/dev/null 2>&1; then
        zenity --error --width=720 --title="ProjectM setup failed (exit $EXIT)" \
          --text="Setup did not finish.\n\nFull log: $LOG\n\nLast lines:\n\n$(tail -25 "$LOG")" 2>/dev/null
    fi
    exit "$EXIT"
fi
date '+[VJ] projectM setup start: %Y-%m-%d %H:%M:%S'

PROJECTM_TAG="v4.1.6"
PROJECTM_REPO="https://github.com/projectM-visualizer/projectm.git"
PRESETS_REPO="https://github.com/mickabrig7/projectM-presets-rpi5.git"
TEXTURES_REPO="https://github.com/projectM-visualizer/presets-milkdrop-texture-pack.git"

echo ""
echo "============================================================"
echo "  pi-paint VJ — ProjectM (MilkDrop) setup"
echo "============================================================"
echo ""
echo "This will:"
echo "  • apt-install build tools (sudo password needed once)"
echo "  • build libprojectM $PROJECTM_TAG with GLES → ./vendor/projectm/"
echo "  • download Pi-5-filtered MilkDrop presets → ./assets/projectm_presets/"
echo "  • download the MilkDrop texture pack → ./assets/projectm_textures/"
echo ""
echo "The build step takes 15-40 minutes on a Pi 5. Leave it running."
echo ""

# ── 1. Build dependencies ─────────────────────────────────────────────
echo "[1/4] Installing build dependencies..."
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git pkg-config \
  libglm-dev libgles-dev libegl-dev \
  python3-numpy
echo "    done."
echo ""

# ── 2. Build libprojectM v4 (GLES) ────────────────────────────────────
mkdir -p vendor
if compgen -G "vendor/projectm/lib*/libprojectM-4.so*" >/dev/null \
   || compgen -G "vendor/projectm/lib/*/libprojectM-4.so*" >/dev/null; then
  echo "[2/4] libprojectM already built in vendor/projectm/ — skipping."
  echo "      (delete vendor/projectm/ to force a rebuild)"
else
  echo "[2/4] Building libprojectM $PROJECTM_TAG (this is the slow part)..."
  if [ ! -d vendor/projectm-src/.git ]; then
    rm -rf vendor/projectm-src
    for attempt in 1 2 3 4; do
      if git clone --depth 1 --branch "$PROJECTM_TAG" --recurse-submodules \
           "$PROJECTM_REPO" vendor/projectm-src; then
        break
      fi
      [ "$attempt" -eq 4 ] && { echo "ERROR: could not clone projectM."; exit 1; }
      WAIT=$((2 ** attempt)); echo "    clone failed — retrying in ${WAIT}s..."; sleep "$WAIT"
    done
  fi
  cmake -S vendor/projectm-src -B vendor/projectm-build \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_GLES=ON \
    -DBUILD_SHARED_LIBS=ON \
    -DBUILD_TESTING=OFF \
    -DCMAKE_INSTALL_PREFIX="$(pwd)/vendor/projectm"
  cmake --build vendor/projectm-build --parallel "$(nproc)"
  cmake --install vendor/projectm-build
  echo "    built and installed to vendor/projectm/."
fi
echo ""

# ── 3. Preset pack (pre-filtered for Pi 5) ────────────────────────────
if [ -d assets/projectm_presets ] && \
   [ "$(find assets/projectm_presets -name '*.milk' 2>/dev/null | head -1)" ]; then
  echo "[3/4] Preset pack already present — skipping."
else
  echo "[3/4] Downloading Pi-5-filtered MilkDrop presets..."
  rm -rf assets/projectm_presets
  for attempt in 1 2 3 4; do
    if git clone --depth 1 "$PRESETS_REPO" assets/projectm_presets; then
      break
    fi
    [ "$attempt" -eq 4 ] && { echo "ERROR: could not clone preset pack."; exit 1; }
    WAIT=$((2 ** attempt)); echo "    clone failed — retrying in ${WAIT}s..."; sleep "$WAIT"
  done
  echo "    done."
fi
echo ""

# ── 4. Texture pack (images many presets sample from) ─────────────────
if [ -d assets/projectm_textures ]; then
  echo "[4/4] Texture pack already present — skipping."
else
  echo "[4/4] Downloading MilkDrop texture pack..."
  for attempt in 1 2 3 4; do
    if git clone --depth 1 "$TEXTURES_REPO" assets/projectm_textures; then
      break
    fi
    [ "$attempt" -eq 4 ] && { echo "WARNING: texture pack clone failed — presets still work, some lose textures."; break; }
    WAIT=$((2 ** attempt)); echo "    clone failed — retrying in ${WAIT}s..."; sleep "$WAIT"
  done
  echo "    done."
fi
echo ""

PRESET_COUNT=$(find assets/projectm_presets -name '*.milk' 2>/dev/null | wc -l)
echo "============================================================"
echo "  ProjectM setup complete! ($PRESET_COUNT presets installed)"
echo "============================================================"
echo ""
echo "Launch with 'Start VJ.sh' as usual. The MilkDrop visuals appear"
echo "in the [ and ] generator cycle right after the existing ones"
echo "(the cycle uses a 40-preset sample of the pack; plug in a USB"
echo "mic and they react to the music)."
echo ""
if command -v zenity >/dev/null 2>&1; then
  zenity --info --width=520 --title="ProjectM setup complete" \
    --text="$PRESET_COUNT MilkDrop presets installed.\n\nLaunch with Start VJ.sh — the new visuals are in the [ and ] generator cycle.\n\nLog: $LOG" 2>/dev/null || true
fi
read -p "Press Enter to close this window..."
