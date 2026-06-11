"""Out-of-process GStreamer/GL generator renderer."""
import json
import random
import sys
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

    def render(self, name, width, height, token, param_x=0.5, param_y=0.5):
        if name not in GPU_GENERATORS:
            raise ValueError(f"unknown GPU generator: {name}")
        self.ensure(name, width, height, token)
        # Resume if we were paused (blackout/freeze); a no-op if already
        # PLAYING.
        self.pipeline.set_state(Gst.State.PLAYING)
        # Push the live 0..1 params as custom GLSL uniforms. glshader keeps
        # its built-in time/width/height/tex regardless, and a shader that
        # doesn't declare param_x/param_y just ignores them (unknown uniform
        # locations are silent no-ops), so this is safe for every generator.
        try:
            st = Gst.Structure.new_from_string(
                "uniforms,param_x=(gfloat){:.6f},param_y=(gfloat){:.6f}".format(
                    float(param_x), float(param_y)))
            if st is not None:
                self.shader.set_property("uniforms", st)
        except Exception as exc:
            print(f"[gpu-worker] uniform push failed: {exc!r}",
                  file=sys.stderr, flush=True)
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
                param_x = float(req.get("param_x", 0.5))
                param_y = float(req.get("param_y", 0.5))
                data = renderer.render(name, width, height, token,
                                       param_x, param_y)
                _send({"ok": True, "width": width, "height": height, "n": len(data)}, data)
            except Exception as exc:
                print(f"[gpu-worker] {exc!r}", file=sys.stderr, flush=True)
                _send({"ok": False, "error": repr(exc)})
    finally:
        renderer.close()


if __name__ == "__main__":
    main()
