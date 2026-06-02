"""Spike B — 4K HEVC decode + present throughput (THE decisive test).

Pure GStreamer, run under SYSTEM python3 (the one with `gi`) — exactly
like gpu_generator_worker.py. No pygame: this rig never mixes GStreamer's
`gi` bindings and pygame in one process.

THE QUESTION
------------
The Pi 5 has exactly ONE hardware video decoder: HEVC / H.265, up to
4K60. Can it HARDWARE-decode 4K H.265 AND get it on screen at 30 fps?

We already learned: pure decode runs ~160 fps (the v4l2slh265dec hardware
decoder is plenty), but a naive display path collapses to ~6 fps because
the decoder's DMABUF frames get bounced through the CPU instead of going
to the display zero-copy. So the open question is purely the PRESENT
path: which video sink takes the decoder's DMABUF directly and holds
30 fps?

  --mode decode  : filesrc -> decodebin -> appsink, timed (decoder ceiling)
  --mode sweep   : try several sinks (glimagesink / waylandsink / kmssink /
                   autovideosink), measure on-screen fps for each via a
                   buffer-counting pad probe (no text overlay to skew it)
  --mode display : one sink only, chosen with --sink

RUN
---
  python3 tests/spike_b_4k_decode.py --mode decode
  python3 tests/spike_b_4k_decode.py --mode sweep --fullscreen
  python3 tests/spike_b_4k_decode.py --mode display --sink kmssink --fullscreen

REPORT BACK
-----------
  * The "decoder plugged" line (should be v4l2slh265dec -> HARDWARE).
  * The "RESULT" line for decode and for EACH sink in the sweep — we want
    to see which sink hits ~30 fps.
  * Any '[spike-b]' error lines.
"""
import argparse
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402


# A live GPU warp for the mapping test: rotate + perspective-keystone +
# breathing zoom of the decoded texture, sampling `tex` at warped coords.
# Black outside the source so the warp is obvious on the wall. This is the
# same glshader contract the generator worker uses (tex, v_texcoord, time).
WARP_SHADER = """#version 100
#ifdef GL_ES
precision mediump float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float time;
void main () {
  vec2 c = v_texcoord - 0.5;
  float a = 0.15 * sin(time * 0.5);
  float s = sin(a); float co = cos(a);
  c = mat2(co, -s, s, co) * c;                 // rotate
  float k = 0.25 * (0.5 + 0.5 * sin(time * 0.4));
  float persp = mix(1.0 - k, 1.0 + k, c.y + 0.5);
  c.x = c.x / persp;                           // perspective keystone
  c = c * (1.0 + 0.1 * sin(time * 0.3));       // breathing zoom
  vec2 suv = c + 0.5;
  // Branchless edge mask — V3D hates branches, so no if().
  vec2 e = step(vec2(0.0), suv) * step(suv, vec2(1.0));
  float m = e.x * e.y;
  gl_FragColor = vec4(texture2D(tex, clamp(suv, 0.0, 1.0)).rgb * m, 1.0);
}
"""


def _classify_decoder(pipeline):
    """Print every element's factory name and flag hardware vs software."""
    names = []
    it = pipeline.iterate_recurse()
    while True:
        res, elem = it.next()
        if res == Gst.IteratorResult.OK:
            f = elem.get_factory()
            names.append(f.get_name() if f else "?")
        elif res == Gst.IteratorResult.DONE:
            break
        else:
            break
    decoders = [n for n in names if "dec" in n.lower()]
    hw = [n for n in decoders
          if any(k in n.lower() for k in ("v4l2", "rpivid", "drm", "sl"))]
    sw = [n for n in decoders if any(k in n.lower() for k in ("avdec", "libav"))]
    if hw:
        print(f"[spike-b] decoder plugged: {', '.join(hw)}  -> HARDWARE", flush=True)
    elif sw:
        print(f"[spike-b] decoder plugged: {', '.join(sw)}  -> SOFTWARE", flush=True)
    else:
        print(f"[spike-b] decoder plugged: {', '.join(decoders) or '??'} -> ?",
              flush=True)


