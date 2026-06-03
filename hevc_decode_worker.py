"""Out-of-process HEVC hardware-decode worker (SPIKE).

Proves the winning bench path end-to-end as a real worker: the Pi 5's HW
HEVC decoder + GPU detile/convert, frame read back to CPU and shipped to the
main process over a pipe — so the main pygame/OpenCV app never holds a 2nd GL
context (the V3D dual-context rule stays intact, like gpu_generator_worker.py).

Runs under system Python (gi/GStreamer live there on the Pi). Protocol mirrors
gpu_generators: client writes one request line on stdin per frame; we reply on
stdout with a JSON header line {"ok",...,"n","width","height"} then n raw BGR
bytes. Loops the clip on EOS so the client can pull indefinitely.

    filesrc ! qtdemux ! h265parse ! v4l2slh265dec !
    glupload ! glcolorconvert ! gldownload ! videoconvert ! BGR ! appsink
"""
import json
import subprocess
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

TIMEOUT_NS = 5 * Gst.SECOND


def gst_escape(path):
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def probe_dims(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        stderr=subprocess.DEVNULL).decode().strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def build(path, fmt="RGB"):
    # pispconvert = the Pi ISP hardware detiler: turns v4l2slh265dec's tiled
    # SAND output into a normal packed format (RGB here) entirely on the ISP,
    # robustly at any resolution — unlike the gl uploader, which fails to map
    # the SAND format at some geometries (e.g. 1920x1080). Needs explicit
    # width/height on its output caps.
    w, h = probe_dims(path)
    desc = (
        f'filesrc location="{gst_escape(path)}" ! qtdemux ! h265parse ! '
        f"v4l2slh265dec ! pispconvert ! "
        f"video/x-raw,format={fmt},width={w},height={h} ! "
        "appsink name=sink sync=false max-buffers=2 drop=false"
    )
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    sink.set_property("emit-signals", False)
    pipeline.set_state(Gst.State.PLAYING)
    if pipeline.get_state(TIMEOUT_NS)[0] != Gst.StateChangeReturn.SUCCESS:
        raise RuntimeError("pipeline failed to reach PLAYING")
    return pipeline, sink


def pull_frame(pipeline, sink):
    """One BGR sample, looping the clip on EOS. Returns (bytes, w, h) or None."""
    for _ in range(2):
        sample = sink.emit("try-pull-sample", TIMEOUT_NS)
        if sample is not None:
            buf = sample.get_buffer()
            caps = sample.get_caps().get_structure(0)
            w = caps.get_value("width")
            h = caps.get_value("height")
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            data = bytes(info.data)
            buf.unmap(info)
            return data, w, h
        # None → likely EOS; flush-seek to the start and try once more.
        pipeline.seek_simple(Gst.Format.TIME,
                             Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
    return None


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: hevc_decode_worker.py CLIP\n")
        return 2
    Gst.init(None)
    fmt = argv[2] if len(argv) > 2 else "RGB"
    out = sys.stdout.buffer
    try:
        pipeline, sink = build(argv[1], fmt)
    except Exception as exc:  # noqa: BLE001
        out.write((json.dumps({"ok": False, "error": repr(exc)}) + "\n").encode())
        out.flush()
        return 1

    for _line in sys.stdin.buffer:          # one frame per request line
        frame = pull_frame(pipeline, sink)
        if frame is None:
            out.write((json.dumps({"ok": False, "error": "no frame"}) + "\n").encode())
            out.flush()
            continue
        data, w, h = frame
        hdr = json.dumps({"ok": True, "n": len(data), "width": w, "height": h})
        out.write((hdr + "\n").encode())
        out.write(data)
        out.flush()

    pipeline.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
