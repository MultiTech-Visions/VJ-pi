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
    """Element chain for each candidate sink. glimagesink gets a glupload
    (which can import the decoder's DMABUF straight to GL); kms/wayland
    sinks take DMABUF directly."""
    if sink == "glimagesink":
        return "glupload ! glimagesink name=vsink force-aspect-ratio=true sync=false"
    if sink == "kmssink":
        return "kmssink name=vsink sync=false"
    if sink == "waylandsink":
        return "waylandsink name=vsink sync=false"
    return "autovideosink name=vsink sync=false"


def measure_sink(clip, decoder, sink, seconds, fullscreen):
    """Play the clip to one sink for `seconds`, counting buffers at the
    sink's pad (no text overlay) to get a clean on-screen fps."""
    desc = f'{_src_chain(clip, decoder)} ! queue ! {_sink_desc(sink)}'
    print(f"[spike-b] ---- sink={sink}: {desc}", flush=True)
    try:
        pipeline = Gst.parse_launch(desc)
    except GLib.Error as exc:
        print(f"[spike-b] {sink:13s} RESULT: not available ({exc})", flush=True)
        return

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
            print(f"[spike-b] {sink}: {fps:5.1f} fps", flush=True)
            st["last"] = now
        return Gst.PadProbeReturn.OK

    pad = vsink.get_static_pad("sink")
    if pad is not None:
        pad.add_probe(Gst.PadProbeType.BUFFER, _probe)

    loop = GLib.MainLoop()
    err = {"msg": None}

    def _bus(_b, msg):
        if msg.type == Gst.MessageType.ERROR:
            e, d = msg.parse_error()
            err["msg"] = f"{e.message}; {d}"
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _bus)

    pipeline.set_state(Gst.State.PLAYING)
    res, _, _ = pipeline.get_state(5 * Gst.SECOND)
    if fullscreen:
        try:
            vsink.set_property("fullscreen", True)
        except Exception:
            pass
    if res == Gst.StateChangeReturn.FAILURE:
        print(f"[spike-b] {sink:13s} RESULT: failed to start "
              f"({err['msg'] or 'state change failure'})", flush=True)
        pipeline.set_state(Gst.State.NULL)
        return
    _classify_decoder(pipeline)
    GLib.timeout_add_seconds(int(seconds), lambda: (loop.quit() or False))
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    if err["msg"]:
        print(f"[spike-b] {sink:13s} RESULT: error ({err['msg']})", flush=True)
    elif st["t0"] is not None and st["n"] > 0:
        fps = st["n"] / (time.perf_counter() - st["t0"])
        verdict = "HOLDS 30fps" if fps >= 29.5 else "below 30fps"
        print(f"[spike-b] {sink:13s} RESULT: {fps:5.1f} fps on screen "
              f"-> {verdict}", flush=True)
    else:
        print(f"[spike-b] {sink:13s} RESULT: no frames reached the sink",
              flush=True)


def run_sweep(clip, decoder, seconds, fullscreen):
    print(f"[spike-b] session: XDG_SESSION_TYPE="
          f"{os.environ.get('XDG_SESSION_TYPE')}, "
          f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}, "
          f"DISPLAY={os.environ.get('DISPLAY')}", flush=True)
    for sink in ("glimagesink", "waylandsink", "kmssink", "autovideosink"):
        try:
            measure_sink(clip, decoder, sink, seconds, fullscreen)
        except Exception as exc:  # noqa: BLE001
            print(f"[spike-b] {sink:13s} RESULT: exception {exc!r}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Spike B: 4K HEVC decode/present")
    ap.add_argument("--clip", default=None,
                    help="Path to a 4K HEVC .mp4 (default: tests/4k_hevc_test.mp4)")
    ap.add_argument("--decoder", default="auto",
                    help="'auto' (decodebin) or force e.g. 'avdec_h265'")
    ap.add_argument("--mode", choices=("decode", "sweep", "display"),
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
    if args.mode == "decode":
        run_decode(args.clip, args.decoder, args.frames)
    elif args.mode == "sweep":
        run_sweep(args.clip, args.decoder, args.seconds, args.fullscreen)
    else:
        measure_sink(args.clip, args.decoder, args.sink, args.seconds,
                     args.fullscreen)
    print("[spike-b] DONE.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
