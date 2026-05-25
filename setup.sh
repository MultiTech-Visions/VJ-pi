#!/bin/bash
# pi-paint VJ — one-time setup.
# Double-click this file in the file manager and choose "Execute in Terminal".
# Installs system deps, creates a Python virtualenv, installs pygame/opencv/numpy.
# Safe to re-run — it skips anything already installed.

set -e
cd "$(dirname "$0")"

echo ""
echo "============================================================"
echo "  pi-paint VJ — Setup"
echo "============================================================"
echo ""
echo "This will install:"
echo "  • SDL2 + OpenGL/EGL/GLES libraries (for pygame + moderngl + opencv)"
echo "  • A Python virtualenv in ./venv/"
echo "  • pygame, opencv-python, numpy, moderngl"
echo ""
echo "You'll be prompted for your password (for 'sudo apt install')."
echo ""
read -p "Press Enter to begin, or Ctrl-C to cancel..."
echo ""

# ── 1. System packages ────────────────────────────────────────────────
echo "[1/3] Installing system packages..."
sudo apt-get update
sudo apt-get install -y \
  python3-venv python3-pip python3-full \
  libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 \
  libgl1 libegl1 libgles2 libglib2.0-0 libsm6 libxext6 libxrender1 \
  mesa-utils libgbm1 \
  libavcodec-extra \
  ffmpeg
echo "    done."
echo ""

# ── 2. Virtualenv ─────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "[2/3] Creating Python virtualenv in ./venv/ ..."
  python3 -m venv venv
  echo "    done."
else
  echo "[2/3] Virtualenv already exists — skipping."
fi
echo ""

# ── 3. Python packages ────────────────────────────────────────────────
echo "[3/3] Installing pygame, opencv-python, numpy, moderngl ..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
echo "    done."
echo ""

echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Drop MP4 video loops into:"
echo "       $(pwd)/assets/clips/      (slots 1-0 on keyboard)"
echo "       $(pwd)/assets/overlays/   (slots Q-P on keyboard)"
echo ""
echo "  2. To launch, double-click one of:"
echo "       Start VJ.sh              — DUAL DISPLAY: control HUD on small"
echo "                                  screen, output fullscreen on projector"
echo "       Test (single screen).sh  — both windows on the primary display,"
echo "                                  for when no projector is connected"
echo ""
echo "  3. (Optional) Run 'Install Desktop Shortcuts.sh' to put"
echo "     clickable launcher icons on your Pi's desktop."
echo ""
read -p "Press Enter to close this window..."
