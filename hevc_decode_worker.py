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


def _conv_chains(w, h):
    # Detile the decoder's tiled SAND output -> packed RGB the client reads.
    # CRITICAL: this worker runs as a child of the pygame app, which already
    # holds a V3D GL context. A SECOND GL context (glupload/glcolorconvert)
    # silently produces ALL-BLACK frames on V3D — the dual-context blackout.
    # So the GL path is LAST resort; prefer the no-GL ISP detiler.
    #   pisp : pispconvert (Pi ISP HW detile) -> NV12 -> videoconvert -> RGB.
    #          No GL context, fast. pispconvert needs explicit output W/H and
    #          can't emit RGB itself, hence the trailing videoconvert.
    #   videoconvert : pure CPU; slow, and may not detile SAND on every build.
    #   gl   : glupload!glcolorconvert!gldownload; fast ONLY with no other GL
    #          context in play (e.g. standalone) — blacks out inside the app.
    return {
        "pisp": f"pispconvert ! video/x-raw,format=NV12,width={w},height={h} ! videoconvert",
        "videoconvert": "videoconvert",
        "gl": "glupload ! glcolorconvert ! gldownload ! videoconvert",
    }


CONV_ORDER = ["pisp", "videoconvert", "gl"]


def _try_build(path, chain, fmt, w, h):
    caps = f"video/x-raw,format={fmt}"
    if w and h:
        caps += f",width={w},height={h}"
    desc = (
        f'filesrc location="{gst_escape(path)}" ! qtdemux ! h265parse ! '
        f"v4l2slh265dec ! {chain} ! {caps} ! "
        "appsink name=sink sync=false max-buffers=2 drop=false"
    )
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    sink.set_property("emit-signals", False)
    pipeline.set_state(Gst.State.PLAYING)
    if pipeline.get_state(TIMEOUT_NS)[0] != Gst.StateChangeReturn.SUCCESS:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("did not reach PLAYING")
    return pipeline, sink


def build(path, fmt, w, h):
    chains = _conv_chains(w, h)
    forced = os.environ.get("VJ_HEVC_CONV", "").strip()
    order = [forced] if forced in chains else CONV_ORDER
    last = None
    for name in order:
        try:
            pipeline, sink = _try_build(path, chains[name], fmt, w, h)
            sys.stderr.write("[hevc-worker] using converter '%s'\n" % name)
            sys.stderr.flush()
            return pipeline, sink
        except Exception as exc:  # noqa: BLE001
            last = exc
            sys.stderr.write("[hevc-worker] converter '%s' failed: %r\n" % (name, exc))
            sys.stderr.flush()
    raise RuntimeError("no working converter (last: %r)" % (last,))


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
