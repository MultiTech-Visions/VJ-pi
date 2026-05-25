#!/bin/bash
# pi-paint VJ — update from GitHub.
# Double-click this file in the file manager and choose "Execute in Terminal".
# Pulls the latest version from the `main` branch and updates Python
# dependencies if requirements.txt changed. Safe to re-run.

set -e
cd "$(dirname "$0")"

echo ""
echo "============================================================"
echo "  pi-paint VJ — Update"
echo "============================================================"
echo ""

# ── 0. Sanity checks ──────────────────────────────────────────────────
if [ ! -d ".git" ]; then
  echo "ERROR: this folder is not a git checkout — nothing to update."
  echo "       (You'd need to re-clone from GitHub to get updates.)"
  read -p "Press Enter to close..."
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: 'git' is not installed. Install it with:"
  echo "       sudo apt-get install -y git"
  read -p "Press Enter to close..."
  exit 1
fi

# ── 1. Stash any local changes ────────────────────────────────────────
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

# ── 2. Remember current requirements.txt hash ─────────────────────────
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

# ── 4. Update Python deps if requirements.txt changed ─────────────────
NEW_REQ_HASH=""
if [ -f requirements.txt ]; then
  NEW_REQ_HASH=$(sha1sum requirements.txt | awk '{print $1}')
fi

if [ -d "venv" ] && [ "$OLD_REQ_HASH" != "$NEW_REQ_HASH" ]; then
  echo "[4/4] requirements.txt changed — updating Python packages..."
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
