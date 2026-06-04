#!/bin/bash
# pi-paint VJ — turn portrait/vertical clips into landscape HEVC clips.
#
# Drop source videos into one of these, depending on how you want the tall
# frame fitted into 16:9 (each is processed differently):
#
#   assets/portrait/rotate/   spin 90° so a sideways-shot clip fills the frame
#   assets/portrait/crop/     fill 16:9 by cropping top & bottom (keep centre)
#   assets/portrait/          (loose files) blur-fill: whole frame centred,
#                             with a blurred copy filling the side bars
#
# Output goes to  assets/clips_hevc/<name>-landscape.mp4  (2048x1152 HEVC),
# ready to play with "Start VJ (2K HEVC).sh". Already-done clips are skipped.
#
# Portrait-only shortcut for the unified processor — to process everything in
# one pass, double-click "Process All Assets.sh" in this folder.

cd "$(dirname "$0")"
exec env VJ_ONLY=portrait bash "Process All Assets.sh"
