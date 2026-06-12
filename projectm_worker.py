"""Out-of-process projectM (MilkDrop) renderer.

Speaks the same protocol as gpu_generator_worker.py: one JSON request per
line on stdin, a JSON header + raw RGB bytes on stdout. The bridge routes
every "pm:*" generator to ONE shared instance of this worker, because a
single projectM instance switches presets in place with a smooth blend
(spawning per-preset would rebuild GL + projectM on every [/] step).

GL isolation: this process owns exactly ONE EGL/GLES context (created
surfaceless, no window), keeping the V3D one-context-per-process rule.
projectM renders into an FBO we own; frames are glReadPixels'd back and
piped to the main process like any other generator.

Audio: a GStreamer audio-only capture thread (no GL elements) feeds mic
PCM to projectM so presets beat-react. If no mic is available the worker
falls back to a synthetic ~120 BPM signal so visuals still move.
Override the capture source with VJ_PM_AUDIO_SRC (a gst-launch source
description, default "autoaudiosrc").

Needs libprojectM v4 (built by "Setup ProjectM.sh" into vendor/projectm/).
Errors land on stderr, which Start VJ.sh tees into vj_last_run.log.
"""
import ctypes
import json
import math
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_float, c_int, \
    c_int16, c_int32, c_size_t, c_uint, c_uint32, c_void_p
from pathlib import Path

import numpy as np

from projectm_presets import PROJECTM_GENERATORS, PRESET_DIR, TEXTURE_DIR

HERE = Path(__file__).resolve().parent

AUDIO_RATE = 44100
PROJECTM_STEREO = 2


def log(msg):
    print(f"[pm-worker] {msg}", file=sys.stderr, flush=True)


# ── EGL: headless GLES context ─────────────────────────────────────────

EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_SURFACE_TYPE = 0x3033
EGL_NONE = 0x3038
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_ES3_BIT = 0x0040
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_OPENGL_ES_API = 0x30A0


def make_egl_context():
    egl = ctypes.CDLL("libEGL.so.1", mode=ctypes.RTLD_GLOBAL)
    egl.eglGetPlatformDisplay.restype = c_void_p
    egl.eglGetPlatformDisplay.argtypes = [c_uint, c_void_p, c_void_p]
    egl.eglGetDisplay.restype = c_void_p
    egl.eglGetDisplay.argtypes = [c_void_p]
    egl.eglInitialize.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
    egl.eglBindAPI.argtypes = [c_uint]
    egl.eglChooseConfig.argtypes = [c_void_p, POINTER(c_int), POINTER(c_void_p),
                                    c_int, POINTER(c_int)]
    egl.eglCreateContext.restype = c_void_p
    egl.eglCreateContext.argtypes = [c_void_p, c_void_p, c_void_p, POINTER(c_int)]
    egl.eglMakeCurrent.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
    egl.eglGetError.restype = c_int

    dpy = egl.eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, None, None)
    if not dpy:
        dpy = egl.eglGetDisplay(None)  # EGL_DEFAULT_DISPLAY
    if not dpy:
        raise RuntimeError("eglGetPlatformDisplay/eglGetDisplay failed")
    major, minor = c_int(), c_int()
    if not egl.eglInitialize(dpy, byref(major), byref(minor)):
        raise RuntimeError(f"eglInitialize failed (0x{egl.eglGetError():04x})")
    egl.eglBindAPI(EGL_OPENGL_ES_API)
    # EGL_SURFACE_TYPE 0: we render only to an FBO, never to an EGL
    # surface, so don't let the default WINDOW_BIT filter configs out.
    cfg_attribs = (c_int * 5)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
                              EGL_SURFACE_TYPE, 0, EGL_NONE)
    cfg = c_void_p()
    n = c_int()
    if not egl.eglChooseConfig(dpy, cfg_attribs, byref(cfg), 1, byref(n)) or n.value < 1:
        raise RuntimeError("eglChooseConfig found no GLES3 config")
    ctx_attribs = (c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE)
    ctx = egl.eglCreateContext(dpy, cfg, None, ctx_attribs)
    if not ctx:
        raise RuntimeError(f"eglCreateContext failed (0x{egl.eglGetError():04x})")
    if not egl.eglMakeCurrent(dpy, None, None, ctx):
        raise RuntimeError(
            f"eglMakeCurrent (surfaceless) failed (0x{egl.eglGetError():04x})")
    log(f"EGL {major.value}.{minor.value} surfaceless GLES context up")
    return egl  # keep a ref so the lib (and context) stays alive


