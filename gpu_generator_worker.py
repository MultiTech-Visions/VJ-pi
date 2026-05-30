"""Out-of-process GStreamer/GL generator renderer."""
import json
import random
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from shader_catalog import GPU_GENERATORS


HERE = Path(__file__).resolve().parent
IMAGES_DIR = HERE / "assets" / "images"
_texture_cycle = []
_texture_idx = 0


def _next_texture_image():
    global _texture_cycle, _texture_idx
    if not IMAGES_DIR.exists():
        return None
    paths = []
    for suffix in ("*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.JPEG"):
        paths.extend(IMAGES_DIR.glob(suffix))
    paths = sorted(paths)
    if not paths:
        return None
    if paths != sorted(_texture_cycle):
        _texture_cycle = paths[:]
        random.shuffle(_texture_cycle)
        _texture_idx = 0
    image = _texture_cycle[_texture_idx % len(_texture_cycle)]
    _texture_idx += 1
    return image


class Renderer:
    def __init__(self):
        Gst.init(None)
        self.pipeline = None
        self.shader = None
        self.sink = None
        self.current = None
        # Rotation phase for lantern generators, integrated here so changing
        # the spin velocity never makes the angle jump. One worker process
        # serves one generator name, so this phase belongs to that lantern.
        self._phase = 0.0
        self._phase_t = None

    def close(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.shader = None
        self.sink = None
        self.current = None

    def pause(self):
        # Stop the GL pipeline from churning V3D while nobody is pulling
        # frames (e.g. during blackout / freeze). PAUSED keeps the built
        # pipeline so render() can resume instantly without a rebuild.
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.PAUSED)

    def ensure(self, name, width, height, token):
        key = (name, width, height, token if name == "donut" else None)
        if self.current == key and self.pipeline is not None:
            return
        self.close()
        shader = GPU_GENERATORS[name]
        if name == "donut":
            image = _next_texture_image()
            if image is None:
                raise RuntimeError("donut needs an image in assets/images/")
            print(f"[gpu-worker] donut texture: {image.name}", file=sys.stderr, flush=True)
            uri = Gst.filename_to_uri(str(image))
            desc = (
                f'uridecodebin uri="{uri}" ! '
                "videoconvert ! imagefreeze ! videoscale ! "
                f"video/x-raw,width={width},height={height},framerate=30/1 ! "
                "glupload ! glshader name=shader ! "
                "gldownload ! videoconvert ! "
                "video/x-raw,format=RGB ! "
                "appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
            )
        else:
            desc = (
                "videotestsrc is-live=true pattern=black ! "
                f"video/x-raw,width={width},height={height},framerate=30/1 ! "
                "glupload ! glshader name=shader ! "
                "gldownload ! videoconvert ! "
                "video/x-raw,format=RGB ! "
                "appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
            )
        self.pipeline = Gst.parse_launch(desc)
        self.shader = self.pipeline.get_by_name("shader")
        self.sink = self.pipeline.get_by_name("sink")
        self.shader.set_property("fragment", shader)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.current = key

    def _set_lantern_uniforms(self, led_hue, led_sat, spin):
        """Push the tunable lantern uniforms onto the glshader. Spin is a
        velocity (rad/s); we integrate it into a continuous phase here, with
        dt clamped so a long pause (blackout) can't fling the rotation."""
        now = time.monotonic()
        dt = 0.0 if self._phase_t is None else min(max(now - self._phase_t, 0.0), 0.1)
        self._phase_t = now
        self._phase += spin * dt
        desc = ("uniforms"
                ",led_hue=(gfloat)%.5f"
                ",led_sat=(gfloat)%.5f"
                ",phase=(gfloat)%.5f" % (led_hue, led_sat, self._phase))
        # new_from_string is the clean 1.20+ API; from_string is the older
        # one and (in some bindings) returns a (struct, end) tuple.
        if hasattr(Gst.Structure, "new_from_string"):
            st = Gst.Structure.new_from_string(desc)
        else:
            st = Gst.Structure.from_string(desc)
            if isinstance(st, tuple):
                st = st[0]
        if st is not None:
            self.shader.set_property("uniforms", st)

    def render(self, name, width, height, token,
               led_hue=0.5, led_sat=0.9, spin=0.0):
        if name not in GPU_GENERATORS:
            raise ValueError(f"unknown GPU generator: {name}")
        self.ensure(name, width, height, token)
        # Resume if we were paused (blackout/freeze); a no-op if already
        # PLAYING.
        self.pipeline.set_state(Gst.State.PLAYING)
        if name.startswith("lantern"):
            self._set_lantern_uniforms(led_hue, led_sat, spin)
        sample = None
        for _ in range(4):
            sample = self.sink.emit("try-pull-sample", 1 * Gst.SECOND)
            if sample is not None:
                break
        if sample is None:
            bus = self.pipeline.get_bus()
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
            if msg is not None and msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                raise RuntimeError(f"{err.message}; {dbg}")
            if msg is not None and msg.type == Gst.MessageType.WARNING:
                err, dbg = msg.parse_warning()
                raise RuntimeError(f"{err.message}; {dbg}")
            raise RuntimeError("appsink returned no sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("failed to map GStreamer buffer")
        try:
            return bytes(info.data)
        finally:
            buf.unmap(info)


def _send(obj, payload=None):
    out = sys.stdout.buffer
    out.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    if payload:
        out.write(payload)
    out.flush()


def main():
    renderer = Renderer()
    try:
        for line in sys.stdin.buffer:
            try:
                req = json.loads(line.decode("utf-8"))
                if req.get("cmd") == "pause":
                    renderer.pause()
                    _send({"ok": True, "paused": True})
                    continue
                name = str(req["name"])
                width = int(req["width"])
                height = int(req["height"])
                token = int(req.get("token", 0))
                data = renderer.render(
                    name, width, height, token,
                    led_hue=float(req.get("led_hue", 0.5)),
                    led_sat=float(req.get("led_sat", 0.9)),
                    spin=float(req.get("spin", 0.0)),
                )
                _send({"ok": True, "width": width, "height": height, "n": len(data)}, data)
            except Exception as exc:
                print(f"[gpu-worker] {exc!r}", file=sys.stderr, flush=True)
                _send({"ok": False, "error": repr(exc)})
    finally:
        renderer.close()


if __name__ == "__main__":
    main()
