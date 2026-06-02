"""GPU compositor — Stage 1 of the GPU-first rebuild.

Plays the clip library on the GPU at full resolution via the proven path
(hardware decode -> glupload -> glcolorconvert -> glshader FX -> display),
loops each clip, switches clips on command, and exposes a live FX uniform.
This is the skeleton the rest of the rebuild hangs off; it does NOT touch
the existing CPU rig (run that as before).

Run it standalone (system python3 — the one with gi):
    python3 gpu_compositor.py [clips_dir]

Type commands + Enter in the terminal:
    n        next clip            p   previous clip
    f 0.6    set FX amount 0..1   f 0   clean passthrough
    q        quit
(JSON lines like {"cmd":"next"} also work — that's how the real
controller/HUD will drive it later.)

Notes / not-yet:
  * Clips must be H.265/HEVC (the Pi 5's only hardware-decoded codec).
    decodebin still plays other codecs, but only HEVC gets the hardware
    decoder; H.264 4K will be slow (software).
  * Display targeting (which monitor) and gapless looping are Stage-1
    follow-ups; v1 opens fullscreen on the default output and does a
    flush-seek loop (a tiny hitch at the loop point is expected).
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
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".MP4", ".MOV", ".MKV", ".M4V")

# FX slot: u_amt=0 is a clean passthrough; >0 dials zoom + RGB split.
# (Same shader proven controllable live in Spike D. Real FX catalogue
# comes in Stage 2 when effects.py is ported to GLSL.)
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
    out = []
    for p in sorted(d.iterdir()):
        if p.suffix in VIDEO_EXTS and not p.name.startswith("_"):
            out.append(p)
    return out


class Compositor:
    def __init__(self, clips, loop_clip):
        self.clips = clips
        self.idx = 0
        self.loop_clip = loop_clip
        self.pipeline = None
        self.fx = None
        self.fx_amt = 0.0
        self.mainloop = None      # set in main(); used to stop on fatal error
        self.err_streak = 0       # consecutive clip failures (loop guard)

    # ---- pipeline lifecycle -------------------------------------------
    def _build(self):
        clip = self.clips[self.idx]
        print(f"[compositor] play [{self.idx + 1}/{len(self.clips)}] "
              f"{clip.name}", flush=True)

        # Proven path (Spikes C/D, 42fps): the EXPLICIT HEVC decoder chain
        # into GL, via parse_launch so GStreamer wires GL-context sharing
        # coherently. decodebin -> glupload fails to negotiate on this Pi,
        # so we use the explicit chain. HEVC-only for now; broader codec /
        # audio handling is a Stage-1 follow-up.
        loc = str(clip).replace("\\", "\\\\").replace('"', '\\"')
        desc = (
            f'filesrc location="{loc}" ! qtdemux ! h265parse ! '
            'v4l2slh265dec ! glupload ! glcolorconvert ! '
            'glshader name=fx ! glimagesink name=sink sync=true'
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            print(f"[compositor] build failed: {exc}", flush=True)
            self.pipeline = None
            return
        self.fx = self.pipeline.get_by_name("fx")
        self.fx.set_property("fragment", FX_SHADER)
        self._apply_fx()
        sink = self.pipeline.get_by_name("sink")
        try:
            sink.set_property("fullscreen", True)
        except Exception:
            pass
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _teardown(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.fx = None

    def start(self):
        if self.clips:
            self._build()

    # ---- actions (always called on the main loop) ---------------------
    def switch(self, delta=None, index=None):
        if not self.clips:
            return
        if index is not None:
            self.idx = index % len(self.clips)
        elif delta is not None:
            self.idx = (self.idx + delta) % len(self.clips)
        self._teardown()
        self._build()

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
            self.err_streak = 0          # a clip prerolled OK
        elif t == Gst.MessageType.EOS:
            self.err_streak = 0
            if self.loop_clip and self.pipeline is not None:
                # Flush-seek back to the start (v1 loop; small hitch).
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
            else:
                GLib.idle_add(lambda: (self.switch(delta=1), False)[1])
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[compositor] ERROR {err.message} :: {dbg}", flush=True)
            self.err_streak += 1
            if self.err_streak > len(self.clips):
                print("[compositor] every clip failed to play — stopping. "
                      "Clips must be H.265/HEVC in MP4; see the error(s) "
                      "above.", flush=True)
                if self.mainloop is not None:
                    self.mainloop.quit()
                return
            # Skip a broken clip rather than spinning on it.
            GLib.idle_add(lambda: (self.switch(delta=1), False)[1])


def _command_reader(dispatch):
    """Read stdin lines on a daemon thread; hand each to the main loop."""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        GLib.idle_add(lambda l=line: (dispatch(l), False)[1])


def main():
    clips_dir = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "assets" / "clips")
    Gst.init(None)
    print(f"[compositor] GStreamer {Gst.version_string()}", flush=True)

    clips = find_clips(clips_dir)
    if not clips:
        print(f"[compositor] no clips in {clips_dir}\n"
              f"             (need .mp4/.mkv etc — H.265/HEVC for hardware "
              f"decode). Try: python3 gpu_compositor.py tests", flush=True)
        return 1
    print(f"[compositor] {len(clips)} clip(s) from {clips_dir}", flush=True)

    comp = Compositor(clips, loop_clip=True)

    def dispatch(line):
        try:
            if line.startswith("{"):
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "next":
                    comp.switch(delta=1)
                elif cmd == "prev":
                    comp.switch(delta=-1)
                elif cmd == "play":
                    comp.switch(index=int(req.get("index", 0)))
                elif cmd == "fx":
                    comp.set_fx(float(req.get("amt", 0.0)))
                elif cmd == "quit":
                    loop.quit()
                return
            parts = line.split()
            key = parts[0].lower()
            if key in ("n", "next"):
                comp.switch(delta=1)
            elif key in ("p", "prev"):
                comp.switch(delta=-1)
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
    print("[compositor] keys: n=next  p=prev  f <0..1>=FX amount  q=quit",
          flush=True)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        comp._teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
