#!/bin/bash
cd "$(dirname "$0")"
lxterminal -e bash -c "claude --dangerously-skip-permissions; exec bash"