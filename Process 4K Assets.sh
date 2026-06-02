#!/bin/bash
# Root-level launcher for the cinematic 4K asset processor.

cd "$(dirname "$0")"
exec bash "assets/Process 4K Assets.sh"
