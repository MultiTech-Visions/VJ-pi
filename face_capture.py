"""Capture face point clouds for the VJ rig's face-cloud base layer.

Run via the `Capture Face.sh` launcher (double-click → Execute). It opens a
live preview of the USB webcam with the dense face mesh drawn on top; press
SPACE to bake the current face into `assets/faces/face_NNN.npz`, ESC (or
close the window) to finish. Capture as many faces as you like in one
session — they all land in the library the VJ app cycles through.

This is the ONLY place a face-landmark model is used, and it runs in its
own venv (`venv_face/`), so the proven main app never takes on the
dependency. The bake output is plain numpy arrays (points + colours); the
app loads those with no extra deps. See `facecloud.py` for the runtime side.

How a face is built:
  * InsightFace detects the face (bounding box). It runs natively here and
    is only used to find/scale the crop — not for landmarks.
  * The dense MediaPipe **Face Mesh** model (478 3D landmarks, forehead
    included) runs on that crop via onnxruntime, giving the same rich mesh
    the original capture used.
  * The 478 landmarks are Delaunay-triangulated and each triangle filled
    with a barycentric point grid → ~8k coloured points sampled from the
    photo, a face "scan" that holds up rotated through a moderate range.

Why this stack: the Pi runs Debian 13 / Python 3.13 on aarch64, for which
Google ships no MediaPipe *package* (and it doesn't support 3.13). But the
Face Mesh *model* runs fine as ONNX through onnxruntime, which installs
natively — so we keep the good dense mesh without the broken dependency.
"""
import sys
import time
from pathlib import Path

import numpy as np
import pygame
from scipy.spatial import Delaunay

import onnxruntime as ort
# InsightFace is heavy to import; do it up front so a missing/broken install
# fails loudly (the launcher surfaces the traceback via the log + zenity).
from insightface.app import FaceAnalysis

from camera import CameraSource


HERE = Path(__file__).resolve().parent
FACES_DIR = HERE / "assets" / "faces"
MESH_ONNX = HERE / "assets" / "models" / "face_mesh.onnx"

# Face Mesh model I/O: 256×256 RGB in [0,1] → 478 landmarks (x,y,z) in the
# crop's pixel space, plus a face-presence logit.
MESH_INPUT = 256
# Square crop = face box scaled by this (1.5 → ~50% margin), so the mesh has
# room to place the forehead/jaw landmarks.
CROP_SCALE = 1.5
# Barycentric grid order per mesh triangle (the 478 base is already dense, so
# a small subdivision reaches ~8k points). n → (n+1)(n+2)/2 points/triangle.
SUBDIV_N = 3
# Depth gain on the model's z before normalising. 1.0 keeps MediaPipe's
# native proportions; flip the sign if a baked face ever looks inside-out.
DEPTH_GAIN = 1.0


def _next_face_path():
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        p = FACES_DIR / f"face_{n:03d}.npz"
        if not p.exists():
            return p
        n += 1


# ── Detection + dense mesh ───────────────────────────────────────────────

def _largest_bbox(faces):
    def area(f):
        x0, y0, x1, y1 = f.bbox
        return (x1 - x0) * (y1 - y0)
    return max(faces, key=area).bbox


