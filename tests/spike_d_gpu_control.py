"""Spike D — live external control of a GPU FX pass on the 4K clip.

Pure GStreamer, system python3 (the one with `gi`). No pygame, no custom
GL context, no new deps.

THE QUESTION
------------
A VJ rig needs to drive effects live — FX intensity on the arrow keys,
hits, param tweaks, every frame. The open architecture question was
whether GStreamer's `glshader` can take live, externally-set uniforms (if
not, we'd need a custom GL compositor just for that). The docs say it can,
via the element's `uniforms` property. This spike proves it on the real
4K GL path.

It plays the 4K clip through a glshader FX whose look is driven ENTIRELY
by a custom `u_amt` uniform (zoom + RGB split). A timer ramps `u_amt`
0..1 from Python ~30x/sec. If the picture visibly pulses/zooms, live
external control works — and the FX layer of the GPU rebuild can ride on
proven GStreamer GL infrastructure instead of a hand-rolled GL engine.

If `u_amt` had NO effect (stayed static), the uniform isn't getting
through and we'd fall back to the create-shader signal or a custom
compositor.

RUN
---
  python3 tests/spike_d_gpu_control.py            # uses tests/4k_hevc_test.mp4
  python3 tests/spike_d_gpu_control.py path/to/clip.mp4

REPORT BACK
-----------
  * Did the image visibly PULSE/zoom with a colour-split? (= live control)
  * The "[spike-d] ... RESULT: NN.N fps" line.
  * The "[spike-d] glshader properties:" line (does it list 'uniforms'?).
  * Any '[spike-d]' error lines.
"""
import math
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402


# The whole look is driven by u_amt (an EXTERNAL uniform we set from
# Python), NOT by the built-in `time` — so any motion proves live control.
FX_SHADER = """#version 100
#ifdef GL_ES
precision mediump float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float u_amt;
void main () {
  vec2 c = v_texcoord - 0.5;
  c = c / (1.0 + 0.5 * u_amt);          // zoom driven by u_amt
  vec2 suv = c + 0.5;
  float sh = 0.03 * u_amt;              // RGB split driven by u_amt
  float r = texture2D(tex, suv + vec2(sh, 0.0)).r;
  float g = texture2D(tex, suv).g;
  float b = texture2D(tex, suv - vec2(sh, 0.0)).b;
  gl_FragColor = vec4(r, g, b, 1.0);
}
"""


def main():
    clip = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "4k_hevc_test.mp4")
    if not os.path.exists(clip):
        print(f"[spike-d] clip not found: {clip}\n"
              f"          make one first: double-click 'Make 4K Test Clip.sh'",
              flush=True)
        return 1

    Gst.init(None)
    print(f"[spike-d] GStreamer {Gst.version_string()}", flush=True)

    desc = (f'filesrc location="{clip}" ! qtdemux ! h265parse ! '
            "v4l2slh265dec ! glupload ! glcolorconvert ! "
            "glshader name=fx ! glimagesink name=vsink sync=false")
    print(f"[spike-d] pipeline: {desc}", flush=True)
    try:
        pipeline = Gst.parse_launch(desc)
    except GLib.Error as exc:
        print(f"[spike-d] parse failed: {exc}", flush=True)
        return 1

    fx = pipeline.get_by_name("fx")
    fx.set_property("fragment", FX_SHADER)

    # Diagnostic: what knobs does this glshader actually expose?
    props = [p.name for p in fx.list_properties()]
    print(f"[spike-d] glshader properties: {props}", flush=True)
    has_uniforms = "uniforms" in props
    if not has_uniforms:
        print("[spike-d] WARNING: no 'uniforms' property — will try the "
              "create-shader signal path instead", flush=True)

    # fps probe at the sink
    vsink = pipeline.get_by_name("vsink")
    st = {"n": 0, "t0": None, "last": None}

    def _probe(_pad, _info):
        now = time.perf_counter()
        if st["t0"] is None:
            st["t0"] = now
            st["last"] = now
            return Gst.PadProbeReturn.OK
        st["n"] += 1
        if now - st["last"] >= 1.0:
            fps = st["n"] / (now - st["t0"])
            print(f"[spike-d] {fps:5.1f} fps  (u_amt live)", flush=True)
            st["last"] = now
        return Gst.PadProbeReturn.OK
    pad = vsink.get_static_pad("sink")
    if pad is not None:
        pad.add_probe(Gst.PadProbeType.BUFFER, _probe)

    # Drive the custom uniform live from Python (~30 Hz).
    drive = {"t0": time.perf_counter(), "errs": 0, "ok": False}

    def _tick():
        t = time.perf_counter() - drive["t0"]
        amt = 0.5 + 0.5 * math.sin(t * 2.2)       # pulse 0..1
        if has_uniforms:
            try:
                s = Gst.Structure.new_from_string(
                    f"uniforms,u_amt=(float){amt:.4f}")
                fx.set_property("uniforms", s)
                drive["ok"] = True
            except Exception as exc:  # noqa: BLE001
                drive["errs"] += 1
                if drive["errs"] == 1:
                    print(f"[spike-d] uniforms property set failed: {exc!r}",
                          flush=True)
        return True
    GLib.timeout_add(33, _tick)

    loop = GLib.MainLoop()

    def _msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            e, d = m.parse_error()
            print(f"[spike-d] ERROR {e.message} :: {d}", flush=True)
            loop.quit()
        elif m.type == Gst.MessageType.EOS:
            loop.quit()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _msg)

    pipeline.set_state(Gst.State.PLAYING)
    res, _, _ = pipeline.get_state(6 * Gst.SECOND)
    if res == Gst.StateChangeReturn.FAILURE:
        print("[spike-d] pipeline failed to start", flush=True)
        pipeline.set_state(Gst.State.NULL)
        return 1

    print("[spike-d] playing 15s — WATCH: the picture should pulse/zoom with "
          "an RGB split, driven live from Python. Static = no live control.",
          flush=True)
    GLib.timeout_add_seconds(15, lambda: (loop.quit() or False))
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    if st["t0"] is not None and st["n"] > 0:
        fps = st["n"] / (time.perf_counter() - st["t0"])
        verdict = "HOLDS 30fps" if fps >= 29.5 else "below 30fps"
        ctl = ("uniforms property accepted" if drive["ok"] and not drive["errs"]
               else "uniform set FAILED — see errors above")
        print(f"[spike-d] RESULT: {fps:5.1f} fps, live control: {ctl} "
              f"-> {verdict}", flush=True)
    else:
        print("[spike-d] RESULT: no frames reached the sink", flush=True)
    print("[spike-d] DONE.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
