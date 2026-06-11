"""Face point-cloud base layer for the VJ rig.

A "face" here is a 3D point cloud baked from a single webcam capture by
`face_capture.py` (which runs the MediaPipe Face Mesh model via onnxruntime
in its OWN isolated venv — see `Capture Face.sh`). Each face is saved as a
tiny `.npz` holding:

    points  (N, 3) float32   — centred + radius-normalised, Y is DOWN (image
                               convention) so it renders upright
    colors  (N, 3) uint8     — RGB sampled from the capture, one per point

Crucially, NOTHING in this module needs the landmark model. The bake is offline;
at show time we only load numpy arrays and splat them, so the proven CPU app
gains a feature without gaining a dependency. Pure numpy/cv2 — no GL context,
so it stays well clear of the V3D dual-context landmine the hybrid is built
around.

Rendering is a software point splat: rotate the cloud (yaw/pitch/roll),
weak-perspective project, depth-shade so the far side dims, and scatter the
points into a black frame nearest-point-wins. A single front capture only
has data for the front of the head, so the engine clamps rotation to a
moderate yaw/pitch range — turn the head to catch an angle, never spin it a
full 360 into the hollow back.
"""
from pathlib import Path

import numpy as np


# Faces are stored radius-normalised to ~1.0; this is the fraction of the
# smaller canvas dimension the cloud spans at the default view.
_FIT = 0.42
# Weak-perspective focal distance (cloud radius units). Larger = flatter /
# more orthographic; smaller = stronger perspective. 3.2 gives a gentle,
# face-flattering amount of depth without warping.
_FOCAL = 3.2


class FaceCloud:
    """One baked face point cloud, renderable at any angle."""

    def __init__(self, points, colors, name="face"):
        # (N,3) float32 positions and (N,3) uint8 colours. Kept as plain
        # arrays; rotation/projection allocate fresh each frame (a few k
        # points — cheap, and avoids any aliasing between frames).
        self.points = np.ascontiguousarray(points, dtype=np.float32)
        self.colors = np.ascontiguousarray(colors, dtype=np.uint8)
        self.name = name

    @classmethod
    def load(cls, path):
        path = Path(path)
        with np.load(path) as data:
            pts = data["points"]
            cols = data["colors"]
        return cls(pts, cols, name=path.stem)

    @property
    def n_points(self):
        return self.points.shape[0]

    def render(self, w, h, yaw, pitch, roll=0.0, point_size=2,
               cx=None, cy=None, fit=_FIT, into=None):
        """Return an (h, w, 3) uint8 RGB frame of the cloud rotated to
        (yaw, pitch, roll) radians, on black.

        yaw   rotates about the vertical axis  (turn head left / right)
        pitch rotates about the horizontal axis(tip head up / down)
        roll  rotates in the image plane.

        cx, cy   pixel centre to project around (default frame centre) — lets
                 a caller place the face off-centre, e.g. two faces side by
                 side facing each other.
        fit      cloud span as a fraction of min(w, h) (default `_FIT`); pass
                 a smaller value to shrink the face so several fit at once.
        into     an existing (h, w, 3) frame to draw into instead of a fresh
                 black one — so multiple clouds can be composited together.
        """
        img = into if into is not None else np.zeros((h, w, 3), dtype=np.uint8)
        if cx is None:
            cx = w * 0.5
        if cy is None:
            cy = h * 0.5
        pts = self.points
        if pts.shape[0] == 0:
            return img

        # Build the combined rotation. Order: yaw (Y) → pitch (X) → roll (Z),
        # applied to column vectors, so R = Rz @ Rx @ Ry and we transform with
        # pts @ R.T.
        # NB: keep these trig locals distinct from the cx/cy CENTRE params.
        cosy, siny = np.cos(yaw), np.sin(yaw)
        cosx, sinx = np.cos(pitch), np.sin(pitch)
        cosz, sinz = np.cos(roll), np.sin(roll)
        Ry = np.array([[cosy, 0.0, siny], [0.0, 1.0, 0.0], [-siny, 0.0, cosy]],
                      dtype=np.float32)
        Rx = np.array([[1.0, 0.0, 0.0], [0.0, cosx, -sinx], [0.0, sinx, cosx]],
                      dtype=np.float32)
        Rz = np.array([[cosz, -sinz, 0.0], [sinz, cosz, 0.0], [0.0, 0.0, 1.0]],
                      dtype=np.float32)
        R = Rz @ Rx @ Ry
        rot = pts @ R.T  # (N,3)

        rx = rot[:, 0]
        ry = rot[:, 1]
        rz = rot[:, 2]  # depth: larger = farther from camera

        # Weak-perspective projection. persp shrinks far points slightly.
        persp = _FOCAL / (_FOCAL + rz)
        scale = min(w, h) * fit
        sx_px = cx + rx * scale * persp
        sy_px = cy + ry * scale * persp
        xs = sx_px.astype(np.int32)
        ys = sy_px.astype(np.int32)

        # Depth shade: near points full brightness, far points dimmed, so the
        # cloud reads as a volume rather than a flat sticker.
        zmin, zmax = float(rz.min()), float(rz.max())
        span = (zmax - zmin) or 1.0
        zn = (rz - zmin) / span                 # 0 near .. 1 far
        bright = (1.0 - 0.6 * zn)[:, None]       # 1.0 .. 0.4
        shaded = np.clip(self.colors.astype(np.float32) * bright, 0, 255
                         ).astype(np.uint8)

        # Painter's order: far → near, so the nearest point at each pixel is
        # written LAST and wins (numpy fancy-assign keeps the last write).
        order = np.argsort(-rz)
        xs, ys, shaded = xs[order], ys[order], shaded[order]

        img_flat = img.reshape(-1, 3)
        size = max(1, int(point_size))
        # Splat each point as a size×size block so a few-thousand-point cloud
        # reads as solid. Bounds are checked per offset; all offsets keep the
        # far→near order so nearer blocks overwrite farther ones.
        for dy in range(size):
            yy = ys + dy
            for dx in range(size):
                xx = xs + dx
                valid = (xx >= 0) & (xx < w) & (yy >= 0) & (yy < h)
                flat = yy[valid] * w + xx[valid]
                img_flat[flat] = shaded[valid]
        return img