def _crop_square(rgb, bbox):
    """Square crop around the face box (with margin), border-replicated if it
    runs off the frame. Returns (crop, x0, y0, side) where (x0,y0) is the
    crop's top-left in full-image coords and side is its pixel size."""
    import cv2
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    half = max(x1 - x0, y1 - y0) * 0.5 * CROP_SCALE
    X0, Y0, side = cx - half, cy - half, 2.0 * half
    h, w = rgb.shape[:2]
    ix0, iy0 = int(round(X0)), int(round(Y0))
    iside = int(round(side))
    pad = max(0, -ix0, -iy0, ix0 + iside - w, iy0 + iside - h)
    if pad:
        rgb = cv2.copyMakeBorder(rgb, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    crop = rgb[iy0 + pad:iy0 + pad + iside, ix0 + pad:ix0 + pad + iside]
    return crop, float(ix0), float(iy0), float(iside)


def _mesh_landmarks(session, rgb, bbox):
    """Run the Face Mesh model on the cropped face; return 478×3 landmarks in
    full-image coords (x,y pixels, z proportional), or None if no face."""
    import cv2
    crop, x0, y0, side = _crop_square(rgb, bbox)
    if crop.size == 0:
        return None
    inp = cv2.resize(crop, (MESH_INPUT, MESH_INPUT)).astype(np.float32) / 255.0
    name = session.get_inputs()[0].name
    out = session.run(None, {name: inp[None, ...]})
    score = float(np.asarray(out[1]).ravel()[0])
    if score <= 0.0:                      # presence logit; >0 ≈ face present
        return None
    lm = np.asarray(out[0], dtype=np.float32).reshape(-1, 3)   # (478,3)
    # Map crop-space (0..256) back to the full image.
    s = side / MESH_INPUT
    lm[:, 0] = x0 + lm[:, 0] * s
    lm[:, 1] = y0 + lm[:, 1] * s
    lm[:, 2] = lm[:, 2] * s               # keep z proportional to x/y
    return lm


# ── Baking ───────────────────────────────────────────────────────────────

def _sample_colors(rgb, pix):
    h, w = rgb.shape[:2]
    xi = np.clip(pix[:, 0].astype(np.int32), 0, w - 1)
    yi = np.clip(pix[:, 1].astype(np.int32), 0, h - 1)
    return rgb[yi, xi].astype(np.uint8)


def _bary_grid(n):
    rows = [(i, j, n - i - j) for i in range(n + 1) for j in range(n + 1 - i)]
    return np.array(rows, dtype=np.float32) / float(n)


def _densify(P, C, tris):
    """Fill every mesh triangle with a barycentric grid of points + colours."""
    if len(tris) == 0:
        return P, C.astype(np.uint8)
    B = _bary_grid(SUBDIV_N)
    pts = np.einsum("mb,tbc->tmc", B, P[tris]).reshape(-1, 3)
    cols = np.einsum("mb,tbc->tmc", B, C[tris].astype(np.float32)).reshape(-1, 3)
    return pts.astype(np.float32), np.clip(cols, 0, 255).astype(np.uint8)


def _normalize(P):
    """Centre on the centroid and scale to unit radius so every baked face is
    the same size regardless of how close the person sat to the camera."""
    P = P - P.mean(axis=0, keepdims=True)
    radius = float(np.linalg.norm(P, axis=1).max()) or 1.0
    return (P / radius).astype(np.float32)


def bake(rgb, lm):
    """Turn one frame + 478 landmarks into (points, colors) ready to save."""
    h, w = rgb.shape[:2]
    aspect = w / float(h)
    # Head space: Y kept DOWN so it renders upright; X aspect-corrected; z in
    # the same per-pixel scale as x/y for natural relief.
    X = (lm[:, 0] / w - 0.5) * aspect
    Y = (lm[:, 1] / h - 0.5)
    Z = DEPTH_GAIN * (lm[:, 2] / h)
    P = np.stack([X, Y, Z], axis=1).astype(np.float32)
    C = _sample_colors(rgb, lm[:, :2])
    tris = Delaunay(lm[:, :2]).simplices.astype(np.int32)
    P, C = _densify(P, C, tris)
    return _normalize(P), C


# ── pygame loop ──────────────────────────────────────────────────────────

def _surface_from_rgb(rgb):
    h, w = rgb.shape[:2]
    return pygame.image.frombuffer(np.ascontiguousarray(rgb), (w, h), "RGB")


def main():
    print("[capture] Face point-cloud capture starting…", flush=True)

    if not MESH_ONNX.exists():
        print(f"[capture] ERROR: face mesh model missing at {MESH_ONNX}",
              flush=True)
        return 3
    mesh = ort.InferenceSession(str(MESH_ONNX),
                                providers=["CPUExecutionProvider"])

    print("[capture] loading InsightFace detector "
          "(first run downloads ~300 MB)…", flush=True)
    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"],
                       providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))
    print("[capture] models ready.", flush=True)

    cam = CameraSource(request_size=(1280, 720), mirror=True)
    if not cam.start():
        print("[capture] ERROR: no working webcam found "
              f"({cam.error or 'unknown'}).", flush=True)
        return 2
    print(f"[capture] camera live on /dev/video{cam.opened_index}", flush=True)

    pygame.init()
    pygame.font.init()
    win_w, win_h = 960, 540
    screen = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption("VJ-pi — Capture Face  (SPACE = capture, "
                               "ESC = done)")
    font = pygame.font.SysFont("DejaVu Sans", 22)
    big = pygame.font.SysFont("DejaVu Sans", 30, bold=True)

    saved = 0
    flash_until = 0.0
    flash_msg = ""
    import cv2  # local — only needed for the preview resize

    clock = pygame.time.Clock()
    running = True
    while running:
        capture_now = False
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key == pygame.K_SPACE:
                    capture_now = True

        rgb = cam.read()
        if rgb is None:
            screen.fill((20, 20, 28))
            msg = big.render("waiting for camera…", True, (220, 220, 230))
            screen.blit(msg, (40, win_h // 2 - 20))
            pygame.display.flip()
            clock.tick(30)
            continue

        faces = app.get(rgb[:, :, ::-1])         # InsightFace wants BGR
        lm = None
        if faces:
            lm = _mesh_landmarks(mesh, rgb, _largest_bbox(faces))
        have_face = lm is not None

        preview = cv2.resize(rgb, (win_w, win_h), interpolation=cv2.INTER_AREA)
        screen.blit(_surface_from_rgb(preview), (0, 0))

        if have_face:
            h0, w0 = rgb.shape[:2]
            sxf, syf = win_w / w0, win_h / h0
            for (lx, ly) in lm[:, :2]:
                screen.fill((90, 230, 255), (int(lx * sxf), int(ly * syf), 1, 1))

        if capture_now:
            if have_face:
                P, C = bake(rgb, lm)
                path = _next_face_path()
                np.savez_compressed(path, points=P, colors=C)
                saved += 1
                flash_msg = f"Saved {path.name}  ({len(P)} points)"
                flash_until = time.time() + 2.0
                print(f"[capture] {flash_msg}", flush=True)
            else:
                flash_msg = "No face detected — face the camera and retry"
                flash_until = time.time() + 2.0
                print(f"[capture] {flash_msg}", flush=True)

        status = ("FACE DETECTED — press SPACE to capture" if have_face
                  else "no face — look at the camera")
        color = (120, 255, 160) if have_face else (255, 180, 90)
        screen.blit(font.render(status, True, color), (16, 12))
        screen.blit(font.render(f"saved this session: {saved}",
                                True, (210, 210, 220)), (16, 40))
        screen.blit(font.render("SPACE capture   ·   ESC done",
                                True, (180, 180, 200)), (16, win_h - 32))
        if time.time() < flash_until:
            box = big.render(flash_msg, True, (255, 255, 255))
            r = box.get_rect(center=(win_w // 2, win_h - 80))
            screen.fill((20, 20, 30), r.inflate(20, 14))
            screen.blit(box, r)

        pygame.display.flip()
        clock.tick(30)

    cam.release()
    pygame.quit()
    total = len(list(FACES_DIR.glob("*.npz"))) if FACES_DIR.exists() else 0
    print(f"[capture] done — saved {saved} this session, "
          f"{total} face(s) in library.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
