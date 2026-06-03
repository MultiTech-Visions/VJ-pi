#!/usr/bin/env python3
"""Decode-path benchmark — does HEVC hardware decode beat H.264 software
decode *once the frame is back in CPU memory as a BGR numpy array*?

Context: the main VJ app's FX/warp/composite pipeline is numpy/OpenCV, so
every clip frame has to land in CPU RAM. Today that means H.264 software
decode via cv2.VideoCapture. The Pi 5 has no H.264 hardware decode but DOES
have a hardware HEVC (H.265) decoder — the open question is whether
HW-decoding HEVC and pulling the frame back to CPU (the "readback") costs
LESS CPU than today's H.264 software decode. If it does, we have headroom to
raise the base canvas to 1080p (or feed 4K detail) while keeping the FX chain.

The catch (see research notes in the commit): the Pi 5 HEVC decoder emits a
tiled "SAND" format (NV12_128C8), so the readback needs a detile + colour
convert. The cost of that step is the whole ballgame, and it differs wildly
by converter — so we measure each:

    * videoconvert  — pure CPU convert (likely the slow trap)
    * gl            — glupload ! glcolorconvert ! gldownload (what cinematic
                      mode already uses on the GPU)
    * pispconvert   — the Pi ISP hardware detiler (needs gstreamer1.0-pispconvert)

Plus an ffmpeg `-hwaccel drm` path (the more robust Pi-5 HEVC route) and the
two OpenCV/H.264 baselines (720p = today, 1080p = the naive bump).

This script measures ONE path per invocation so the launcher can run the
OpenCV paths under the venv (cv2) and the GStreamer paths under system Python
(gi/Gst). Each run prints a single RESULT line; read them all from the log.

Modes:
    bench_decode.py prep   --source SRC --outdir DIR [--with-4k]
    bench_decode.py opencv FILE
    bench_decode.py ffmpeg FILE
    bench_decode.py gst    FILE --conv {videoconvert,gl,pisp}

Decision metric: FPS vs the operator's 13 fps floor, and CPU% (lower = more
headroom for FX). Nothing here touches the live app — it's a standalone probe.
"""
import argparse
import os
import subprocess
import sys
import time

FLOOR_FPS = 13.0          # operator's stated minimum acceptable framerate
DEFAULT_FRAMES = 300      # timed frames after warmup
DEFAULT_WARMUP = 30       # frames discarded before timing (fill caches/pipeline)


# ── dependency-free CPU sampling ─────────────────────────────────────────
# Reads the aggregate /proc/stat cpu line; returns (busy, total) jiffies.
# The heavy decode work lives in other processes / the kernel, so per-process
# accounting would miss it — system-wide busy% is the honest headroom number.
def _cpu_jiffies():
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError:
        return None
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    total = sum(vals)
    return total - idle, total


class CpuMeter:
    def __init__(self):
        self.start = _cpu_jiffies()

    def percent(self):
        end = _cpu_jiffies()
        if not self.start or not end:
            return float("nan")
        dbusy = end[0] - self.start[0]
        dtotal = end[1] - self.start[1]
        return 100.0 * dbusy / dtotal if dtotal else float("nan")


def report(label, frames, elapsed, cpu_pct, dims="", note=""):
    fps = frames / elapsed if elapsed > 0 else 0.0
    ms = 1000.0 * elapsed / frames if frames else 0.0
    verdict = "PASS" if fps >= FLOOR_FPS else "FAIL"
    extra = f" {dims}" if dims else ""
    extra += f" :: {note}" if note else ""
    print(
        f"RESULT {label:<28} fps={fps:6.1f} [{verdict}]  "
        f"cpu%={cpu_pct:5.1f}  ms/frame={ms:6.2f}  frames={frames}{extra}",
        flush=True,
    )