def _src_chain(clip, decoder):
    if decoder in (None, "", "auto"):
        return f'filesrc location="{clip}" ! decodebin'
    return f'filesrc location="{clip}" ! qtdemux ! h265parse ! {decoder}'


def run_decode(clip, decoder, n_frames):
    """Decode to appsink with NO colour-convert = pure decoder throughput."""
    desc = (f'{_src_chain(clip, decoder)} ! '
            "appsink name=sink emit-signals=false max-buffers=2 drop=true sync=false")
    print(f"[spike-b] pipeline: {desc}", flush=True)
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    pipeline.get_state(5 * Gst.SECOND)
    _classify_decoder(pipeline)

    pulled = 0
    first_size = None
    t_start = None
    last_beat = None
    try:
        while pulled < n_frames:
            sample = sink.emit("try-pull-sample", 3 * Gst.SECOND)
            if sample is None:
                break
            now = time.perf_counter()
            if first_size is None:
                s = sample.get_caps().get_structure(0)
                first_size = (s.get_value("width"), s.get_value("height"))
            if t_start is None:
                t_start = now
                last_beat = now
                pulled += 1
                continue
            pulled += 1
            if now - last_beat >= 1.0:
                fps = (pulled - 1) / (now - t_start)
                print(f"[spike-b] decode: {pulled - 1:4d} frames avg {fps:5.1f} "
                      f"fps size={first_size}", flush=True)
                last_beat = now
    finally:
        pipeline.set_state(Gst.State.NULL)
    if t_start is not None and pulled > 1:
        fps = (pulled - 1) / (time.perf_counter() - t_start)
        verdict = "holds 30fps" if fps >= 29.5 else "below 30fps"
        print(f"[spike-b] decode    RESULT: {fps:5.1f} fps over {pulled - 1} "
              f"frames, size={first_size} -> {verdict}", flush=True)


def _sink_desc(sink):
    """Element chain per candidate. The earlier sweep died 'not-negotiated'
    because there was no converter between the decoder (NV12 in GPU/DMABUF
    memory) and the sink. So:
      gl      = GPU convert path: glupload ! glcolorconvert ! glimagesink
                (conversion on the GPU — the one that should be FAST)
      wayland = CPU-convert baseline to waylandsink (will negotiate; slow)
      kms     = CPU-convert baseline to kmssink"""
    if sink == "gl":
        return ("glupload ! glcolorconvert ! "
                "glimagesink name=vsink force-aspect-ratio=true sync=false")
    if sink == "wayland":
        return "videoconvert ! waylandsink name=vsink sync=false"
    if sink == "kms":
        return "videoconvert ! kmssink name=vsink sync=false"
    return "videoconvert ! autovideosink name=vsink sync=false"


def measure_pipeline(desc, label, seconds, setup=None):
    """Run a full pipeline string for `seconds`, counting buffers at the
    element named 'vsink' for a clean on-screen fps. Prints the real bus
    error if it won't start. `setup(pipeline)` runs after construction
    (e.g. to set a glshader fragment) before the pipeline goes PLAYING."""
    print(f"[spike-b] ---- {label}: {desc}", flush=True)
    try:
        pipeline = Gst.parse_launch(desc)
    except GLib.Error as exc:
        print(f"[spike-b] {label:14s} RESULT: parse failed ({exc})", flush=True)
        return

    if setup is not None:
        try:
            setup(pipeline)
        except Exception as exc:  # noqa: BLE001
            print(f"[spike-b] {label:14s} setup failed: {exc!r}", flush=True)

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
            print(f"[spike-b] {label}: {fps:5.1f} fps", flush=True)
            st["last"] = now
        return Gst.PadProbeReturn.OK

    pad = vsink.get_static_pad("sink") if vsink is not None else None
    if pad is not None:
        pad.add_probe(Gst.PadProbeType.BUFFER, _probe)

    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)

    # Synchronously wait for preroll (ASYNC_DONE) OR the real error.
    msg = bus.timed_pop_filtered(
        6 * Gst.SECOND,
        Gst.MessageType.ERROR | Gst.MessageType.ASYNC_DONE | Gst.MessageType.EOS)
    if msg is not None and msg.type == Gst.MessageType.ERROR:
        e, d = msg.parse_error()
        print(f"[spike-b] {label:14s} RESULT: ERROR {e.message} :: {d}", flush=True)
        pipeline.set_state(Gst.State.NULL)
        return
    if msg is None:
        print(f"[spike-b] {label:14s} RESULT: no preroll within 6s (stuck)",
              flush=True)
        pipeline.set_state(Gst.State.NULL)
        return

    _classify_decoder(pipeline)

    loop = GLib.MainLoop()

    def _bus_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            e, d = m.parse_error()
            print(f"[spike-b] {label}: late ERROR {e.message} :: {d}", flush=True)
            loop.quit()
        elif m.type == Gst.MessageType.EOS:
            loop.quit()
    bus.add_signal_watch()
    bus.connect("message", _bus_msg)
    GLib.timeout_add_seconds(int(seconds), lambda: (loop.quit() or False))
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    if st["t0"] is not None and st["n"] > 0:
        fps = st["n"] / (time.perf_counter() - st["t0"])
        verdict = "HOLDS 30fps" if fps >= 29.5 else "below 30fps"
        print(f"[spike-b] {label:14s} RESULT: {fps:5.1f} fps on screen "
              f"-> {verdict}", flush=True)
    else:
        print(f"[spike-b] {label:14s} RESULT: prerolled but no frames advanced",
              flush=True)


