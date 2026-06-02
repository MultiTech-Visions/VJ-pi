"""Spike B — 4K HEVC decode → GPU present throughput (THE decisive test).

THE QUESTION
------------
`--gpu-scale` already proves the Pi can cheaply *upscale* a small canvas
to a 4K projector. But upscaling 720p to 4K adds no detail. For genuine
"cinematic 4K" the decoded frame itself has to be 4K — and that lands on
the one hardware fact that defines this board:

    The Pi 5 has exactly ONE hardware video decoder: HEVC / H.265, up to
    4K60. H.264 / VP9 / AV1 are software-only. (The H.264 block was
    removed.)

So the cinematic dream lives or dies on this: can the Pi hardware-decode
4K H.265 and get those frames onto the projector at 30 fps? This spike
measures exactly that, on the production stack (GStreamer decode → SDL2
streaming Texture present), and — critically — reports WHICH decoder
GStreamer actually plugged, so we can tell hardware from software.

It runs up to three measurements so we can see where any cost is:
  1. decode-only      : GStreamer decodes 4K HEVC, frames pulled to RAM,
                        nothing presented. Pure decoder throughput.
  2. decode + upload  : + each frame uploaded to an SDL2 streaming
                        texture (the ~24 MB/frame CPU→GPU bounce).
  3. decode + present : + drawn to the projector window (full path).

NEED A 4K HEVC CLIP
-------------------
Point it at one with --clip, or make a synthetic one first:
    ./tests/make_4k_test_clip.sh           # writes tests/4k_hevc_test.mp4

RUN (on the Pi)
---------------
    ./venv/bin/python tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 \
        --output-display 1 --fullscreen
Force software decode for an A/B comparison:
    ./venv/bin/python tests/spike_b_4k_decode.py --clip CLIP --decoder avdec_h265
Decode-only (no window needed):
    ./venv/bin/python tests/spike_b_4k_decode.py --clip CLIP --mode decode

WHAT TO REPORT BACK
-------------------
  * The "[spike-b] decoder plugged: ..." line — is it a V4L2/hardware
    decoder (e.g. v4l2slh265dec) or software (avdec_h265 / libav)?
  * The fps numbers for each mode.
  * Decoded frame size (should be 3840x2160).
  * Any '[spike-b]' error lines.
"""
import argparse
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def _build_pipeline(clip, decoder):
    """filesrc → demux/parse → (decoder) → RGB → appsink.

    `decodebin` auto-plugs the best available decoder, which on a working
    Pi 5 should be the V4L2 stateless HEVC hardware decoder. Passing an
    explicit --decoder forces a specific element so we can A/B hardware
    vs software (avdec_h265)."""
    if decoder in (None, "", "auto"):
        # decodebin auto-plugs demux + parse + the best decoder (the
        # hardware HEVC decoder on a working Pi 5), so feed it filesrc.
        src = f'filesrc location="{clip}"'
        dec_chain = "decodebin"
    else:
        # Forced decoder: we demux + parse ourselves, then the element.
        src = f'filesrc location="{clip}" ! qtdemux ! h265parse'
        dec_chain = decoder
    desc = (
        f'{src} ! {dec_chain} ! '
        "videoconvert ! video/x-raw,format=RGB ! "
        "appsink name=sink emit-signals=false max-buffers=2 drop=false sync=false"
    )
    return desc


def _report_decoder(pipeline):
    """Walk the running pipeline and print every element's factory name,
    flagging whether a hardware (V4L2/rpivid) or software decoder was
    plugged. This is the headline diagnostic of the whole spike."""
    names = []
    it = pipeline.iterate_recurse()
    while True:
        ok, elem = it.next()
        if ok == Gst.IteratorResult.OK:
            factory = elem.get_factory()
            fname = factory.get_name() if factory else "?"
            names.append(fname)
        elif ok == Gst.IteratorResult.DONE:
            break
        else:
            break
    decoders = [n for n in names if "dec" in n.lower()]
    hw = [n for n in decoders
          if any(k in n.lower() for k in ("v4l2", "rpivid", "drm", "sl"))]
    sw = [n for n in decoders if any(k in n.lower() for k in ("avdec", "libav"))]
    print(f"[spike-b] pipeline elements: {', '.join(names)}", flush=True)
    if hw:
        print(f"[spike-b] decoder plugged: {', '.join(hw)}  → HARDWARE ✓",
              flush=True)
    elif sw:
        print(f"[spike-b] decoder plugged: {', '.join(sw)}  → SOFTWARE "
              f"(no hw decode!)", flush=True)
    else:
        print(f"[spike-b] decoder plugged: {', '.join(decoders) or '??'}  "
              f"→ UNKNOWN (inspect element list above)", flush=True)


def _pull(sink, seconds=2.0):
    sample = sink.emit("try-pull-sample", int(seconds * Gst.SECOND))
    return sample


