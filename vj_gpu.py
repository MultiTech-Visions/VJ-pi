"""vj_gpu.py — GPU-native foundation: one HEVC clip, projection-mapped onto
N corner-pinned surfaces, entirely on the GPU.

This is the correct foundation, not a bolt-on: the decoded frame stays on
the GPU the whole way (hardware decode -> glupload -> a corner-pin mapping
shader -> projector). The CPU never touches a pixel, which is the only way
4K works on this Pi.

The mapping shader is GENERATED from a list of surfaces (each a quad given
by 4 corners in 0..1 output space, order TL,TR,BR,BL — same model as
mapping.py). For each surface we bake the inverse homography (output ->
clip UV) into the shader; per output pixel it tests each surface and
samples the clip through it. Editing a surface = regenerate the shader.

Run (SYSTEM python3 — the one with gi):
    python3 vj_gpu.py [clips_dir]

Terminal keys (+ Enter):  n next clip   p prev clip   g cycle grid (1/9/16)
                          q quit
Clips must be H.265/HEVC. Fullscreen-on-projector via the labwc rule.
"""
import json
import math
import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

HERE = Path(__file__).resolve().parent
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".MP4", ".MOV", ".MKV", ".M4V")


# ---- homography (pure python; no numpy in system python3) -------------
def _square_to_quad(q):
    """3x3 mapping clip unit-square corners (0,0),(1,0),(1,1),(0,1) to the
    output quad q=[TL,TR,BR,BL]. Heckbert's method. Row-major."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = q
    dx1, dx2 = x1 - x2, x3 - x2
    dx3 = x0 - x1 + x2 - x3
    dy1, dy2 = y1 - y2, y3 - y2
    dy3 = y0 - y1 + y2 - y3
    if abs(dx3) < 1e-12 and abs(dy3) < 1e-12:
        a, b, c = x1 - x0, x2 - x1, x0
        d, e, f = y1 - y0, y2 - y1, y0
        g = h = 0.0
    else:
        den = dx1 * dy2 - dx2 * dy1
        g = (dx3 * dy2 - dx2 * dy3) / den
        h = (dx1 * dy3 - dx3 * dy1) / den
        a, b, c = x1 - x0 + g * x1, x3 - x0 + h * x3, x0
        d, e, f = y1 - y0 + g * y1, y3 - y0 + h * y3, y0
    return [[a, b, c], [d, e, f], [g, h, 1.0]]


def _inv3(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    A, B, C = (e * i - f * h), -(d * i - f * g), (d * h - e * g)
    D, E, F = -(b * i - c * h), (a * i - c * g), -(a * h - b * g)
    G, H, I = (b * f - c * e), -(a * f - c * d), (a * e - b * d)
    det = a * A + b * B + c * C
    return [[A / det, D / det, G / det],
            [B / det, E / det, H / det],
            [C / det, F / det, I / det]]


def _grid_surfaces(n, warp=0.13):
    """n x n grid of quads tiling 0..1, each rotated a little about its
    centre so the mapping is visibly warped (not just a flat grid)."""
    surfaces = []
    for r in range(n):
        for cidx in range(n):
            x0, y0 = cidx / n, r / n
            x1, y1 = (cidx + 1) / n, (r + 1) / n
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            ang = warp * (1 if (r + cidx) % 2 == 0 else -1)
            s, co = math.sin(ang), math.cos(ang)
            quad = []
            for (px, py) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                dx, dy = px - cx, py - cy
                quad.append((cx + dx * co - dy * s, cy + dx * s + dy * co))
            surfaces.append(quad)
    return surfaces


def _fullscreen_surfaces():
    return [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]]


def build_mapping_shader(surfaces):
    blocks = []
    for quad in surfaces:
        m = _inv3(_square_to_quad(quad))
        (m00, m01, m02), (m10, m11, m12), (m20, m21, m22) = m
        blocks.append(f"""  {{
    float w = ({m20:.8g})*p.x + ({m21:.8g})*p.y + ({m22:.8g});
    float u = (({m00:.8g})*p.x + ({m01:.8g})*p.y + ({m02:.8g})) / w;
    float v = (({m10:.8g})*p.x + ({m11:.8g})*p.y + ({m12:.8g})) / w;
    if (u >= 0.0 && u <= 1.0 && v >= 0.0 && v <= 1.0) {{
      col = texture2D(tex, vec2(u, v));
    }}
  }}""")
    return ("#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\n"
            "varying vec2 v_texcoord;\nuniform sampler2D tex;\n"
            "void main () {\n  vec2 p = v_texcoord;\n"
            "  vec4 col = vec4(0.0, 0.0, 0.0, 1.0);\n"
            + "\n".join(blocks)
            + "\n  gl_FragColor = col;\n}\n")


GRID_CYCLE = [1, 3, 4]    # 1=fullscreen, 3=9 surfaces, 4=16 surfaces


class Compositor:
    def __init__(self, clips):
        self.clips = clips
        self.idx = 0
        self.grid_i = 2          # start at 16 surfaces
        self.pipeline = None
        self.mainloop = None
        self.err_streak = 0

    def _surfaces(self):
        n = GRID_CYCLE[self.grid_i]
        return _fullscreen_surfaces() if n == 1 else _grid_surfaces(n)

    def _build(self):
        clip = self.clips[self.idx]
        surfaces = self._surfaces()
        print(f"[vj-gpu] [{self.idx + 1}/{len(self.clips)}] {clip.name} "
              f"-> {len(surfaces)} surface(s)", flush=True)
        loc = str(clip).replace("\\", "\\\\").replace('"', '\\"')
        desc = (
            f'filesrc location="{loc}" ! qtdemux ! h265parse ! '
            'v4l2slh265dec ! glupload ! glcolorconvert ! '
            'glshader name=map ! glimagesink name=sink sync=true'
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            print(f"[vj-gpu] build failed: {exc}", flush=True)
            self.pipeline = None
            return
        self.pipeline.get_by_name("map").set_property(
            "fragment", build_mapping_shader(surfaces))
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _teardown(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _rebuild(self):
        self._teardown()
        self._build()

    def start(self):
        if self.clips:
            self._build()

    def switch(self, delta):
        if self.clips:
            self.idx = (self.idx + delta) % len(self.clips)
            self._rebuild()

    def cycle_grid(self):
        self.grid_i = (self.grid_i + 1) % len(GRID_CYCLE)
        self._rebuild()

    def _on_bus(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            self.err_streak = 0
        elif t == Gst.MessageType.EOS:
            self.err_streak = 0
            if self.pipeline is not None:
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[vj-gpu] ERROR {err.message} :: {dbg}", flush=True)
            self.err_streak += 1
            if self.err_streak > len(self.clips):
                print("[vj-gpu] every clip failed — stopping. Clips must be "
                      "H.265/HEVC.", flush=True)
                if self.mainloop is not None:
                    self.mainloop.quit()
                return
            GLib.idle_add(lambda: (self.switch(1), False)[1])


def find_clips(clips_dir):
    d = Path(clips_dir)
    if not d.exists():
        return []
    return [p for p in sorted(d.iterdir())
            if p.suffix in VIDEO_EXTS and not p.name.startswith("_")]


def _reader(dispatch):
    for raw in sys.stdin:
        line = raw.strip()
        if line:
            GLib.idle_add(lambda l=line: (dispatch(l), False)[1])


def main():
    clips_dir = sys.argv[1] if len(sys.argv) > 1 else str(
        HERE / "assets" / "clips")
    Gst.init(None)
    clips = find_clips(clips_dir)
    if not clips:
        print(f"[vj-gpu] no clips in {clips_dir}", flush=True)
        return 1
    print(f"[vj-gpu] {len(clips)} clip(s) from {clips_dir}", flush=True)
    comp = Compositor(clips)

    def dispatch(line):
        try:
            if line.startswith("{"):
                cmd = json.loads(line).get("cmd")
                {"next": lambda: comp.switch(1),
                 "prev": lambda: comp.switch(-1),
                 "grid": comp.cycle_grid,
                 "quit": loop.quit}.get(cmd, lambda: None)()
                return
            k = line.split()[0].lower()
            if k in ("n", "next"):
                comp.switch(1)
            elif k in ("p", "prev"):
                comp.switch(-1)
            elif k in ("g", "grid"):
                comp.cycle_grid()
            elif k in ("q", "quit"):
                loop.quit()
        except Exception as exc:  # noqa: BLE001
            print(f"[vj-gpu] bad command {line!r}: {exc!r}", flush=True)

    loop = GLib.MainLoop()
    comp.mainloop = loop
    threading.Thread(target=_reader, args=(dispatch,), daemon=True).start()
    comp.start()
    print("[vj-gpu] keys: n=next  p=prev  g=grid(1/9/16)  q=quit", flush=True)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        comp._teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
