"""Spike B — 4K HEVC decode throughput (THE decisive test).

Pure GStreamer, run under SYSTEM python3 (the one with `gi`) — exactly
like gpu_generator_worker.py. No pygame: this rig never mixes GStreamer's
`gi` bindings and pygame in one process (gi lives in system Python, pygame
in the venv), so this spike stays gi-only and the launcher runs it with
/usr/bin/python3.

THE QUESTION
------------
The Pi 5 has exactly ONE hardware video decoder: HEVC / H.265, up to
4K60. H.264 / VP9 / AV1 are software-only. So genuine "cinematic 4K"
lives or dies on: can the Pi HARDWARE-decode 4K H.265 fast enough to
feed the projector at 30 fps? This spike decodes a 4K HEVC clip as fast
as it can, times it, and — critically — reports WHICH decoder GStreamer
plugged, so we can tell hardware from software.

  --mode decode  (default): filesrc → decodebin → RGB → appsink, timed.
                 Pure decoder throughput. GL-free, most robust.
  --mode display          : filesrc → decodebin → glimagesink on screen,
                 fps measured by fpsdisplaysink. Tests the on-screen path.

NEED A 4K HEVC CLIP
-------------------
Double-click "Make 4K Test Clip.sh" once (writes tests/4k_hevc_test.mp4).

RUN
---
  python3 tests/spike_b_4k_decode.py                 # decode throughput
  python3 tests/spike_b_4k_decode.py --mode display  # + show on screen
  python3 tests/spike_b_4k_decode.py --decoder avdec_h265   # force software

WHAT TO REPORT BACK
-------------------
  * The "[spike-b] decoder plugged: ..." line — HARDWARE or SOFTWARE?
  * The "[spike-b] ... RESULT: NN.N fps" line.
  * The decoded frame size (should be 3840x2160).
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
    """Walk the running pipeline, print every element's factory name, and
    flag whether a hardware (V4L2/rpivid) or software decoder was plugged.
    This is the headline diagnostic of the whole spike."""
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
    print(f"[spike-b] pipeline elements: {', '.join(names)}", flush=True)
    if hw:
        print(f"[spike-b] decoder plugged: {', '.join(hw)}  -> HARDWARE", flush=True)
    elif sw:
        print(f"[spike-b] decoder plugged: {', '.join(sw)}  -> SOFTWARE "
              f"(no hw decode!)", flush=True)
    else:
        print(f"[spike-b] decoder plugged: {', '.join(decoders) or '??'}  "
              f"-> UNKNOWN (see element list above)", flush=True)


def _src_chain(clip, decoder):
    if decoder in (None, "", "auto"):
        # decodebin auto-plugs demux + parse + the best decoder (the
        # hardware HEVC decoder on a working Pi 5).
        return f'filesrc location="{clip}" ! decodebin'
    # Forced decoder: demux + parse ourselves, then the named element.
    return f'filesrc location="{clip}" ! qtdemux ! h265parse ! {decoder}'


def run_decode(clip, decoder, n_frames):
    """Decode as fast as possible to appsink; time the steady-state fps."""
    # NO videoconvert/RGB here. Forcing a 4K CPU colour-convert to RGB is
    # itself a ~6fps bottleneck and hides the decoder's real speed. Pull
    # the decoder's native output straight to appsink = pure decode.
    desc = (f'{_src_chain(clip, decoder)} ! '
            "appsink name=sink emit-signals=false max-buffers=2 drop=true sync=false")
    print(f"[spike-b] pipeline: {desc}", flush=True)
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    pipeline.get_state(5 * Gst.SECOND)   # let decodebin finish plugging
    _classify_decoder(pipeline)

    pulled = 0
    first_size = None
    t_start = None
    last_beat = None
    try:
        while pulled < n_frames:
            sample = sink.emit("try-pull-sample", 3 * Gst.SECOND)
            if sample is None:
                bus = pipeline.get_bus()
                msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg is not None and msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    print(f"[spike-b] ERROR: {err.message}; {dbg}", flush=True)
                else:
                    print(f"[spike-b] stream ended at {pulled} frames", flush=True)
                break
            now = time.perf_counter()
            if first_size is None:
                s = sample.get_caps().get_structure(0)
                first_size = (s.get_value("width"), s.get_value("height"))
            if t_start is None:        # start timing after first frame (warm-up)
                t_start = now
                last_beat = now
                pulled += 1
                continue
            pulled += 1
            if now - last_beat >= 1.0:
                fps = (pulled - 1) / (now - t_start)
                print(f"[spike-b] decode: {pulled - 1:4d} frames  "
                      f"avg {fps:5.1f} fps  size={first_size}", flush=True)
                last_beat = now
    finally:
        pipeline.set_state(Gst.State.NULL)

    if t_start is not None and pulled > 1:
        fps = (pulled - 1) / (time.perf_counter() - t_start)
        verdict = "holds 30fps" if fps >= 29.5 else "BELOW 30fps"
        print(f"[spike-b] decode   RESULT: {fps:5.1f} fps over {pulled - 1} "
              f"frames, size={first_size}  -> {verdict}", flush=True)
    else:
        print("[spike-b] decode   RESULT: no frames decoded", flush=True)


def run_display(clip, decoder, seconds, fullscreen):
    """Decode + show on screen via glimagesink; fpsdisplaysink measures the
    real on-screen present rate."""
    os.environ.setdefault("GST_GL_PLATFORM", "egl")
    fs = "true" if fullscreen else "false"
    desc = (f'{_src_chain(clip, decoder)} ! videoconvert ! '
            f"fpsdisplaysink name=fps video-sink=\"glimagesink force-aspect-ratio=true\" "
            f"text-overlay=true signal-fps-measurements=true sync=false")
    print(f"[spike-b] pipeline: {desc}", flush=True)
    pipeline = Gst.parse_launch(desc)
    try:
        pipeline.get_by_name("fps").set_property("fullscreen", fullscreen)
    except Exception:
        pass

    last = {"avg": 0.0}

    def on_fps(_sink, fps, droprate, avgfps):
        last["avg"] = avgfps
        print(f"[spike-b] display: {fps:5.1f} fps  (avg {avgfps:5.1f}, "
              f"drop {droprate:5.1f})", flush=True)
    try:
        pipeline.get_by_name("fps").connect("fps-measurements", on_fps)
    except Exception:
        pass

    loop = GLib.MainLoop()

    def on_bus(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[spike-b] ERROR: {err.message}; {dbg}", flush=True)
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            print("[spike-b] display: clip ended", flush=True)
            loop.quit()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_bus)

    pipeline.set_state(Gst.State.PLAYING)
    pipeline.get_state(5 * Gst.SECOND)
    _classify_decoder(pipeline)
    GLib.timeout_add_seconds(int(seconds), lambda: (loop.quit() or False))
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
    verdict = "holds 30fps" if last["avg"] >= 29.5 else "below 30fps"
    print(f"[spike-b] display  RESULT: avg {last['avg']:5.1f} fps on screen "
          f"-> {verdict}", flush=True)
    print(f"[spike-b] display: done ({seconds:.0f}s). Smooth ~30fps with a "
          "hardware decoder = 4K cinematic is real.", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Spike B: 4K HEVC decode throughput")
    ap.add_argument("--clip", default=None,
                    help="Path to a 4K HEVC .mp4 (default: tests/4k_hevc_test.mp4)")
    ap.add_argument("--decoder", default="auto",
                    help="'auto' (decodebin, auto hw) or force an element, "
                         "e.g. 'v4l2slh265dec' or 'avdec_h265'")
    ap.add_argument("--mode", choices=("decode", "display"), default="decode")
    ap.add_argument("--frames", type=int, default=300,
                    help="Frames to time in decode mode (default 300)")
    ap.add_argument("--seconds", type=float, default=15.0,
                    help="Seconds to run in display mode (default 15)")
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()

    from pathlib import Path
    if args.clip is None:
        args.clip = str(Path(__file__).resolve().parent / "4k_hevc_test.mp4")
    if not os.path.exists(args.clip):
        print(f"[spike-b] clip not found: {args.clip}\n"
              f"          make one first: double-click 'Make 4K Test Clip.sh'\n"
              f"          (or run ./tests/make_4k_test_clip.sh)", flush=True)
        return 1

    Gst.init(None)
    if args.mode == "decode":
        run_decode(args.clip, args.decoder, args.frames)
    else:
        run_display(args.clip, args.decoder, args.seconds, args.fullscreen)
    print("[spike-b] DONE.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
