"""Capture face point clouds for the VJ rig's face-cloud base layer.

Run via the `Capture Face.sh` launcher (double-click → Execute). It opens a
live preview of the USB webcam with the detected face mesh drawn on top;
press SPACE to bake the current face into `assets/faces/face_NNN.npz`, ESC
(or close the window) to finish. Capture as many faces as you like in one
session — they all land in the library the VJ app cycles through.

This is the ONLY place MediaPipe is used, and it runs in its own venv
(`venv_face/`), so the proven main app never takes on the dependency. The
bake output is plain numpy arrays (points + colours); the app loads those
with no extra deps. See `facecloud.py` for the runtime side.

What "baking" does: MediaPipe Face Mesh returns ~478 3D landmarks from one
RGB frame. 478 points alone look sparse, so we densify by interpolating
extra points along the mesh tessellation edges and sampling each point's
colour from the photo. The result (~8k coloured points) reads as a face
"scan" that holds up when rotated through a moderate yaw/pitch range.
"""
import sys
import time
from pathlib import Path

import numpy as np
import pygame

# MediaPipe is heavy to import; do it up front so a missing/broken install
# fails loudly (the launcher surfaces the traceback via zenity).
import mediapipe as mp

from camera import CameraSource


FACES_DIR = Path(__file__).resolve().parent / "assets" / "faces"
# Points added along each tessellation edge (interior, excluding endpoints).
# 3 → ~8k points total, a good density/speed balance for the Pi.
EDGE_SUBDIV = 3


def _next_face_path():
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        p = FACES_DIR / f"face_{n:03d}.npz"
        if not p.exists():
            return p
        n += 1


def _landmarks_to_arrays(landmarks, rgb):
    """Return (points Nx3 float32 in head space, pixel-coords Nx2) from one
    set of MediaPipe landmarks. Y is kept DOWN (image convention) so the
    cloud renders upright; X is aspect-corrected so the face isn't squashed.
    """
    h, w = rgb.shape[:2]
    aspect = w / float(h)
    xs = np.array([lm.x for lm in landmarks], dtype=np.float32)
    ys = np.array([lm.y for lm in landmarks], dtype=np.float32)
    zs = np.array([lm.z for lm in landmarks], dtype=np.float32)
    # 3D head-space coords. MediaPipe's z is roughly scaled like x (i.e. by
    # width), smaller = closer to camera; mirror that scaling here.
    P = np.stack([(xs - 0.5) * aspect, (ys - 0.5), zs * aspect], axis=1)
    # Pixel coords for colour sampling.
    px = np.clip(xs * w, 0, w - 1)
    py = np.clip(ys * h, 0, h - 1)
    pix = np.stack([px, py], axis=1)
    return P, pix


def _sample_colors(rgb, pix):
    h, w = rgb.shape[:2]
    xi = np.clip(pix[:, 0].astype(np.int32), 0, w - 1)
    yi = np.clip(pix[:, 1].astype(np.int32), 0, h - 1)
    return rgb[yi, xi].astype(np.uint8)


def _densify(P, C, edges):
    """Add interpolated points + colours along each mesh edge so the cloud
    is dense, not just the ~478 raw vertices."""
    pts = [P]
    cols = [C.astype(np.float32)]
    e = np.array(sorted(edges), dtype=np.int32)
    # Guard against any edge index outside the vertex count.
    e = e[(e[:, 0] < len(P)) & (e[:, 1] < len(P))]
    a, b = P[e[:, 0]], P[e[:, 1]]
    ca = C[e[:, 0]].astype(np.float32)
    cb = C[e[:, 1]].astype(np.float32)
    for k in range(1, EDGE_SUBDIV + 1):
        t = k / (EDGE_SUBDIV + 1.0)
        pts.append(a * (1.0 - t) + b * t)
        cols.append(ca * (1.0 - t) + cb * t)
    P_all = np.concatenate(pts, axis=0).astype(np.float32)
    C_all = np.clip(np.concatenate(cols, axis=0), 0, 255).astype(np.uint8)
    return P_all, C_all


def _normalize(P):
    """Centre on the centroid and scale to unit radius so every baked face
    is the same size regardless of how close the person sat to the camera."""
    P = P - P.mean(axis=0, keepdims=True)
    radius = float(np.linalg.norm(P, axis=1).max()) or 1.0
    return (P / radius).astype(np.float32)


def bake(rgb, landmarks, edges):
    """Turn one frame + landmark set into (points, colors) ready to save."""
    P, pix = _landmarks_to_arrays(landmarks, rgb)
    C = _sample_colors(rgb, pix)
    P, C = _densify(P, C, edges)
    P = _normalize(P)
    return P, C


def _surface_from_rgb(rgb):
    """pygame Surface from an (h, w, 3) RGB uint8 array."""
    h, w = rgb.shape[:2]
    return pygame.image.frombuffer(np.ascontiguousarray(rgb), (w, h), "RGB")


def main():
    print("[capture] Face point-cloud capture starting…", flush=True)
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

    mp_fm = mp.solutions.face_mesh
    edges = mp_fm.FACEMESH_TESSELATION
    face_mesh = mp_fm.FaceMesh(
        static_image_mode=False, refine_landmarks=True,
        max_num_faces=1, min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )

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

        results = face_mesh.process(rgb)
        have_face = bool(results.multi_face_landmarks)
        landmarks = (results.multi_face_landmarks[0].landmark
                     if have_face else None)

        # Preview: downscale the camera frame to the window and draw dots.
        preview = cv2.resize(rgb, (win_w, win_h), interpolation=cv2.INTER_AREA)
        surface = _surface_from_rgb(preview)
        screen.blit(surface, (0, 0))

        if have_face:
            h0, w0 = rgb.shape[:2]
            sxf, syf = win_w / w0, win_h / h0
            for lm in landmarks:
                x = int(lm.x * w0 * sxf)
                y = int(lm.y * h0 * syf)
                screen.fill((90, 230, 255), (x, y, 2, 2))

        # Capture on SPACE if a face is present.
        if capture_now:
            if have_face:
                P, C = bake(rgb, landmarks, edges)
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

        # HUD text.
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

    face_mesh.close()
    cam.release()
    pygame.quit()
    total = len(list(FACES_DIR.glob("*.npz"))) if FACES_DIR.exists() else 0
    print(f"[capture] done — saved {saved} this session, "
          f"{total} face(s) in library.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