# ── Minimal GLES bindings (FBO + readback only) ────────────────────────

GL_TEXTURE_2D = 0x0DE1
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR = 0x2601
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_PACK_ALIGNMENT = 0x0D05


class Gl:
    def __init__(self):
        g = ctypes.CDLL("libGLESv2.so.2", mode=ctypes.RTLD_GLOBAL)
        g.glGenTextures.argtypes = [c_int, POINTER(c_uint32)]
        g.glBindTexture.argtypes = [c_uint, c_uint32]
        g.glTexImage2D.argtypes = [c_uint, c_int, c_int, c_int, c_int,
                                   c_int, c_uint, c_uint, c_void_p]
        g.glTexParameteri.argtypes = [c_uint, c_uint, c_int]
        g.glGenFramebuffers.argtypes = [c_int, POINTER(c_uint32)]
        g.glBindFramebuffer.argtypes = [c_uint, c_uint32]
        g.glFramebufferTexture2D.argtypes = [c_uint, c_uint, c_uint, c_uint32, c_int]
        g.glCheckFramebufferStatus.restype = c_uint
        g.glCheckFramebufferStatus.argtypes = [c_uint]
        g.glDeleteTextures.argtypes = [c_int, POINTER(c_uint32)]
        g.glDeleteFramebuffers.argtypes = [c_int, POINTER(c_uint32)]
        g.glViewport.argtypes = [c_int, c_int, c_int, c_int]
        g.glPixelStorei.argtypes = [c_uint, c_int]
        g.glReadPixels.argtypes = [c_int, c_int, c_int, c_int, c_uint, c_uint, c_void_p]
        g.glFinish.argtypes = []
        self.g = g


# ── libprojectM v4 bindings ────────────────────────────────────────────

