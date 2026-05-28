#!/bin/bash
# Compatibility launcher: the real processor lives at the repo root.
cd "$(dirname "$0")/.."
exec ./Process\ Assets.sh "$@"
