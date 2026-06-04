#!/bin/bash
# pi-paint VJ — process raw 4K/cinematic clips.
#
# Reads raw hi-res video from  assets/4k/  and writes Pi-5-playable HEVC
# (<=3840x2160, <=30fps, no audio) to  assets/4k/processed/  for cinematic
# mode (press N in the app). Clips already in that format are stream-copied;
# already-processed ones are skipped.
#
# 4K-only shortcut for the unified processor. To do EVERYTHING (2K + portrait
# + 4K) in one pass, double-click "Process All Assets.sh" in this folder.

cd "$(dirname "$0")"
exec env VJ_ONLY=4k bash "Process All Assets.sh"
