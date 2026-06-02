#!/bin/bash
# Read-only: gathers the info needed to write a fullscreen-on-projector
# window rule (Wayland compositor, output names, config locations).
# Double-click → "Execute in Terminal", then screenshot the output or send
# vj_last_display.log.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_display.log"

{
  echo "==================== display setup ===================="
  echo "XDG_CURRENT_DESKTOP = $XDG_CURRENT_DESKTOP"
  echo "XDG_SESSION_TYPE    = $XDG_SESSION_TYPE"
  echo "WAYLAND_DISPLAY     = $WAYLAND_DISPLAY"
  echo
  echo "--- compositor process (which one is running) ---"
  ps -e -o comm= | grep -iE 'labwc|wayfire|weston|mutter|sway|kwin|cage' | sort -u \
    || echo "(none matched — unusual)"
  echo
  echo "--- outputs / monitors (wlr-randr) ---"
  if command -v wlr-randr >/dev/null 2>&1; then
    wlr-randr 2>&1
  else
    echo "wlr-randr not installed (install: sudo apt install wlr-randr)"
  fi
  echo
  echo "--- open Wayland windows (lswt, labwc only) ---"
  if command -v lswt >/dev/null 2>&1; then
    lswt 2>&1
  else
    echo "lswt not installed (labwc ships it; install: sudo apt install lswt)"
  fi
  echo
  echo "--- config files present ---"
  echo "labwc:   $(ls -d ~/.config/labwc 2>/dev/null || echo 'none')"
  ls -la ~/.config/labwc/ 2>/dev/null
  echo "wayfire: $(ls ~/.config/wayfire.ini 2>/dev/null || echo 'none')"
  echo "======================================================="
} 2>&1 | tee "$LOG"

echo
echo "Send Sam the output above (or vj_last_display.log)."
read -p "Press Enter to close..."
