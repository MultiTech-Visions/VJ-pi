#!/bin/bash
# pi-paint VJ — update from GitHub.
# Double-click in the file manager and choose "Execute" (or "Execute
# in Terminal" if you want to watch it live).
#
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
#
# Failure reporting: when launched via "Execute" the file manager
# gives this script no terminal, so the old `read -p "Press Enter
# to close..."` prompts EOF-ed instantly and the window vanished
# without showing anything. We now tee all output to vj_update.log
# and pop a scrollable zenity dialog on exit (success or failure)
# so the operator always sees what happened.

set -e
cd "$(dirname "$0")"

LOG="$(pwd)/vj_update.log"

if [ -t 1 ]; then
  FROM_TERMINAL=1
else
  FROM_TERMINAL=0
  # Mirror every byte of stdout+stderr into the log so the GUI dialog
  # can show what happened. tee makes the output visible to a terminal
  # too if one happens to be attached upstream (unlikely with file
  # manager launch but harmless).
  exec > >(tee "$LOG") 2>&1
fi

show_log_dialog() {
  local title="$1"
  local logfile="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --text-info --title="$title" --filename="$logfile" \
      --width=900 --height=600 --no-wrap 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    xmessage -file "$logfile" -title "$title" 2>/dev/null
    return
  fi
  for term in lxterminal xterm gnome-terminal mate-terminal x-terminal-emulator; do
    if command -v "$term" >/dev/null 2>&1; then
      "$term" -e bash -c "less '$logfile'; read -p 'Press Enter to close...'"
      return
    fi
  done
}

# Single exit handler — fires on success, error, or `set -e` blowup.
# Cleans up any half-finished bootstrap TMPDIR and surfaces the result.
TMPDIR=""
on_exit() {
  local code=$?
  if [ -n "$TMPDIR" ] && [ -d "$TMPDIR" ]; then
    rm -rf "$TMPDIR"
  fi
  if [ "$FROM_TERMINAL" = "1" ]; then
    if [ "$code" -ne 0 ]; then
      echo ""
      echo "Update FAILED (exit $code) — see error above."
    fi
    read -p "Press Enter to close..." 2>/dev/null || true
  else
    local title
    if [ "$code" -eq 0 ]; then
      title="VJ-pi: Update complete — $LOG"
    else
      title="VJ-pi: Update FAILED (exit $code) — $LOG"
    fi
    show_log_dialog "$title" "$LOG"
  fi
}
trap on_exit EXIT

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
  exit 1
fi

# ── 1. Bootstrap if this isn't a git checkout ─────────────────────────
if [ ! -d ".git" ]; then
  echo "[1/4] No .git folder found — this folder isn't a git checkout yet."
  echo "      Bootstrapping by cloning $REPO_URL ..."
  echo ""

  TMPDIR=$(mktemp -d)

  for attempt in 1 2 3 4; do
    if git clone --branch main "$REPO_URL" "$TMPDIR/repo"; then
      break
    fi
    if [ "$attempt" -eq 4 ]; then
      echo "ERROR: could not clone $REPO_URL after 4 attempts."
      echo "       Check your internet connection and try again."
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
  TMPDIR=""

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
