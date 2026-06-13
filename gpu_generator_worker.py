"""Out-of-process GStreamer/GL generator renderer."""
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from shader_catalog import GPU_GENERATORS, IMAGE_GENERATORS


HERE = Path(__file__).resolve().parent
IMAGES_DIR = HERE / "assets" / "images"

# Slideshow tuning (env-overridable). The cube turns like a carousel; each
# side face is swapped to the next photo the moment it rotates to the rear
# (out of sight), so images only ever change on the hidden back.
CUBE_SPIN_SPEED = float(os.environ.get("VJ_CUBE_SPIN", "0.25"))  # rad/s base
# Constant forward tilt of the cube. MUST match TILT in CUBE_SHADER.
CUBE_TILT = -0.30


def _list_images():
    if not IMAGES_DIR.exists():
        return []
    paths = []
    for suffix in ("*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.JPEG"):
        paths.extend(IMAGES_DIR.glob(suffix))
    return sorted(paths)


_cell_cache = {}  # (path, cw, ch) -> RGBA cell, so swaps/re-activations are cheap


def _load_cell(path, cw, ch):
    """Decode an image and letterbox it (no distortion) into a cw x ch RGBA
    cell on a black background. Decodes at reduced resolution (the JPEGs are
    12MP phone photos; a cube cell is a few hundred px) and caches the result
    so a face swap never stalls the render thread."""
    ck = (str(path), cw, ch)
    cached = _cell_cache.get(ck)
    if cached is not None:
        return cached
    cell = np.zeros((ch, cw, 4), dtype=np.uint8)
    cell[:, :, 3] = 255
    # REDUCED_COLOR_4 decodes at 1/4 size (1/16 the pixels) — far faster and
    # still well above a cube face's resolution.
    img = cv2.imread(str(path), cv2.IMREAD_REDUCED_COLOR_4)  # BGR, 1/4 size
    if img is None:
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return cell
    ih, iw = img.shape[:2]
    if iw == 0 or ih == 0:
        return cell
    scale = min(cw / iw, ch / ih)
    nw = max(1, int(round(iw * scale)))
    nh = max(1, int(round(ih * scale)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    x0 = (cw - nw) // 2
    y0 = (ch - nh) // 2
    cell[y0:y0 + nh, x0:x0 + nw, 0] = resized[:, :, 2]  # R
    cell[y0:y0 + nh, x0:x0 + nw, 1] = resized[:, :, 1]  # G
    cell[y0:y0 + nh, x0:x0 + nw, 2] = resized[:, :, 0]  # B
    _cell_cache[ck] = cell
    return cell


def _cube_face_normals(spin):
    """World-space outward normals of the 6 cube faces under the SAME
    rotation the shader applies: rotX(CUBE_TILT) * rotY(spin). Face order
    matches the shader: +X,-X,+Y,-Y,+Z,-Z. Returns a (6,3) array."""
    cy, sy = math.cos(spin), math.sin(spin)
    cx, sx = math.cos(CUBE_TILT), math.sin(CUBE_TILT)
    ry = np.array([[cy, 0.0, -sy], [0.0, 1.0, 0.0], [sy, 0.0, cy]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]])
    r = rx @ ry
    local = np.array([
        [1.0, 0.0, 0.0], [-1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0], [0.0, -1.0, 0.0],
        [0.0, 0.0, 1.0], [0.0, 0.0, -1.0],
    ])
    return local @ r.T


class CubeSlideshow:
    """Rolling 3x2 image atlas for the picture-cube generator.

    Holds one photo per cube face and accumulates the carousel angle. The
    moment a face rotates to the rear (becomes the most hidden — largest
    world-normal +z, pointing away from the -z camera) it is swapped to the
    next photo, while it is out of sight. So a visible face never changes
    under your eyes; new pictures appear only as fresh faces swing around.
    The cycle walks the entire images/ folder, reshuffling on each full pass,
    for a set-and-forget slideshow.
    """

    def __init__(self, width, height):
        self.width = width
        self.height = height
        # Integer cell boundaries that exactly tile width x height (3 cols,
        # 2 rows); the shader maps faces to thirds/halves of the texture.
        self.col_x = [(c * width) // 3 for c in range(4)]
        self.row_y = [(r * height) // 2 for r in range(3)]
        self.atlas = np.zeros((height, width, 4), dtype=np.uint8)
        self.atlas[:, :, 3] = 255
        self.spin = 0.0
        self.last_t = time.monotonic()
        self.rear_face = -1   # which face is currently at the rear
        self.cycle = []
        self.cycle_idx = 0
        self.face_paths = [None] * 6
        self.reset()

    def _next_path(self):
        imgs = _list_images()
        if not imgs:
            return None
        if self.cycle_idx >= len(self.cycle) or sorted(self.cycle) != imgs:
            self.cycle = imgs[:]
            random.shuffle(self.cycle)
            self.cycle_idx = 0
        path = self.cycle[self.cycle_idx % len(self.cycle)]
        self.cycle_idx += 1
        return path

    def _draw_face(self, face, path):
        if path is None:
            return
        c, r = face % 3, face // 3
        cw = self.col_x[c + 1] - self.col_x[c]
        ch = self.row_y[r + 1] - self.row_y[r]
        cell = _load_cell(path, cw, ch)
        self.atlas[self.row_y[r]:self.row_y[r + 1],
                   self.col_x[c]:self.col_x[c + 1]] = cell
        self.face_paths[face] = path

    def reset(self):
        """Fresh shuffle + repaint all six faces (called on (re)activation)."""
        self.spin = 0.0
        self.last_t = time.monotonic()
        self.cycle = []
        self.cycle_idx = 0
        if _list_images():
            for face in range(6):
                self._draw_face(face, self._next_path())
        # Remember the starting rear face so we don't swap it immediately.
        self.rear_face = int(np.argmax(_cube_face_normals(self.spin)[:, 2]))

    def step(self, param_x):
        """Advance the carousel angle. The instant a new face reaches the rear
        (out of sight), swap it to the next photo. Returns the spin angle for
        the cube_spin uniform."""
        now = time.monotonic()
        dt = min(0.2, max(0.0, now - self.last_t))
        self.last_t = now
        # param_x = 0 -> ~0.04 rad/s (a revolution every ~2.5 min), the slow
        # floor; param_x = 1 -> ~0.29 rad/s (~22s). Default 0.5 -> ~40s.
        speed = CUBE_SPIN_SPEED * (0.15 + max(0.0, min(1.0, param_x)))
        self.spin += dt * speed
        # Rear-most face = largest world +z normal (pointing away from camera).
        rear = int(np.argmax(_cube_face_normals(self.spin)[:, 2]))
        if rear != self.rear_face:
            self.rear_face = rear
            if _list_images():
                self._draw_face(rear, self._next_path())
        return self.spin

    def buffer_bytes(self):
        return self.atlas.tobytes()


class Renderer:
    def __init__(self):
        Gst.init(None)
        self.pipeline = None
        self.shader = None
        self.sink = None
        self.src = None       # appsrc, for image/atlas generators
        self.cube = None      # CubeSlideshow when current generator is one
        self.current = None

    def close(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.shader = None
        self.sink = None
        self.src = None
        self.cube = None
        self.current = None

    def pause(self):
        # Stop the GL pipeline from churning V3D while nobody is pulling
        # frames (e.g. during blackout / freeze). PAUSED keeps the built
        # pipeline so render() can resume instantly without a rebuild.
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.PAUSED)

    def ensure(self, name, width, height, token):
        is_image = name in IMAGE_GENERATORS
        key = (name, width, height, token if is_image else None)
        if self.current == key and self.pipeline is not None:
            return
        self.close()
        shader = GPU_GENERATORS[name]
        if is_image:
            if not _list_images():
                raise RuntimeError(f"{name} needs an image in assets/images/")
            # Feed a worker-composed RGBA atlas frame-by-frame via appsrc, so
            # the slideshow can swap face images live without rebuilding the
            # GL pipeline. The shader maps cube faces to atlas cells.
            self.cube = CubeSlideshow(width, height)
            print(f"[gpu-worker] cube slideshow: {len(_list_images())} images",
                  file=sys.stderr, flush=True)
            desc = (
                "appsrc name=src is-live=true do-timestamp=true format=time "
                "block=true max-bytes=0 ! "
                f"video/x-raw,format=RGBA,width={width},height={height},framerate=30/1 ! "
                "videoconvert ! glupload ! glshader name=shader ! "
                "gldownload ! videoconvert ! "
                "video/x-raw,format=RGB ! "
                "appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
            )
        else:
            desc = (
                "videotestsrc is-live=true pattern=black ! "
                f"video/x-raw,width={width},height={height},framerate=30/1 ! "
                "glupload ! glshader name=shader ! "
                "gldownload ! videoconvert ! "
                "video/x-raw,format=RGB ! "
                "appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
            )
        self.pipeline = Gst.parse_launch(desc)
        self.shader = self.pipeline.get_by_name("shader")
        self.sink = self.pipeline.get_by_name("sink")
        self.src = self.pipeline.get_by_name("src")
        self.shader.set_property("fragment", shader)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.current = key

    def render(self, name, width, height, token, param_x=0.5, param_y=0.5):
        if name not in GPU_GENERATORS:
            raise ValueError(f"unknown GPU generator: {name}")
        self.ensure(name, width, height, token)
        # Resume if we were paused (blackout/freeze); a no-op if already
        # PLAYING.
        self.pipeline.set_state(Gst.State.PLAYING)
        # Image/atlas generators (the cube) feed their texture via appsrc:
        # advance the slideshow, then push the current atlas frame so glshader
        # has an input buffer to run over. cube_spin keeps the shader's tumble
        # in lockstep with the worker's hidden-face picker.
        cube_spin = 0.0
        if self.cube is not None and self.src is not None:
            cube_spin = self.cube.step(float(param_x))
            buf = Gst.Buffer.new_wrapped(self.cube.buffer_bytes())
            ret = self.src.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                raise RuntimeError(f"appsrc push-buffer returned {ret!r}")
        # Push the live 0..1 params as custom GLSL uniforms. glshader keeps
        # its built-in time/width/height/tex regardless, and a shader that
        # doesn't declare param_x/param_y just ignores them (unknown uniform
        # locations are silent no-ops), so this is safe for every generator.
        try:
            st = Gst.Structure.new_from_string(
                "uniforms,param_x=(gfloat){:.6f},param_y=(gfloat){:.6f},"
                "cube_spin=(gfloat){:.6f}".format(
                    float(param_x), float(param_y), float(cube_spin)))
            if st is not None:
                self.shader.set_property("uniforms", st)
        except Exception as exc:
            print(f"[gpu-worker] uniform push failed: {exc!r}",
                  file=sys.stderr, flush=True)
        sample = None
        for _ in range(4):
            sample = self.sink.emit("try-pull-sample", 1 * Gst.SECOND)
            if sample is not None:
                break
        if sample is None:
            bus = self.pipeline.get_bus()
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
            if msg is not None and msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                raise RuntimeError(f"{err.message}; {dbg}")
            if msg is not None and msg.type == Gst.MessageType.WARNING:
                err, dbg = msg.parse_warning()
                raise RuntimeError(f"{err.message}; {dbg}")
            raise RuntimeError("appsink returned no sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("failed to map GStreamer buffer")
        try:
            return bytes(info.data)
        finally:
            buf.unmap(info)


def _send(obj, payload=None):
    out = sys.stdout.buffer
    out.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    if payload:
        out.write(payload)
    out.flush()


def main():
    renderer = Renderer()
    try:
        for line in sys.stdin.buffer:
            try:
                req = json.loads(line.decode("utf-8"))
                if req.get("cmd") == "pause":
                    renderer.pause()
                    _send({"ok": True, "paused": True})
                    continue
                name = str(req["name"])
                width = int(req["width"])
                height = int(req["height"])
                token = int(req.get("token", 0))
                param_x = float(req.get("param_x", 0.5))
                param_y = float(req.get("param_y", 0.5))
                data = renderer.render(name, width, height, token,
                                       param_x, param_y)
                _send({"ok": True, "width": width, "height": height, "n": len(data)}, data)
            except Exception as exc:
                print(f"[gpu-worker] {exc!r}", file=sys.stderr, flush=True)
                _send({"ok": False, "error": repr(exc)})
    finally:
        renderer.close()


if __name__ == "__main__":
    main()
