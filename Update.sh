#!/bin/bash
# pi-paint VJ — update from GitHub.
# Double-click this file in the file manager and choose "Execute in Terminal".
# Pulls the latest version from the `main` branch and updates Python
# dependencies if requirements.txt changed. Safe to re-run.
#
# Works two ways:
#   • If this folder is already a git clone: fast-forwards to origin/main.
#   • If you got the files as a ZIP download (no .git folder): it does a
#     one-time bootstrap by cloning the repo into a temp dir, moving the
#     .git folder into place, and force-syncing tracked files. Your own
#     MP4 clips in assets/clips/ and assets/overlays/ are untracked, so
#     they're left alone. Same for venv/ and vj_state.json.

set -e
cd "$(dirname "$0")"

# Tee everything to vj_last_update.log so the operator (or a debug
# request) can always check what the most recent update actually did.
#
# Implementation: re-exec self with stdout/stderr piped to tee.
# Previous version used `exec > >(tee -a "$LOG")` which leaves tee's
# file output block-buffered (~4 KB) — set -e mid-script then kills
# the subshell before tee flushes, and the disk log silently loses
# the tail (i.e. the actual error). The pipe-form below survives
# that: when set -e exits the inner shell, the pipe closes, tee gets
# EOF, flushes everything to the file, and exits cleanly. tee
# truncates the file on open (no -a) so a fresh run replaces stale
# content instead of growing forever.
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
  echo "[1/4] No .git folder found — this folder isn't a git checkout yet."
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

  # Move the freshly-cloned .git into place so this folder becomes a
  # real git checkout. Untracked files (your clips, venv, etc.) stay put.
  mv "$TMPDIR/repo/.git" ./.git
  rm -rf "$TMPDIR"
  trap - EXIT

  # Force-sync tracked files to origin/main. Untracked files are untouched.
  echo "    Syncing tracked files to latest main..."
  git fetch origin main
  git reset --hard origin/main
  git checkout main 2>/dev/null || git checkout -b main origin/main

  echo "    done. This folder is now a git checkout — future updates will be fast."
  echo ""
  STASHED=0
else
  # ── 1b. Already a git checkout — stash any local changes ────────────
  STASHED=0
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[1/4] Local changes detected — stashing them before update..."
    git stash push -u -m "Update.sh auto-stash $(date +%Y-%m-%d_%H:%M:%S)"
    STASHED=1
    echo "    done. (Restore later with: git stash pop)"
  else
    echo "[1/4] No local changes to stash."
  fi
  echo ""

  # ── 2. Remember requirements.txt hash before pulling ──────────────────
  OLD_REQ_HASH=""
  if [ -f requirements.txt ]; then
    OLD_REQ_HASH=$(sha1sum requirements.txt | awk '{print $1}')
  fi

  # ── 3. Fetch + fast-forward to origin/main ────────────────────────────
  echo "[2/4] Fetching latest from GitHub..."
  for attempt in 1 2 3 4; do
    if git fetch origin main; then
      break
    fi
    if [ "$attempt" -eq 4 ]; then
      echo "ERROR: could not fetch from origin after 4 attempts."
      echo "       Check your internet connection and try again."
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
  echo "[3/4] Updating to latest main (current branch: $CURRENT_BRANCH)..."
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

# ── 4. Update Python deps if requirements.txt changed ─────────────────
# (For the bootstrap path, OLD_REQ_HASH is unset, so we always run pip.)
NEW_REQ_HASH=""
if [ -f requirements.txt ]; then
  NEW_REQ_HASH=$(sha1sum requirements.txt | awk '{print $1}')
fi

if [ -d "venv" ] && [ "${OLD_REQ_HASH:-bootstrap}" != "$NEW_REQ_HASH" ]; then
  echo "[4/4] requirements.txt changed (or first run) — updating Python packages..."
  ./venv/bin/pip install --upgrade pip
  ./venv/bin/pip install -r requirements.txt
  echo "    done."
elif [ ! -d "venv" ]; then
  echo "[4/4] No venv found — run setup.sh to install dependencies."
else
  echo "[4/4] requirements.txt unchanged — skipping pip install."
fi
echo ""

# ── Done ──────────────────────────────────────────────────────────────
echo "============================================================"
echo "  Update complete!"
echo "============================================================"
echo ""
if [ "$STASHED" -eq 1 ]; then
  echo "NOTE: your local changes were stashed before the update."
  echo "      Restore them with:   git stash pop"
  echo ""
fi
echo "You can now double-click 'Start VJ.sh' to launch."
echo ""
read -p "Press Enter to close this window..."