def measure_sink(clip, decoder, sink, seconds, fullscreen):
    desc = f'{_src_chain(clip, decoder)} ! queue ! {_sink_desc(sink)}'
    measure_pipeline(desc, sink, seconds)


def run_dmabuf(clip, seconds):
    """The documented zero-copy route: keep the decoder's DMABUF (the Pi's
    tiled NV12_128C8 'SAND' format) and import it into GL via glupload, no
    CPU videoconvert. If ALL of these fail 'not-negotiated', the installed
    GStreamer is almost certainly too old for DMABUF+DRM-modifier glupload
    and needs updating (apt)."""
    dec = f'filesrc location="{clip}" ! qtdemux ! h265parse ! v4l2slh265dec'
    candidates = [
        (f'{dec} ! glupload ! glimagesink name=vsink sync=false',
         "gl-direct"),
        (f'{dec} ! glupload ! glcolorconvert ! glimagesink name=vsink sync=false',
         "gl-colorconv"),
        (f'{dec} ! capsfilter caps="video/x-raw(memory:DMABuf)" ! '
         f'glupload ! glimagesink name=vsink sync=false',
         "gl-dmabufcaps"),
    ]
    for desc, label in candidates:
        try:
            measure_pipeline(desc, label, seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"[spike-b] {label:14s} RESULT: exception {exc!r}", flush=True)


def run_warp(clip, seconds):
    """Spike C: warp the decoded 4K GL texture ON THE GPU and measure fps.
    If this holds 30, projection mapping can be a GPU shader pass and there
    is no need for a separate CPU mapping path — the whole rig can go
    GPU-first. Tests two mechanisms:
      glshader-warp  = custom GLSL warp (what real mapping would use; same
                       element the generator worker uses)
      gltransformation = built-in GL 3D perspective transform"""
    dec = (f'filesrc location="{clip}" ! qtdemux ! h265parse ! '
           "v4l2slh265dec ! glupload ! glcolorconvert")

    def _set_shader(pipeline):
        sh = pipeline.get_by_name("warp")
        if sh is not None:
            sh.set_property("fragment", WARP_SHADER)

    measure_pipeline(
        f'{dec} ! glshader name=warp ! glimagesink name=vsink sync=false',
        "glshader-warp", seconds, setup=_set_shader)
    measure_pipeline(
        f'{dec} ! gltransformation name=warp rotation-y=25.0 fov=80.0 ! '
        "glimagesink name=vsink sync=false",
        "gltransformation", seconds)