class FacePool:
    """The library of baked faces in `faces_dir`. Mirrors the role ClipPool
    plays for clips, but trivially small: a face is a few hundred KB, so we
    just load each FaceCloud lazily and keep it cached for the session."""

    def __init__(self, faces_dir):
        self.dir = Path(faces_dir)
        self.files = self._scan()
        self.idx = 0
        self._cache = {}   # filename stem → FaceCloud

    def _scan(self):
        if not self.dir.exists():
            return []
        return sorted(p for p in self.dir.glob("*.npz") if p.is_file())

    def reload(self):
        """Rescan the folder (after a capture session adds new faces)."""
        cur = self.files[self.idx].stem if self.files else None
        self.files = self._scan()
        if cur is not None:
            for i, p in enumerate(self.files):
                if p.stem == cur:
                    self.idx = i
                    break
            else:
                self.idx = 0
        else:
            self.idx = 0

    def __len__(self):
        return len(self.files)

    def name(self, idx=None):
        if not self.files:
            return None
        if idx is None:
            idx = self.idx
        return self.files[idx % len(self.files)].stem

    def current(self):
        """Return the selected FaceCloud (lazy-loaded), or None if the
        library is empty / the file fails to load."""
        if not self.files:
            return None
        self.idx %= len(self.files)
        path = self.files[self.idx]
        cloud = self._cache.get(path.stem)
        if cloud is None:
            try:
                cloud = FaceCloud.load(path)
            except Exception as exc:  # noqa: BLE001
                print(f"[vj] facecloud: failed to load {path.name}: {exc!r}")
                return None
            self._cache[path.stem] = cloud
        return cloud

    def step(self, delta):
        """Advance the selection by delta (wraps). Returns the new name."""
        if not self.files:
            return None
        self.idx = (self.idx + delta) % len(self.files)
        return self.name()

    def peek(self, delta):
        """Return the FaceCloud `delta` slots from the current selection
        WITHOUT moving the selection (lazy-loaded + cached), or None. Used by
        the two-faces view to grab the partner face."""
        if not self.files:
            return None
        path = self.files[(self.idx + delta) % len(self.files)]
        cloud = self._cache.get(path.stem)
        if cloud is None:
            try:
                cloud = FaceCloud.load(path)
            except Exception as exc:  # noqa: BLE001
                print(f"[vj] facecloud: failed to load {path.name}: {exc!r}")
                return None
            self._cache[path.stem] = cloud
        return cloud