def _find_projectm():
    cands = []
    vendor = HERE / "vendor" / "projectm"
    if vendor.exists():
        cands += sorted(p for p in vendor.rglob("libprojectM-4.so*")
                        if not p.is_symlink()) or sorted(vendor.rglob("libprojectM-4.so*"))
    cands += ["libprojectM-4.so.4", "libprojectM-4.so", "libprojectM.so.4"]
    last = None
    for cand in cands:
        try:
            return ctypes.CDLL(str(cand), mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            last = exc
    raise RuntimeError(
        f"libprojectM v4 not found (run 'Setup ProjectM.sh'); last error: {last}")


class ProjectM:
    def __init__(self):
        pm = _find_projectm()
        pm.projectm_create.restype = c_void_p
        pm.projectm_create.argtypes = []
        pm.projectm_destroy.argtypes = [c_void_p]
        pm.projectm_load_preset_file.restype = c_bool
        pm.projectm_load_preset_file.argtypes = [c_void_p, c_char_p, c_bool]
        pm.projectm_set_window_size.argtypes = [c_void_p, c_size_t, c_size_t]
        pm.projectm_set_mesh_size.argtypes = [c_void_p, c_size_t, c_size_t]
        pm.projectm_set_fps.argtypes = [c_void_p, c_int32]
        pm.projectm_set_preset_locked.argtypes = [c_void_p, c_bool]
        pm.projectm_set_hard_cut_enabled.argtypes = [c_void_p, c_bool]
        pm.projectm_set_aspect_correction.argtypes = [c_void_p, c_bool]
        pm.projectm_set_beat_sensitivity.argtypes = [c_void_p, c_float]
        pm.projectm_set_texture_search_paths.argtypes = [c_void_p,
                                                         POINTER(c_char_p), c_size_t]
        pm.projectm_opengl_render_frame.argtypes = [c_void_p]
        pm.projectm_pcm_add_int16.argtypes = [c_void_p, POINTER(c_int16),
                                              c_uint, c_int]
        # v4.2+ renders straight into a caller FBO; older v4 renders into
        # whatever framebuffer is bound, which we arrange ourselves.
        self.render_fbo = getattr(pm, "projectm_opengl_render_frame_fbo", None)
        if self.render_fbo is not None:
            self.render_fbo.argtypes = [c_void_p, c_uint32]
        self.pm = pm
        self.handle = c_void_p(pm.projectm_create())
        if not self.handle.value:
            raise RuntimeError("projectm_create returned NULL "
                               "(GLES context insufficient?)")
        mesh = os.environ.get("VJ_PM_MESH", "48x32")
        try:
            mw, mh = (int(v) for v in mesh.lower().split("x"))
        except ValueError:
            mw, mh = 48, 32
        pm.projectm_set_mesh_size(self.handle, mw, mh)
        pm.projectm_set_fps(self.handle, 30)
        pm.projectm_set_preset_locked(self.handle, True)
        pm.projectm_set_hard_cut_enabled(self.handle, False)
        pm.projectm_set_aspect_correction(self.handle, True)
        paths = [p for p in (TEXTURE_DIR, PRESET_DIR) if p.exists()]
        if paths:
            arr = (c_char_p * len(paths))(*[str(p).encode() for p in paths])
            pm.projectm_set_texture_search_paths(self.handle, arr, len(paths))
        log(f"projectM up (mesh {mw}x{mh}, "
            f"{'fbo render' if self.render_fbo else 'bound-framebuffer render'})")


# ── Audio: USB mic via GStreamer (audio-only, no GL elements) ──────────

class MicCapture(threading.Thread):
    """Pulls S16LE stereo PCM from the default capture device and feeds
    projectM. alive() goes False if the pipeline can't start or stalls,
    flipping the renderer to the synthetic-beat fallback."""

    def __init__(self, projectm):
        super().__init__(daemon=True)
        self.projectm = projectm
        self.last_sample = 0.0
        self.failed = False

    def alive(self):
        return not self.failed and (time.time() - self.last_sample) < 1.5

    def run(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            src = os.environ.get("VJ_PM_AUDIO_SRC", "autoaudiosrc")
            desc = (f"{src} ! queue ! audioconvert ! audioresample ! "
                    f"audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels=2 ! "
                    "appsink name=sink emit-signals=false max-buffers=4 "
                    "drop=true sync=false")
            pipeline = Gst.parse_launch(desc)
            sink = pipeline.get_by_name("sink")
            pipeline.set_state(Gst.State.PLAYING)
            log(f"mic capture starting ({src})")
            misses = 0
            while True:
                sample = sink.emit("try-pull-sample", Gst.SECOND // 2)
                if sample is None:
                    misses += 1
                    if misses > 20:  # ~10s of silence from the pipeline
                        raise RuntimeError("mic pipeline stopped producing")
                    msg = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                    if msg is not None:
                        err, dbg = msg.parse_error()
                        raise RuntimeError(f"{err.message}; {dbg}")
                    continue
                misses = 0
                buf = sample.get_buffer()
                ok, info = buf.map(Gst.MapFlags.READ)
                if not ok:
                    continue
                try:
                    frames = len(info.data) // 4  # 2 ch × int16
                    if frames:
                        pcm = (c_int16 * (frames * 2)).from_buffer_copy(info.data)
                        self.projectm.pm.projectm_pcm_add_int16(
                            self.projectm.handle, pcm, frames, PROJECTM_STEREO)
                        self.last_sample = time.time()
                finally:
                    buf.unmap(info)
        except Exception as exc:
            log(f"mic capture unavailable ({exc!r}); using synthetic beat")
            self.failed = True


# ── Renderer ───────────────────────────────────────────────────────────

class Renderer:
    def __init__(self):
        self._egl = make_egl_context()
        self.gl = Gl().g
        self.projectm = ProjectM()
        self.mic = MicCapture(self.projectm)
        self.mic.start()
        self.fbo = c_uint32(0)
        self.tex = c_uint32(0)
        self.size = None
        self.current = None
        self.beat_sens = None
        self._synth_t = 0.0

    def _ensure_fbo(self, width, height):
        if self.size == (width, height):
            return
        g = self.gl
        if self.tex.value:
            g.glDeleteFramebuffers(1, byref(self.fbo))
            g.glDeleteTextures(1, byref(self.tex))
        g.glGenTextures(1, byref(self.tex))
        g.glBindTexture(GL_TEXTURE_2D, self.tex.value)
        g.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0,
                       GL_RGBA, GL_UNSIGNED_BYTE, None)
        g.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        g.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        g.glGenFramebuffers(1, byref(self.fbo))
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo.value)
        g.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                 GL_TEXTURE_2D, self.tex.value, 0)
        status = g.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError(f"FBO incomplete: 0x{status:04x}")
        pm = self.projectm
        pm.pm.projectm_set_window_size(pm.handle, width, height)
        self.size = (width, height)

    def _feed_synthetic_audio(self):
        # ~120 BPM kick + hat-ish noise so presets pulse without a mic.
        frames = AUDIO_RATE // 30
        t = self._synth_t + np.arange(frames) / AUDIO_RATE
        self._synth_t = float(t[-1]) + 1.0 / AUDIO_RATE
        beat = (t * 2.0) % 1.0                       # 120 BPM phase
        kick = np.sin(2 * np.pi * 55 * t) * np.exp(-beat * 9.0)
        hat = np.random.default_rng(int(self._synth_t * 1000)).standard_normal(
            frames) * 0.12 * np.exp(-((t * 4.0) % 1.0) * 14.0)
        mono = np.clip((kick * 0.8 + hat) * 0.7, -1.0, 1.0)
        stereo = np.empty(frames * 2, dtype=np.int16)
        stereo[0::2] = stereo[1::2] = (mono * 32767).astype(np.int16)
        pcm = stereo.ctypes.data_as(POINTER(c_int16))
        self.projectm.pm.projectm_pcm_add_int16(
            self.projectm.handle, pcm, frames, PROJECTM_STEREO)

    def render(self, name, width, height, param_x=0.5):
        path = PROJECTM_GENERATORS.get(name)
        if path is None:
            raise ValueError(f"unknown projectM preset: {name}")
        self._ensure_fbo(width, height)
        pm = self.projectm
        if self.current != name:
            # smooth=True crossfades from the running preset; on a failed
            # load keep whatever is playing (projectM's idle preset at worst)
            # rather than blacking out mid-set.
            if not pm.pm.projectm_load_preset_file(pm.handle, path.encode(), True):
                log(f"preset failed to load: {path}")
            self.current = name
        sens = 0.5 + float(param_x) * 1.5   # param_x knob → 0.5..2.0
        if self.beat_sens is None or abs(sens - self.beat_sens) > 0.01:
            pm.pm.projectm_set_beat_sensitivity(pm.handle, sens)
            self.beat_sens = sens
        if not self.mic.alive():
            self._feed_synthetic_audio()
        g = self.gl
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo.value)
        g.glViewport(0, 0, width, height)
        if pm.render_fbo is not None:
            pm.render_fbo(pm.handle, self.fbo.value)
        else:
            pm.pm.projectm_opengl_render_frame(pm.handle)
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo.value)
        g.glPixelStorei(GL_PACK_ALIGNMENT, 1)
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        g.glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE,
                       rgba.ctypes.data_as(c_void_p))
        # GL reads bottom-up; the pipe protocol carries top-down RGB.
        return np.ascontiguousarray(rgba[::-1, :, :3]).tobytes()


# ── stdin/stdout protocol loop (mirrors gpu_generator_worker.py) ───────

def _send(obj, payload=None):
    out = sys.stdout.buffer
    out.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    if payload:
        out.write(payload)
    out.flush()


def main():
    renderer = None
    for line in sys.stdin.buffer:
        try:
            req = json.loads(line.decode("utf-8"))
            if req.get("cmd") == "pause":
                # Nothing renders unless asked, so pause is just an ack.
                _send({"ok": True, "paused": True})
                continue
            if renderer is None:
                renderer = Renderer()
            name = str(req["name"])
            width = int(req["width"])
            height = int(req["height"])
            param_x = float(req.get("param_x", 0.5))
            data = renderer.render(name, width, height, param_x)
            _send({"ok": True, "width": width, "height": height,
                   "n": len(data)}, data)
        except Exception as exc:
            log(f"{exc!r}")
            _send({"ok": False, "error": repr(exc)})


if __name__ == "__main__":
    main()