def probe_dims(path):
    """Return (width, height) via ffprobe, or (None, None)."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
             str(path)],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None, None


# ── OpenCV / H.264 software baseline (the current clips.py path) ──────────
def measure_opencv(path, frames, warmup, label):
    import cv2  # venv
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print(f"RESULT {label:<28} SKIP :: cv2 could not open {path}", flush=True)
        return
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def grab():
        ok, fr = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, fr = cap.read()
        return fr

    for _ in range(warmup):
        if grab() is None:
            break
    meter = CpuMeter()
    t0 = time.perf_counter()
    n = 0
    for _ in range(frames):
        fr = grab()
        if fr is None:
            break
        n += 1
    elapsed = time.perf_counter() - t0
    cpu = meter.percent()
    cap.release()
    report(label, n, elapsed, cpu, dims=f"{w}x{h}")


# ── ffmpeg -hwaccel drm → raw bgr24 pipe → numpy ─────────────────────────
def measure_ffmpeg(path, frames, warmup, label):
    import numpy as np  # venv
    w, h = probe_dims(path)
    if not w:
        print(f"RESULT {label:<28} SKIP :: ffprobe failed on {path}", flush=True)
        return
    frame_bytes = w * h * 3
    cmd = [
        "ffmpeg", "-v", "error", "-stream_loop", "-1",
        "-hwaccel", "drm", "-i", str(path),
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=frame_bytes)

    def read_frame():
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            return None
        # .copy() mirrors handing a real contiguous frame to the FX pipeline.
        return np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()

    try:
        for _ in range(warmup):
            if read_frame() is None:
                break
        meter = CpuMeter()
        t0 = time.perf_counter()
        n = 0
        for _ in range(frames):
            if read_frame() is None:
                break
            n += 1
        elapsed = time.perf_counter() - t0
        cpu = meter.percent()
    finally:
        proc.kill()
        err = proc.stderr.read().decode(errors="replace").strip()
    if n == 0:
        print(f"RESULT {label:<28} FAIL :: no frames "
              f"(hwaccel drm unavailable?) :: {err[:200]}", flush=True)
        return
    report(label, n, elapsed, cpu, dims=f"{w}x{h}")


# ── GStreamer v4l2slh265dec → BGR appsink → numpy ────────────────────────
CONV_CHAINS = {
    # CPU-only convert. Likely slow / may not detile SAND — that's the point.
    "videoconvert": "videoconvert",
    # GPU detile+convert then read back to CPU (cinematic mode's converter,
    # plus a trailing videoconvert to guarantee BGR for the appsink).
    "gl": "glupload ! glcolorconvert ! gldownload ! videoconvert",
    # Pi ISP hardware detiler. Needs gstreamer1.0-pispconvert installed.
    "pisp": "pispconvert",
}


def measure_gst(path, conv, frames, warmup, label):
    import gi  # system python
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    try:
        import numpy as np
        have_np = True
    except Exception:
        have_np = False

    w, h = probe_dims(path)
    chain = CONV_CHAINS[conv]
    # pispconvert REQUIRES explicit output W/H (per Pi docs); harmless elsewhere.
    caps = "video/x-raw,format=BGR"
    if w and h:
        caps += f",width={w},height={h}"
    src = str(path).replace("\\", "\\\\").replace('"', '\\"')
    desc = (
        f'filesrc location="{src}" ! qtdemux ! h265parse ! v4l2slh265dec ! '
        f'{chain} ! {caps} ! '
        f'appsink name=sink sync=false max-buffers=3 drop=false'
    )
    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f"RESULT {label:<28} SKIP :: pipeline build failed "
              f"(element missing?) :: {exc}", flush=True)
        return
    sink = pipeline.get_by_name("sink")
    sink.set_property("emit-signals", False)
    bus = pipeline.get_bus()
    TIMEOUT_NS = 5 * Gst.SECOND   # never block longer than this on one frame

    def bus_error():
        msg = bus.timed_pop_filtered(0, Gst.MessageType.ERROR)
        if msg:
            err, _dbg = msg.parse_error()
            return err.message
        return None

    # Start, and confirm it actually reaches PLAYING. A converter that can't
    # handle the decoder's tiled SAND output stalls negotiation — catch that
    # here instead of blocking forever on the first pull.
    pipeline.set_state(Gst.State.PLAYING)
    st = pipeline.get_state(TIMEOUT_NS)[0]
    if st != Gst.StateChangeReturn.SUCCESS:
        emsg = bus_error() or f"did not reach PLAYING within 5s ({st!r})"
        print(f"RESULT {label:<28} FAIL :: {emsg} (conv={conv})", flush=True)
        pipeline.set_state(Gst.State.NULL)
        return

    def pull():
        # try-pull-sample returns None on timeout OR EOS — never blocks forever.
        sample = sink.emit("try-pull-sample", TIMEOUT_NS)
        if sample is None:
            return False
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        # Force the readback into a real CPU frame, as the FX pipeline would.
        if have_np:
            _ = np.frombuffer(info.data, np.uint8).copy()
        else:
            _ = bytes(info.data)
        buf.unmap(info)
        return True

    try:
        # Warm up, bailing cleanly (with the bus error, if any) on a stall.
        for _ in range(warmup):
            if not pull():
                emsg = bus_error() or "no frame within 5s (pipeline stalled)"
                print(f"RESULT {label:<28} FAIL :: {emsg} (conv={conv})", flush=True)
                pipeline.set_state(Gst.State.NULL)
                return
        meter = CpuMeter()
        t0 = time.perf_counter()
        got = 0
        for _ in range(frames):
            if not pull():
                break
            got += 1
        elapsed = time.perf_counter() - t0
        cpu = meter.percent()
    finally:
        pipeline.set_state(Gst.State.NULL)

    if got == 0:
        emsg = bus_error() or "no frames pulled"
        print(f"RESULT {label:<28} FAIL :: {emsg} (conv={conv})", flush=True)
        return
    report(label, got, elapsed, cpu, dims=f"{w}x{h}", note=f"conv={conv}")


# ── prep: build matched test clips from one source ───────────────────────
def run_ffmpeg(args):
    print("[bench] ffmpeg " + " ".join(args), flush=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def prep(source, outdir, with_4k):
    os.makedirs(outdir, exist_ok=True)
    made = {}
    common_in = ["-stream_loop", "-1", "-i", str(source), "-t", "15", "-an"]
    targets = [
        ("h264_720p.mp4",  "1280:720",  "libx264"),
        ("h264_1080p.mp4", "1920:1080", "libx264"),
        ("h265_720p.mp4",  "1280:720",  "libx265"),
        ("h265_1080p.mp4", "1920:1080", "libx265"),
    ]
    if with_4k:
        targets.append(("h265_2160p.mp4", "3840:2160", "libx265"))
    for name, scale, codec in targets:
        out = os.path.join(outdir, name)
        if os.path.exists(out):
            print(f"[bench] keep existing {name}", flush=True)
            made[name] = out
            continue
        try:
            run_ffmpeg([*common_in, "-vf", f"scale={scale}",
                        "-c:v", codec, "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", out])
            made[name] = out
        except subprocess.CalledProcessError as exc:
            print(f"[bench] FAILED to build {name}: {exc}", flush=True)
    print("[bench] prep done: " + ", ".join(sorted(made)), flush=True)
    return made


def main(argv):
    p = argparse.ArgumentParser(description="VJ-pi decode-path benchmark")
    sub = p.add_subparsers(dest="mode", required=True)

    pp = sub.add_parser("prep")
    pp.add_argument("--source", required=True)
    pp.add_argument("--outdir", default="bench_assets")
    pp.add_argument("--with-4k", action="store_true")

    for name in ("opencv", "ffmpeg"):
        sp = sub.add_parser(name)
        sp.add_argument("file")
        sp.add_argument("--label", default=None)
        sp.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
        sp.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)

    sg = sub.add_parser("gst")
    sg.add_argument("file")
    sg.add_argument("--conv", choices=list(CONV_CHAINS), required=True)
    sg.add_argument("--label", default=None)
    sg.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    sg.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)

    a = p.parse_args(argv)

    if a.mode == "prep":
        prep(a.source, a.outdir, a.with_4k)
        return 0
    if a.mode == "opencv":
        measure_opencv(a.file, a.frames, a.warmup,
                       a.label or f"opencv {os.path.basename(a.file)}")
        return 0
    if a.mode == "ffmpeg":
        measure_ffmpeg(a.file, a.frames, a.warmup,
                       a.label or f"ffmpeg-drm {os.path.basename(a.file)}")
        return 0
    if a.mode == "gst":
        measure_gst(a.file, a.conv, a.frames, a.warmup,
                    a.label or f"gst-{a.conv} {os.path.basename(a.file)}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
