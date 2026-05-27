#!/bin/bash
# pi-paint VJ — update from GitHub.
# Double-click this file in the file manager and choose "Execute in Terminal".
# Pulls the latest from main and runs setup.sh again if its content changed.
#
# Works two ways:
#   • If this folder is already a git clone: fast-forwards to origin/main.
#   • If you got the files as a ZIP download (no .git folder): bootstraps
#     by cloning the repo into a temp dir, moving the .git folder into
#     place, and force-syncing tracked files. Your own MP4 clips in
#     assets/clips/ and assets/overlays/ are untracked, so they're left
#     alone. Same for vj_state.json and the log files.

set -e
cd "$(dirname "$0")"

# Self-tee re-exec so the log is reliable even when set -e exits the
# inner shell mid-script. The previous `exec > >(tee -a ...)` pattern
# left tee's file output block-buffered — on an aborted update the
# tail of the log silently vanished (which is exactly the part you
# needed to see). With the pipe form, the inner shell ending closes
# the pipe, tee gets EOF, flushes the file, and exits cleanly.
LOG="$(pwd)/vj_last_update.log"
if [ "${VJ_UPDATE_TEED:-0}" = "0" ]; then
    export VJ_UPDATE_TEED=1
    exec bash "$0" "$@" 2>&1 | tee "$LOG"
fi
date '+[VJ] update start: %Y-%m-%d %H:%M:%S'

REPO_URL="https://github.com/MultiTech-Visions/VJ-pi.git"

echo ""
echo "============================================================"
echo "  pi-paint VJ — Update"
echo "============================================================"
echo ""

# ── 0. Sanity checks ──────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: 'git' is not installed. Install it with:"
  echo "       sudo apt-get install -y git"
  read -p "Press Enter to close..."
  exit 1
fi

# ── 1. Bootstrap if this isn't a git checkout ─────────────────────────
if [ ! -d ".git" ]; then
  echo "[1/3] No .git folder found — this folder isn't a git checkout yet."
  echo "      Bootstrapping by cloning $REPO_URL ..."
  echo ""

  TMPDIR=$(mktemp -d)
  trap 'rm -rf "$TMPDIR"' EXIT

  for attempt in 1 2 3 4; do
    if git clone --branch main "$REPO_URL" "$TMPDIR/repo"; then
      break
    fi
    if [ "$attempt" -eq 4 ]; then
      echo "ERROR: could not clone $REPO_URL after 4 attempts."
      echo "       Check your internet connection and try again."
      read -p "Press Enter to close..."
      exit 1
    fi
    WAIT=$((2 ** attempt))
    echo "    clone failed — retrying in ${WAIT}s..."
    sleep "$WAIT"
  done

  mv "$TMPDIR/repo/.git" ./.git
  rm -rf "$TMPDIR"
  trap - EXIT

  echo "    Syncing tracked files to latest main..."
  git fetch origin main
  git reset --hard origin/main
  git checkout main 2>/dev/null || git checkout -b main origin/main

  echo "    done. This folder is now a git checkout — future updates will be fast."
  echo ""
  OLD_SETUP_HASH=""
else
  # ── 1b. Already a git checkout ────────────────────────────────────
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[1/3] Local changes detected — stashing them before update..."
    git stash push -u -m "Update.sh auto-stash $(date +%Y-%m-%d_%H:%M:%S)"
    echo "    done. (Restore later with: git stash pop)"
  else
    echo "[1/3] No local changes to stash."
  fi
  echo ""

  OLD_SETUP_HASH=""
  if [ -f setup.sh ]; then
    OLD_SETUP_HASH=$(sha1sum setup.sh | awk '{print $1}')
  fi

  echo "[2/3] Fetching latest from GitHub..."
  for attempt in 1 2 3 4; do
    if git fetch origin main; then
      break
    fi
    if [ "$attempt" -eq 4 ]; then
      echo "ERROR: could not fetch from origin after 4 attempts."
      read -p "Press Enter to close..."
      exit 1
    fi
    WAIT=$((2 ** attempt))
    echo "    fetch failed — retrying in ${WAIT}s..."
    sleep "$WAIT"
  done
  echo "    done."
  echo ""

  CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
  echo "[3/3] Updating to latest main (current branch: $CURRENT_BRANCH)..."
  if [ "$CURRENT_BRANCH" = "main" ]; then
    git pull --ff-only origin main
  else
    echo "    Not on 'main' (on '$CURRENT_BRANCH'). Switching to main..."
    git checkout main
    git pull --ff-only origin main
  fi
  echo "    done."
  echo ""
fi

# ── 2. Re-run setup.sh if it changed ──────────────────────────────────
# In the GStreamer era there's no requirements.txt to diff (all deps
# come from apt via setup.sh). If setup.sh's content changed,
# something in the apt list probably moved — re-run it so the
# operator doesn't trip over a missing package next launch.
NEW_SETUP_HASH=""
if [ -f setup.sh ]; then
  NEW_SETUP_HASH=$(sha1sum setup.sh | awk '{print $1}')
fi
if [ -n "$NEW_SETUP_HASH" ] && [ "$OLD_SETUP_HASH" != "$NEW_SETUP_HASH" ]; then
  echo "setup.sh changed since last run — you should re-run it:"
  echo "  bash setup.sh"
  echo ""
fi

# ── Done ──────────────────────────────────────────────────────────────
echo "============================================================"
echo "  Update complete!"
echo "============================================================"
echo ""
echo "You can now double-click 'Start VJ.sh' to launch."
echo ""
read -p "Press Enter to close this window..."
