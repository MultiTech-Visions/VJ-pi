"""GPU compositor — Stage 1 of the GPU-first rebuild.

Plays the clip library AND the GLSL generators (from shader_catalog) on the
GPU at full resolution, loops clips, switches source on command, and runs
everything through a live FX slot. This is the skeleton the rest of the
rebuild hangs off; it does NOT touch the existing CPU rig.

Run it standalone (system python3 — the one with gi):
    python3 gpu_compositor.py [clips_dir]

Type commands + Enter in the terminal:
    n        next (clip or generator, within the current source)
    p        previous
    g        generators: switch to / cycle the GLSL generators
    c        clips: switch back to the clip library
    f 0.6    set FX amount 0..1     f 0   clean passthrough
    q        quit
(JSON lines like {"cmd":"next"} / {"cmd":"gen"} also work — that's how the
real controller/HUD will drive it later.)

Notes / not-yet:
  * Clips must be H.265/HEVC (the Pi 5's only hardware-decoded codec).
  * Generators render at GEN_W x GEN_H and are upscaled to the projector
    (cheap + smooth; full-4K generator passes would be slow on V3D).
  * The FX pass is always present for now, so even a clean clip pays one
    GL pass (~21fps vs 42). Dropping it when FX==0 (for full-speed clean
    cinematic) is a follow-up.
  * Fullscreen-on-projector is handled by the labwc window rule, not here.
"""
import json
import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    from shader_catalog import GPU_GENERATORS, GPU_GENERATOR_ORDER
except Exception as exc:  # noqa: BLE001
    print(f"[compositor] could not import shader_catalog ({exc!r}); "
          f"generators disabled", flush=True)
    GPU_GENERATORS, GPU_GENERATOR_ORDER = {}, []

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".MP4", ".MOV", ".MKV", ".M4V")
GEN_W, GEN_H = 1280, 720      # generator render res (upscaled to projector)

# FX slot: u_amt=0 is a clean passthrough; >0 dials zoom + RGB split.
# (Real FX catalogue comes in Stage 2 when effects.py is ported to GLSL.)
FX_SHADER = """#version 100
#ifdef GL_ES
precision mediump float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float u_amt;
void main () {
  vec2 c = v_texcoord - 0.5;
  c = c / (1.0 + 0.5 * u_amt);
  vec2 suv = c + 0.5;
  float sh = 0.03 * u_amt;
  float r = texture2D(tex, suv + vec2(sh, 0.0)).r;
  float g = texture2D(tex, suv).g;
  float b = texture2D(tex, suv - vec2(sh, 0.0)).b;
  gl_FragColor = vec4(r, g, b, 1.0);
}
"""


def find_clips(clips_dir):
    d = Path(clips_dir)
    if not d.exists():
        return []
    return [p for p in sorted(d.iterdir())
            if p.suffix in VIDEO_EXTS and not p.name.startswith("_")]


def list_generators():
    # donut needs an image source; skip it for now.
    return [g for g in GPU_GENERATOR_ORDER
            if g in GPU_GENERATORS and g != "donut"]