def run_sweep(clip, decoder, seconds, fullscreen):
    print(f"[spike-b] session: XDG_SESSION_TYPE="
          f"{os.environ.get('XDG_SESSION_TYPE')}, "
          f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}, "
          f"DISPLAY={os.environ.get('DISPLAY')}, "
          f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR')}", flush=True)
    for sink in ("gl", "wayland", "kms"):
        try:
            measure_sink(clip, decoder, sink, seconds, fullscreen)
        except Exception as exc:  # noqa: BLE001
            print(f"[spike-b] {sink:13s} RESULT: exception {exc!r}", flush=True)


def run_playbin(clip, seconds):
    """The production auto path: playbin3 picks demux/decode/convert/sink
    itself. fpsdisplaysink (text-overlay off, so no CPU 4K overlay tax)
    measures the real on-screen rate."""
    uri = Gst.filename_to_uri(clip)
    pb = (Gst.ElementFactory.make("playbin3", None)
          or Gst.ElementFactory.make("playbin", None))
    if pb is None:
        print("[spike-b] playbin RESULT: playbin not available", flush=True)
        return
    pb.set_property("uri", uri)
    fps_sink = Gst.ElementFactory.make("fpsdisplaysink", None)
    fps_sink.set_property("text-overlay", False)
    fps_sink.set_property("signal-fps-measurements", True)
    fps_sink.set_property("sync", False)
    last = {"avg": 0.0}

    def _on_fps(_s, fps, drop, avg):
        last["avg"] = avg
        print(f"[spike-b] playbin: {fps:5.1f} fps (avg {avg:5.1f}, "
              f"drop {drop:5.1f})", flush=True)
    fps_sink.connect("fps-measurements", _on_fps)
    pb.set_property("video-sink", fps_sink)

    loop = GLib.MainLoop()

    def _msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            e, d = m.parse_error()
            print(f"[spike-b] playbin: ERROR {e.message} :: {d}", flush=True)
            loop.quit()
        elif m.type == Gst.MessageType.EOS:
            loop.quit()
    bus = pb.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _msg)

    pb.set_state(Gst.State.PLAYING)
    pb.get_state(5 * Gst.SECOND)
    _classify_decoder(pb)
    GLib.timeout_add_seconds(int(seconds), lambda: (loop.quit() or False))
    try:
        loop.run()
    finally:
        pb.set_state(Gst.State.NULL)
    verdict = "HOLDS 30fps" if last["avg"] >= 29.5 else "below 30fps"
    print(f"[spike-b] playbin   RESULT: avg {last['avg']:5.1f} fps -> {verdict}",
          flush=True)


def main():
    ap = argparse.ArgumentParser(description="Spike B: 4K HEVC decode/present")
    ap.add_argument("--clip", default=None,
                    help="Path to a 4K HEVC .mp4 (default: tests/4k_hevc_test.mp4)")
    ap.add_argument("--decoder", default="auto",
                    help="'auto' (decodebin) or force e.g. 'avdec_h265'")
    ap.add_argument("--mode",
                    choices=("decode", "sweep", "display", "playbin",
                             "dmabuf", "warp"),
                    default="decode")
    ap.add_argument("--sink", default="glimagesink",
                    help="Sink for --mode display")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--seconds", type=float, default=8.0,
                    help="Seconds per sink (sweep/display)")
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()

    from pathlib import Path
    if args.clip is None:
        args.clip = str(Path(__file__).resolve().parent / "4k_hevc_test.mp4")
    if not os.path.exists(args.clip):
        print(f"[spike-b] clip not found: {args.clip}\n"
              f"          make one first: double-click 'Make 4K Test Clip.sh'",
              flush=True)
        return 1

    Gst.init(None)
    print(f"[spike-b] GStreamer {Gst.version_string()}", flush=True)
    if args.mode == "decode":
        run_decode(args.clip, args.decoder, args.frames)
    elif args.mode == "sweep":
        run_sweep(args.clip, args.decoder, args.seconds, args.fullscreen)
    elif args.mode == "playbin":
        run_playbin(args.clip, args.seconds)
    elif args.mode == "dmabuf":
        run_dmabuf(args.clip, args.seconds)
    elif args.mode == "warp":
        run_warp(args.clip, args.seconds)
    else:
        measure_sink(args.clip, args.decoder, args.sink, args.seconds,
                     args.fullscreen)
    print("[spike-b] DONE.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
