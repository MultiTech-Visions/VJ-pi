"""Out-of-process HEVC hardware-decode worker (pipelined / shared-memory).

The Pi 5 HW-decodes HEVC but the decoder emits a tiled SAND format; pispconvert
(Pi ISP) detiles + colour-converts it robustly at any resolution. We run that in
a SEPARATE process so the main pygame/OpenCV app never holds a 2nd GL/V3D
context (the dual-context rule), then hand frames to the main process through
shared memory (/dev/shm) — no per-frame copy down a pipe.

    filesrc ! qtdemux ! h265parse ! v4l2slh265dec ! pispconvert ! RGB ! appsink

Runs under system Python (gi/GStreamer). Protocol: the client creates the shm
file and a ring of N slots, spawns us with (clip, shm_path, W, H, slots, fmt),
then per frame writes a slot index as an ascii line on stdin; we decode one
frame into that slot and reply "1\\n" (ok) or "0\\n" (error). Looping the clip
on EOS so the client can pull forever.
"""
import mmap
import os
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

TIMEOUT_NS = 5 * Gst.SECOND


def gst_escape(path):
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build(path, fmt, w, h):
    # gl converter: HW decode + GPU detile/convert + readback. FAST, but only
    # negotiates the Pi SAND format at certain geometries — proven solid at
    # 2048x1152 (the bake target), fails at 1080p/4K. Bake clips to 2048x1152
    # and this is the fast path; pispconvert is the robust-but-slow fallback.
    desc = (
        f'filesrc location="{gst_escape(path)}" ! qtdemux ! h265parse ! '
        "v4l2slh265dec ! glupload ! glcolorconvert ! gldownload ! "
        f"videoconvert ! video/x-raw,format={fmt} ! "
        "appsink name=sink sync=false max-buffers=2 drop=false"
    )
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    sink.set_property("emit-signals", False)
    pipeline.set_state(Gst.State.PLAYING)
    if pipeline.get_state(TIMEOUT_NS)[0] != Gst.StateChangeReturn.SUCCESS:
        raise RuntimeError("pipeline failed to reach PLAYING")
    return pipeline, sink


def pull_into(pipeline, sink, mm, off, nbytes):
    """Decode one frame (looping on EOS) into mm[off:off+nbytes]. True on ok."""
    for _ in range(2):
        sample = sink.emit("try-pull-sample", TIMEOUT_NS)
        if sample is not None:
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return False
            n = min(nbytes, info.size)
            mm[off:off + n] = info.data[:n]
            buf.unmap(info)
            return True
        pipeline.seek_simple(Gst.Format.TIME,
                             Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
    return False


def main(argv):
    if len(argv) < 7:
        sys.stderr.write("usage: worker CLIP SHM_PATH W H SLOTS FMT\n")
        return 2
    clip, shm_path = argv[1], argv[2]
    w, h, slots = int(argv[3]), int(argv[4]), int(argv[5])
    fmt = argv[6]
    bpp = 4 if fmt in ("RGBx", "BGRx", "RGBA", "BGRA") else 3
    fb = w * h * bpp

    Gst.init(None)
    fd = os.open(shm_path, os.O_RDWR)
    mm = mmap.mmap(fd, slots * fb)
    out = sys.stdout.buffer

    try:
        pipeline, sink = build(clip, fmt, w, h)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[hevc-worker] build failed: {exc!r}\n")
        out.write(b"0\n"); out.flush()
        return 1

    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            slot = int(line)
        except ValueError:
            continue
        ok = pull_into(pipeline, sink, mm, slot * fb, fb)
        out.write(b"1\n" if ok else b"0\n")
        out.flush()

    pipeline.set_state(Gst.State.NULL)
    mm.close()
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