class Compositor:
    def __init__(self, clips, gens, loop_clip):
        self.clips = clips
        self.gens = gens
        self.idx = 0
        self.gen_idx = 0
        self.mode = "clip" if clips else ("gen" if gens else "clip")
        self.loop_clip = loop_clip
        self.pipeline = None
        self.fx = None
        self.fx_amt = 0.0
        self.mainloop = None
        self.err_streak = 0

    # ---- pipeline lifecycle -------------------------------------------
    def _launch(self, desc, gen_shader=None):
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            print(f"[compositor] build failed: {exc}", flush=True)
            self.pipeline = None
            return False
        self.fx = self.pipeline.get_by_name("fx")
        if self.fx is not None:
            self.fx.set_property("fragment", FX_SHADER)
            self._apply_fx()
        if gen_shader is not None:
            gen = self.pipeline.get_by_name("gen")
            if gen is not None:
                gen.set_property("fragment", gen_shader)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        self.pipeline.set_state(Gst.State.PLAYING)
        return True

    def _build_clip(self):
        clip = self.clips[self.idx]
        print(f"[compositor] clip [{self.idx + 1}/{len(self.clips)}] "
              f"{clip.name}", flush=True)
        # Proven path (Spikes C/D, 42fps): explicit HEVC decoder -> GL.
        loc = str(clip).replace("\\", "\\\\").replace('"', '\\"')
        desc = (
            f'filesrc location="{loc}" ! qtdemux ! h265parse ! '
            'v4l2slh265dec ! glupload ! glcolorconvert ! '
            'glshader name=fx ! glimagesink name=sink sync=true'
        )
        self._launch(desc)

    def _build_gen(self):
        name = self.gens[self.gen_idx]
        print(f"[compositor] generator [{self.gen_idx + 1}/{len(self.gens)}] "
              f"{name}", flush=True)
        desc = (
            'videotestsrc is-live=true pattern=black ! '
            f'video/x-raw,width={GEN_W},height={GEN_H},framerate=30/1 ! '
            'glupload ! glshader name=gen ! glshader name=fx ! '
            'glimagesink name=sink sync=true'
        )
        self._launch(desc, gen_shader=GPU_GENERATORS[name])

    def _build(self):
        if self.mode == "gen" and self.gens:
            self._build_gen()
        elif self.clips:
            self._build_clip()
        else:
            print("[compositor] nothing to play (no clips, no generators)",
                  flush=True)

    def _teardown(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.fx = None

    def _rebuild(self):
        self._teardown()
        self._build()

    def start(self):
        self._build()

    # ---- actions (always on the main loop) ----------------------------
    def switch(self, delta=1):
        if self.mode == "gen" and self.gens:
            self.gen_idx = (self.gen_idx + delta) % len(self.gens)
        elif self.clips:
            self.idx = (self.idx + delta) % len(self.clips)
        self._rebuild()

    def set_mode(self, mode):
        if mode == "gen" and not self.gens:
            print("[compositor] no generators available", flush=True)
            return
        if mode == "clip" and not self.clips:
            print("[compositor] no clips available", flush=True)
            return
        self.mode = mode
        self._rebuild()

    def set_clip(self, index):
        if self.clips:
            self.mode = "clip"
            self.idx = index % len(self.clips)
            self._rebuild()

    def set_fx(self, amt):
        self.fx_amt = max(0.0, min(1.0, amt))
        self._apply_fx()

    def _apply_fx(self):
        if self.fx is None:
            return
        try:
            s = Gst.Structure.new_from_string(
                f"uniforms,u_amt=(float){self.fx_amt:.4f}")
            self.fx.set_property("uniforms", s)
        except Exception as exc:  # noqa: BLE001
            print(f"[compositor] fx set failed: {exc!r}", flush=True)

    # ---- bus ----------------------------------------------------------
    def _on_bus(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            self.err_streak = 0
        elif t == Gst.MessageType.EOS:
            self.err_streak = 0
            if self.loop_clip and self.pipeline is not None:
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
            else:
                GLib.idle_add(lambda: (self.switch(1), False)[1])
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[compositor] ERROR {err.message} :: {dbg}", flush=True)
            self.err_streak += 1
            cap = len(self.clips) + len(self.gens) + 1
            if self.err_streak > cap:
                print("[compositor] everything failed to play — stopping. "
                      "Clips must be H.265/HEVC; see the error(s) above.",
                      flush=True)
                if self.mainloop is not None:
                    self.mainloop.quit()
                return
            GLib.idle_add(lambda: (self.switch(1), False)[1])


def _command_reader(dispatch):
    for raw in sys.stdin:
        line = raw.strip()
        if line:
            GLib.idle_add(lambda l=line: (dispatch(l), False)[1])


def main():
    clips_dir = sys.argv[1] if len(sys.argv) > 1 else str(
        HERE / "assets" / "clips")
    Gst.init(None)
    print(f"[compositor] GStreamer {Gst.version_string()}", flush=True)

    clips = find_clips(clips_dir)
    gens = list_generators()
    print(f"[compositor] {len(clips)} clip(s) from {clips_dir}, "
          f"{len(gens)} generator(s)", flush=True)
    if not clips and not gens:
        print("[compositor] nothing to play. Need HEVC clips or "
              "shader_catalog generators.", flush=True)
        return 1

    comp = Compositor(clips, gens, loop_clip=True)

    def dispatch(line):
        try:
            if line.startswith("{"):
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "next":
                    comp.switch(1)
                elif cmd == "prev":
                    comp.switch(-1)
                elif cmd == "gen":
                    comp.set_mode("gen")
                elif cmd == "clip":
                    comp.set_mode("clip")
                elif cmd == "play":
                    comp.set_clip(int(req.get("index", 0)))
                elif cmd == "fx":
                    comp.set_fx(float(req.get("amt", 0.0)))
                elif cmd == "quit":
                    loop.quit()
                return
            parts = line.split()
            key = parts[0].lower()
            if key in ("n", "next"):
                comp.switch(1)
            elif key in ("p", "prev"):
                comp.switch(-1)
            elif key in ("g", "gen"):
                # switch into generators, or advance if already there
                if comp.mode == "gen":
                    comp.switch(1)
                else:
                    comp.set_mode("gen")
            elif key in ("c", "clip", "clips"):
                comp.set_mode("clip")
            elif key in ("f", "fx") and len(parts) > 1:
                comp.set_fx(float(parts[1]))
            elif key in ("q", "quit"):
                loop.quit()
            else:
                print(f"[compositor] ? {line!r}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[compositor] bad command {line!r}: {exc!r}", flush=True)

    loop = GLib.MainLoop()
    comp.mainloop = loop
    threading.Thread(target=_command_reader, args=(dispatch,),
                     daemon=True).start()
    comp.start()
    print("[compositor] keys: n=next p=prev g=generators c=clips "
          "f <0..1>=FX q=quit", flush=True)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        comp._teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
