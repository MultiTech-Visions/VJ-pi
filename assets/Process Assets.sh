#!/bin/bash
# pi-paint VJ — process your 2K clips (HEVC, Pi-5 hardware-decode target).
#
# Reads raw landscape video from  assets/clips/  and bakes each to
# assets/clips_hevc/  as 2048x1152 HEVC — the format "Start VJ (2K HEVC).sh"
# plays with the Pi 5 hardware decoder. Already-baked clips are skipped.
#
# 2K-only shortcut for the unified processor. To do EVERYTHING (2K + portrait
# + 4K) in one pass, double-click "Process All Assets.sh" in this folder.
#
# NOTE: a big library bakes much faster on a PC — see pc_clip_baker/ — then
# upload the finished clips with "Upload from Phone.sh". On-Pi encoding works
# but is software (slow), best for a few field clips.

cd "$(dirname "$0")"
exec env VJ_ONLY=clips bash "Process All Assets.sh"