def main():
    ap = argparse.ArgumentParser(description="Spike B: 4K HEVC decode throughput")
    ap.add_argument("--clip", required=True, help="Path to a 4K HEVC .mp4")
    ap.add_argument("--decoder", default="auto",
                    help="'auto' (decodebin, auto hw), or force an element "
                         "e.g. 'v4l2slh265dec' or 'avdec_h265'")
    ap.add_argument("--mode", choices=("decode", "upload", "present", "all"),
                    default="all", help="Which measurement(s) to run")
    ap.add_argument("--frames", type=int, default=300,
                    help="Frames to time per mode (default 300 ≈ 10s @30fps)")
    ap.add_argument("--output-display", type=int, default=0)
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()

    import os
    if not os.path.exists(args.clip):
        print(f"[spike-b] clip not found: {args.clip}\n"
              f"          make one with ./tests/make_4k_test_clip.sh", flush=True)
        return 1

    Gst.init(None)
    desc = _build_pipeline(args.clip, args.decoder)
    print(f"[spike-b] pipeline: {desc}", flush=True)

    modes = (["decode", "upload", "present"] if args.mode == "all"
             else [args.mode])
    # 'present'/'upload' need an output window + SDL renderer; set up once.
    gpu = None
    if any(m in ("upload", "present") for m in modes):
        gpu = _setup_gpu_output(args)
        if gpu is None and "present" in modes:
            print("[spike-b] no GPU output; dropping 'present'/'upload' modes",
                  flush=True)
            modes = [m for m in modes if m == "decode"] or ["decode"]

    for mode in modes:
        _run_mode(desc, mode, args.frames, gpu)

    if gpu is not None:
        import pygame
        pygame.quit()
    print("[spike-b] DONE.", flush=True)
    return 0


def _setup_gpu_output(args):
    try:
        import pygame
        os_env = __import__("os").environ
        os_env.setdefault("SDL_HINT_GRAB_KEYBOARD", "0")
        pygame.init()
        from pygame._sdl2.video import Window, Renderer, Texture
        if args.fullscreen:
            try:
                size = pygame.display.get_desktop_sizes()[args.output_display]
            except (pygame.error, IndexError, AttributeError):
                size = (1920, 1080)
        else:
            size = (1280, 720)
        pos = (0x2FFF0000 | (args.output_display & 0xFFFF),
               0x2FFF0000 | (args.output_display & 0xFFFF))
        win = None
        for kwargs in ({"size": size, "position": pos,
                        "borderless": bool(args.fullscreen)},
                       {"size": size, "position": pos}, {"size": size}):
            try:
                win = Window("Spike B — 4K OUTPUT", **kwargs)
                break
            except TypeError:
                continue
        win.show()
        renderer = Renderer(win)
        print(f"[spike-b] GPU output window {size} on display "
              f"{args.output_display}", flush=True)
        return {"pygame": pygame, "Texture": Texture, "renderer": renderer,
                "tex": None, "size": None}
    except Exception as exc:
        print(f"[spike-b] GPU output unavailable: {exc!r}", flush=True)
        return None


def _run_mode(desc, mode, n_frames, gpu):
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    # Wait for preroll so the decoder is actually plugged before we report.
    pipeline.get_state(5 * Gst.SECOND)
    if mode == "decode":
        _report_decoder(pipeline)

    pulled = 0
    first_size = None
    t_start = None
    last_beat = None
    try:
        while pulled < n_frames:
            sample = _pull(sink, 2.0)
            if sample is None:
                bus = pipeline.get_bus()
                msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg is not None and msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    print(f"[spike-b] ERROR: {err.message}; {dbg}", flush=True)
                    break
                # EOS (clip ended) — loop by restarting, or just stop.
                print(f"[spike-b] {mode}: stream ended at {pulled} frames",
                      flush=True)
                break
            now = time.perf_counter()
            if t_start is None:  # start timing after first frame (warm-up)
                t_start = now
                last_beat = now
                pulled += 1
                continue

            if mode in ("upload", "present") and gpu is not None:
                buf = sample.get_buffer()
                caps = sample.get_caps().get_structure(0)
                w = caps.get_value("width")
                h = caps.get_value("height")
                if first_size is None:
                    first_size = (w, h)
                ok, info = buf.map(Gst.MapFlags.READ)
                if ok:
                    try:
                        _present(gpu, info.data, w, h, draw=(mode == "present"))
                    finally:
                        buf.unmap(info)
                gpu["pygame"].event.pump()
            else:
                caps = sample.get_caps().get_structure(0)
                if first_size is None:
                    first_size = (caps.get_value("width"),
                                  caps.get_value("height"))

            pulled += 1
            if now - last_beat >= 1.0:
                fps = (pulled - 1) / (now - t_start)
                print(f"[spike-b] {mode}: {pulled - 1:4d} frames  "
                      f"avg {fps:5.1f}fps  size={first_size}", flush=True)
                last_beat = now
    finally:
        pipeline.set_state(Gst.State.NULL)

    if t_start is not None and pulled > 1:
        elapsed = time.perf_counter() - t_start
        fps = (pulled - 1) / elapsed if elapsed > 0 else 0.0
        verdict = "✓ holds 30fps" if fps >= 29.5 else "✗ below 30fps"
        print(f"[spike-b] {mode:8s} RESULT: {fps:5.1f} fps over "
              f"{pulled - 1} frames, size={first_size}  {verdict}", flush=True)


def _present(gpu, data, w, h, draw):
    pygame = gpu["pygame"]
    renderer = gpu["renderer"]
    if gpu["tex"] is None or gpu["size"] != (w, h):
        gpu["tex"] = gpu["Texture"](renderer, (w, h), streaming=True)
        gpu["size"] = (w, h)
        try:
            renderer.logical_size = (w, h)
        except Exception:
            pass
    surf = pygame.image.frombuffer(bytes(data), (w, h), "RGB")
    gpu["tex"].update(surf)
    if draw:
        renderer.clear()
        gpu["tex"].draw()
        renderer.present()


if __name__ == "__main__":
    sys.exit(main())
