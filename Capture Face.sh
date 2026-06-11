#!/bin/bash
# pi-paint VJ — face point-cloud capture.
# Double-click in the file manager and choose "Execute".
#
# Opens a live webcam preview with the detected face landmarks drawn on top.
#   SPACE   capture the current face → assets/faces/face_NNN.npz
#   ESC     finish (capture as many as you like in one session)
#
# The captured faces show up in the VJ app as the face-cloud base layer:
# press the ` (backtick) key to toggle it, and , / . to cycle through faces.
#
# This tool uses InsightFace (to find the face) plus the MediaPipe Face Mesh
# model (the dense 478-point mesh, run via onnxruntime), installed into its
# OWN virtualenv (venv_face/) the first time you run it — so the main VJ app
# is never touched. That first run downloads ~a few hundred MB and can take
# several minutes; a progress window shows while it works.

cd "$(dirname "$0")"
LOG="$(pwd)/vj_last_capture.log"
FACE_VENV="venv_face"

show_dialog() {
  # $1 = kind (error|info), $2 = title, $3 = body
  local kind="$1" title="$2" body="$3"
  if command -v zenity >/dev/null 2>&1; then
    zenity --"$kind" --width=720 --title="$title" --text="$body" 2>/dev/null
    return
  fi
  if command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
    return
  fi
  echo "$title"; echo "$body"
}

: >"$LOG"
date '+[VJ] face-capture launch: %Y-%m-%d %H:%M:%S' >>"$LOG"
git -C "$(pwd)" log --oneline -1 2>/dev/null >>"$LOG"

# ── Ensure the isolated capture venv exists with InsightFace installed ────
need_install=0
if [ ! -x "$FACE_VENV/bin/python" ]; then
  echo "[VJ] creating $FACE_VENV ..." >>"$LOG"
  python3 -m venv "$FACE_VENV" >>"$LOG" 2>&1 || {
    show_dialog error "VJ-pi: face setup failed" \
      "Could not create the Python venv for face capture.\n\nLog: $LOG"
    exit 1
  }
  need_install=1
fi
if ! "$FACE_VENV/bin/python" -c "import insightface, scipy, onnxruntime" >/dev/null 2>&1; then
  need_install=1
fi

if [ "$need_install" -eq 1 ]; then
  echo "[VJ] installing face-capture deps (this can take a few minutes)..." >>"$LOG"
  install_cmd() {
    "$FACE_VENV/bin/pip" install --upgrade pip 2>&1
    "$FACE_VENV/bin/pip" install -r requirements-face.txt 2>&1
  }
  if command -v zenity >/dev/null 2>&1; then
    # Stream pip output through a pulsating progress dialog; tee to the log.
    install_cmd | tee -a "$LOG" | \
      zenity --progress --pulsate --auto-close --no-cancel --width=520 \
        --title="VJ-pi: first-time face-capture setup" \
        --text="Installing InsightFace (one-time, a few minutes)…" 2>/dev/null
    rc=${PIPESTATUS[0]}
  else
    install_cmd >>"$LOG" 2>&1
    rc=$?
  fi
  if [ "$rc" -ne 0 ] || ! "$FACE_VENV/bin/python" -c "import insightface, scipy, onnxruntime" >/dev/null 2>&1; then
    TAIL=$(tail -30 "$LOG" 2>/dev/null)
    show_dialog error "VJ-pi: face-capture install failed" \
      "Installing InsightFace failed.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
    exit 1
  fi
fi

# ── Ensure the landmark model is downloaded (first run pulls ~300 MB) ──────
# InsightFace fetches the model the first time it runs; do it here, under a
# progress dialog, so the operator sees something happen instead of the app
# appearing to hang on a silent download.
MODEL_DIR="$HOME/.insightface/models/buffalo_l"
if [ ! -d "$MODEL_DIR" ]; then
  echo "[VJ] downloading InsightFace model (one-time, ~300 MB)..." >>"$LOG"
  warm_cmd() {
    "$FACE_VENV/bin/python" - <<'PY' 2>&1
from insightface.app import FaceAnalysis
app = FaceAnalysis(
    name="buffalo_l",
    allowed_modules=["detection"],
    providers=["CPUExecutionProvider"],
)
app.prepare(ctx_id=-1, det_size=(640, 640))
print("[VJ] model ready")
PY
  }
  if command -v zenity >/dev/null 2>&1; then
    warm_cmd | tee -a "$LOG" | \
      zenity --progress --pulsate --auto-close --no-cancel --width=520 \
        --title="VJ-pi: first-time face-model download" \
        --text="Downloading the face model (one-time, ~300 MB)…" 2>/dev/null
  else
    warm_cmd >>"$LOG" 2>&1
  fi
  if [ ! -d "$MODEL_DIR" ]; then
    TAIL=$(tail -30 "$LOG" 2>/dev/null)
    show_dialog error "VJ-pi: face-model download failed" \
      "Downloading the InsightFace model failed.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
    exit 1
  fi
fi

# ── Ensure the dense Face Mesh model is present (ships in the repo; this is
#    a safety net for a fresh clone where it's somehow missing) ────────────
MESH_ONNX="assets/models/face_mesh.onnx"
MESH_URL="https://huggingface.co/astaileyyoung/FaceMeshONNX/resolve/main/mesh.onnx"
if [ ! -f "$MESH_ONNX" ]; then
  echo "[VJ] fetching face mesh model..." >>"$LOG"
  mkdir -p "$(dirname "$MESH_ONNX")"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$MESH_URL" -o "$MESH_ONNX" >>"$LOG" 2>&1
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$MESH_URL" -O "$MESH_ONNX" >>"$LOG" 2>&1
  fi
  if [ ! -s "$MESH_ONNX" ]; then
    rm -f "$MESH_ONNX"
    TAIL=$(tail -20 "$LOG" 2>/dev/null)
    show_dialog error "VJ-pi: face mesh model missing" \
      "Could not obtain the face mesh model.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
    exit 1
  fi
fi

# ── Run the capture tool ─────────────────────────────────────────────────
"$FACE_VENV/bin/python" face_capture.py >>"$LOG" 2>&1
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
  TAIL=$(tail -40 "$LOG" 2>/dev/null)
  show_dialog error "VJ-pi: face capture crashed (exit $EXIT)" \
    "face_capture.py exited with status $EXIT.\n\nFull log: $LOG\n\nLast lines:\n\n$TAIL"
  exit "$EXIT"
fi

SUMMARY=$(grep -E '^\[capture\] done' "$LOG" | tail -1)
COUNT=$(ls assets/faces/*.npz 2>/dev/null | wc -l | tr -d ' ')
show_dialog info "VJ-pi: face capture finished" \
  "${SUMMARY:-Capture finished.}\n\nFaces in library: ${COUNT}\n\nIn the VJ app, press the \` key to show the face cloud, and , / . to cycle through faces."
exit 0
