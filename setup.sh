#!/bin/bash
# pi-paint VJ — one-time setup.
# Double-click this file in the file manager and choose "Execute in Terminal".
#
# Installs the GTK3 + GStreamer stack we need for the rewrite. Unlike
# the pygame/cv2 era, there is NO Python virtualenv any more — every
# dependency is a system apt package because PyGObject can't be
# meaningfully pip-installed. Safe to re-run.

set -e
cd "$(dirname "$0")"

echo ""
echo "============================================================"
echo "  pi-paint VJ — Setup"
echo "============================================================"
echo ""
echo "This installs (all via apt, no pip / venv):"
echo "  • GTK3 + PyGObject Python bindings"
echo "  • GStreamer 1.x + core/good/bad/libav/gl/gtk3 plugin sets"
echo "  • ffmpeg (for the HEVC re-encode step)"
echo ""
echo "You'll be prompted for your password (for 'sudo apt install')."
echo ""
read -p "Press Enter to begin, or Ctrl-C to cancel..."
echo ""

# ── 1. System packages ────────────────────────────────────────────────
# Notes on the choices:
#   * python3-gi + gir1.2-gtk-3.0 + gir1.2-gst-1.0  → GTK3 and
#     GStreamer accessible from Python via introspection.
#   * gstreamer1.0-gtk3 → the `gtksink` element, which exposes a
#     Gtk.Widget for video playback. Phase 1 needs exactly this.
#   * gstreamer1.0-libav → software H.264 decode (Pi 5 has no
#     hardware H.264 block, so this is the only path for non-HEVC
#     clips).
#   * gstreamer1.0-gl → GL upload/convert/sink elements, single
#     EGL context shared across the pipeline.
echo "[1/1] Installing system packages..."
sudo apt-get update
sudo apt-get install -y \
  python3 python3-gi python3-gi-cairo \
  gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 \
  gir1.2-gst-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-libav \
  gstreamer1.0-gl \
  gstreamer1.0-gtk3 \
  gstreamer1.0-x \
  ffmpeg \
  mpv \
  libgl1 libegl1
echo "    done."
echo ""

# Sanity check: confirm gtksink is reachable. If this fails, the
# pipeline can't build and the operator finds out at launch — better
# to surface it now.
if ! gst-inspect-1.0 gtksink >/dev/null 2>&1; then
  echo "WARNING: gst-inspect-1.0 can't find the 'gtksink' element."
  echo "         The package 'gstreamer1.0-gtk3' installed, but the"
  echo "         element didn't register. Try logging out + back in"
  echo "         so GST_PLUGIN_PATH picks up the new install."
fi

echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Drop MP4 video loops into:"
echo "       $(pwd)/assets/clips/      (slots 1-0 on keyboard)"
echo "       $(pwd)/assets/overlays/   (slots Q-P on keyboard)"
echo "     Hardware decode requires HEVC — re-encode H.264 clips"
echo "     via the asset processor (coming in a later phase)."
echo ""
echo "  2. To launch, double-click one of:"
echo "       Start VJ.sh              — DUAL DISPLAY: control HUD on small"
echo "                                  screen, output fullscreen on projector"
echo "       Test (single screen).sh  — both windows on the primary display,"
echo "                                  for when no projector is connected"
echo ""
read -p "Press Enter to close this window..."
