#!/bin/bash
# Open Codex CLI on this repo in a terminal.
# Double-click in the file manager and choose "Execute".
#
# Codex is interactive (TUI) so we need a real terminal. This
# launcher finds one (lxterminal preferred, falls back through
# the usual suspects) and runs codex inside it, cd'd to the
# repo. First-time use: codex will prompt for OpenAI auth.
#
# If you'd rather use a terminal you already have open, just
# `cd` here and run `codex` yourself — that works identically.

cd "$(dirname "$0")"
REPO_DIR="$(pwd)"

CODEX_BIN="$HOME/.local/bin/codex"
if [ ! -x "$CODEX_BIN" ]; then
    CODEX_BIN="$(command -v codex 2>/dev/null)"
fi
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
    # No codex installed. Surface a clear error via zenity if
    # available, since this is a double-click context with no
    # console to print to.
    MSG="Codex CLI isn't installed.\n\nInstall it with:\n  curl -fsSL https://chatgpt.com/codex/install.sh | sh\n\nThen re-run this launcher."
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --width=520 --title="Codex not found" --text="$MSG"
    else
        printf '%b\n' "$MSG"
    fi
    exit 1
fi

# Build the command to run inside the terminal. We pre-load
# the repo directory and `codex` itself. If the user hasn't
# auth'd yet, codex will prompt them on first run — no need
# to gate on that here.
INNER_CMD="cd '$REPO_DIR' && exec '$CODEX_BIN'"

# Pick a terminal emulator and run codex in it. lxterminal
# is the Pi OS default, x-terminal-emulator is the Debian
# alternatives symlink that always exists if any terminal
# is installed.
for term in lxterminal x-terminal-emulator xterm gnome-terminal mate-terminal foot; do
    if command -v "$term" >/dev/null 2>&1; then
        case "$term" in
            lxterminal)
                exec "$term" --working-directory="$REPO_DIR" \
                    -e bash -c "$INNER_CMD; echo; read -p 'Codex exited. Press Enter to close.'"
                ;;
            xterm|x-terminal-emulator)
                exec "$term" -e bash -c "$INNER_CMD; echo; read -p 'Codex exited. Press Enter to close.'"
                ;;
            gnome-terminal|mate-terminal)
                exec "$term" --working-directory="$REPO_DIR" \
                    -- bash -c "$INNER_CMD; echo; read -p 'Codex exited. Press Enter to close.'"
                ;;
            foot)
                exec "$term" bash -c "cd '$REPO_DIR' && $INNER_CMD; echo; read -p 'Codex exited. Press Enter to close.'"
                ;;
        esac
    fi
done

# Nothing found. Last-resort message.
MSG="No terminal emulator available (tried lxterminal, x-terminal-emulator, xterm, gnome-terminal, mate-terminal, foot).\n\nOpen a terminal manually and run:\n  cd '$REPO_DIR' && codex"
if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=520 --title="No terminal" --text="$MSG"
else
    printf '%b\n' "$MSG"
fi
exit 1
