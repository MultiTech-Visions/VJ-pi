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
from collections import OrderedDict
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


# projectM fires this when a preset can't be loaded/compiled (the real
# signal — load_preset_file itself returns void). Signature from
# callbacks.h: void(const char* filename, const char* message, void* ud).
PRESET_FAIL_CB = ctypes.CFUNCTYPE(None, c_char_p, c_char_p, c_void_p)


# ── EGL: headless GLES context ─────────────────────────────────────────

EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_SURFACE_TYPE = 0x3033
EGL_NONE = 0x3038
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_ES3_BIT = 0x0040
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_OPENGL_ES_API = 0x30A0
EGL_PBUFFER_BIT = 0x0001
EGL_WIDTH = 0x3057
EGL_HEIGHT = 0x3056
EGL_RED_SIZE = 0x3024
EGL_GREEN_SIZE = 0x3023
EGL_BLUE_SIZE = 0x3022


class EglContext:
    """Offscreen GLES3 context backed by a PBUFFER surface.

    A pbuffer gives us a complete *default* framebuffer (FBO 0). This is
    essential: released libprojectM (<=v4.1.6 — there is no FBO-target API
    until master) renders its final pass to framebuffer 0. With a SURFACELESS
    context there is no FBO 0, so every render raised GL_INVALID_FRAMEBUFFER_
    OPERATION (0x0506) and produced a black frame — and the broken pipeline
    state appeared to wedge V3D after a few presets. Rendering into a real
    pbuffer fixes both. (When built from master, the worker instead targets
    its own FBO via projectm_opengl_render_frame_fbo; the pbuffer is then a
    harmless spare default framebuffer.)
    """

    def __init__(self, width, height):
        egl = ctypes.CDLL("libEGL.so.1", mode=ctypes.RTLD_GLOBAL)
        egl.eglGetPlatformDisplay.restype = c_void_p
        egl.eglGetPlatformDisplay.argtypes = [c_uint, c_void_p, c_void_p]
        egl.eglGetDisplay.restype = c_void_p
        egl.eglGetDisplay.argtypes = [c_void_p]
        egl.eglInitialize.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
        egl.eglBindAPI.argtypes = [c_uint]
        egl.eglChooseConfig.argtypes = [c_void_p, POINTER(c_int),
                                        POINTER(c_void_p), c_int, POINTER(c_int)]
        egl.eglCreatePbufferSurface.restype = c_void_p
        egl.eglCreatePbufferSurface.argtypes = [c_void_p, c_void_p, POINTER(c_int)]
        egl.eglDestroySurface.argtypes = [c_void_p, c_void_p]
        egl.eglCreateContext.restype = c_void_p
        egl.eglCreateContext.argtypes = [c_void_p, c_void_p, c_void_p,
                                         POINTER(c_int)]
        egl.eglMakeCurrent.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
        egl.eglGetError.restype = c_int
        self.egl = egl

        dpy = egl.eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, None, None)
        if not dpy:
            dpy = egl.eglGetDisplay(None)  # EGL_DEFAULT_DISPLAY
        if not dpy:
            raise RuntimeError("eglGetPlatformDisplay/eglGetDisplay failed")
        major, minor = c_int(), c_int()
        if not egl.eglInitialize(dpy, byref(major), byref(minor)):
            raise RuntimeError(f"eglInitialize failed (0x{egl.eglGetError():04x})")
        egl.eglBindAPI(EGL_OPENGL_ES_API)
        cfg_attribs = (c_int * 11)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
                                   EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                                   EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
                                   EGL_BLUE_SIZE, 8, EGL_NONE)
        cfg = c_void_p()
        n = c_int()
        if not egl.eglChooseConfig(dpy, cfg_attribs, byref(cfg), 1,
                                   byref(n)) or n.value < 1:
            raise RuntimeError("eglChooseConfig found no pbuffer GLES3 config")
        ctx = egl.eglCreateContext(dpy, cfg, None,
                                   (c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 3,
                                               EGL_NONE))
        if not ctx:
            raise RuntimeError(
                f"eglCreateContext failed (0x{egl.eglGetError():04x})")
        self.dpy, self.cfg, self.ctx = dpy, cfg, ctx
        self.surf = None
        self.size = (0, 0)
        self.resize(width, height)
        log(f"EGL {major.value}.{minor.value} pbuffer {width}x{height} "
            f"GLES context up")

    def resize(self, width, height):
        """(Re)create the pbuffer surface at width×height and make current.
        Cheap — touches only the surface, not the context or projectM."""
        if self.size == (width, height) and self.surf is not None:
            return
        egl = self.egl
        new = egl.eglCreatePbufferSurface(
            self.dpy, self.cfg,
            (c_int * 5)(EGL_WIDTH, int(width), EGL_HEIGHT, int(height), EGL_NONE))
        if not new:
            raise RuntimeError(
                f"eglCreatePbufferSurface failed (0x{egl.eglGetError():04x})")
        if not egl.eglMakeCurrent(self.dpy, new, new, self.ctx):
            raise RuntimeError(
                f"eglMakeCurrent (pbuffer) failed (0x{egl.eglGetError():04x})")
        if self.surf is not None:
            egl.eglDestroySurface(self.dpy, self.surf)
        self.surf = new
        self.size = (width, height)


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
        g.glGetError.restype = c_uint
        g.glGetError.argtypes = []
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
    _logged_gl = False   # GL/version banner is logged once across all instances

    def __init__(self):
        pm = _find_projectm()
        pm.projectm_create.restype = c_void_p
        pm.projectm_create.argtypes = []
        pm.projectm_destroy.argtypes = [c_void_p]
        # NOTE: projectm_load_preset_file returns VOID in libprojectM v4
        # (see core.h). It was bound as c_bool and its "result" tested —
        # that read a garbage register, so the old "preset failed to load"
        # log was meaningless. Real failures arrive via the switch-failed
        # callback registered below.
        pm.projectm_load_preset_file.restype = None
        pm.projectm_load_preset_file.argtypes = [c_void_p, c_char_p, c_bool]
        pm.projectm_set_preset_switch_failed_event_callback.argtypes = [
            c_void_p, c_void_p, c_void_p]
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
        # Capture real load/compile failures. The callback fires from the
        # render thread during load; we stash the last failure so render()
        # can report which preset actually broke (and why).
        self.last_fail = None  # (filename, message) of the most recent failure

        def _on_fail(filename, message, _ud):
            self.last_fail = (
                filename.decode("utf-8", "replace") if filename else "",
                message.decode("utf-8", "replace") if message else "")
        self._fail_cb = PRESET_FAIL_CB(_on_fail)  # keep ref alive
        pm.projectm_set_preset_switch_failed_event_callback(
            self.handle, self._fail_cb, None)
        pm.projectm_destroy.argtypes = [c_void_p]
        if not ProjectM._logged_gl:
            ProjectM._logged_gl = True
            try:
                g = ctypes.CDLL("libGLESv2.so.2")
                g.glGetString.restype = c_char_p
                ver = g.glGetString(0x1F02)  # GL_VERSION
                sl = g.glGetString(0x8B8C)   # GL_SHADING_LANGUAGE_VERSION
                log(f"GL: {ver.decode() if ver else '?'} / "
                    f"{sl.decode() if sl else '?'}")
            except Exception:
                pass
            log(f"projectM up (mesh {mw}x{mh}, "
                f"{'fbo render' if self.render_fbo else 'bound-framebuffer'})")

    def destroy(self):
        if self.handle and self.handle.value:
            self.pm.projectm_destroy(self.handle)
            self.handle = c_void_p(0)


# ── Audio: USB mic via GStreamer (audio-only, no GL elements) ──────────

class MicCapture(threading.Thread):
    """Pulls S16LE stereo PCM from the default capture device and stashes the
    latest chunk. The renderer feeds it to whichever projectM instance it is
    about to render (there can be several — one per on-screen preset), so the
    mic can't be bound to a single instance. alive() goes False if the pipeline
    can't start or stalls, flipping the renderer to the synthetic-beat fallback."""

    def __init__(self):
        super().__init__(daemon=True)
        self.last_sample = 0.0
        self.failed = False
        self._lock = threading.Lock()
        self._pcm = None            # (ctypes int16 array, frame_count)

    def alive(self):
        return not self.failed and (time.time() - self.last_sample) < 1.5

    def latest(self):
        with self._lock:
            return self._pcm

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
                        with self._lock:
                            self._pcm = (pcm, frames)
                        self.last_sample = time.time()
                finally:
                    buf.unmap(info)
        except Exception as exc:
            log(f"mic capture unavailable ({exc!r}); using synthetic beat")
            self.failed = True


# ── Renderer ───────────────────────────────────────────────────────────

class Renderer:
    """Holds a small LRU pool of projectM instances — one per active preset —
    all sharing the single EGL context. A mapping scene with several pm:* boxes
    renders each box's own instance, so shaders compile ONCE per preset instead
    of being reloaded+recompiled every box every frame (the old single-instance
    worker did the latter, dropping a multi-preset mapping scene to ~0.25fps)."""

    def __init__(self):
        # GL + instances are built lazily on the first render() so the pbuffer
        # can be sized to the real canvas.
        self.egl = None
        self.gl = None
        self.mic = None
        self.fbo = c_uint32(0)
        self.tex = c_uint32(0)
        self.size = None
        self._ready = False
        self._uses_fbo = False        # master build renders into a caller FBO
        self.instances = OrderedDict()   # name -> ProjectM (preset loaded), LRU
        self._synth_t = 0.0
        self._checked = set()         # presets sanity-logged
        self._frames_since = {}       # name -> frames rendered since created
        # Max warm instances. Must exceed the number of DISTINCT pm:* presets
        # on screen at once, or the pool thrashes (evict→recreate→recompile).
        # Only on-screen presets actually render each frame, so a generous pool
        # is cheap (warm shaders in memory) — render cost scales with boxes,
        # not pool size.
        try:
            self.max_instances = max(1, int(
                os.environ.get("VJ_PM_INSTANCES", "12")))
        except ValueError:
            self.max_instances = 12
        # Throttle NEW-instance creation (= a shader compile) to one per
        # interval, so a fast [/] browse or a big scene loading all at once
        # can't storm V3D. On-screen presets that already have a warm instance
        # are never throttled — they render every frame.
        try:
            self.create_interval = max(0.0, int(
                os.environ.get("VJ_PM_SWITCH_MS", "400")) / 1000.0)
        except ValueError:
            self.create_interval = 0.4
        self._last_create = 0.0

    def _ensure_ready(self, width, height):
        if self._ready:
            return
        self.egl = EglContext(width, height)
        self.gl = Gl().g
        self.mic = MicCapture()
        self.mic.start()
        self._ready = True

    def _apply_size(self, width, height):
        if self.size == (width, height):
            return
        self.egl.resize(width, height)
        if self._uses_fbo:
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
        for pm in self.instances.values():
            pm.pm.projectm_set_window_size(pm.handle, width, height)
        self.size = (width, height)

    def _make_instance(self, name, width, height):
        pm = ProjectM()
        self._uses_fbo = pm.render_fbo is not None
        pm.pm.projectm_set_window_size(pm.handle, width, height)
        pm.last_fail = None
        # smooth=False = hard cut. Each instance loads its preset exactly once.
        pm.pm.projectm_load_preset_file(
            pm.handle, PROJECTM_GENERATORS[name].encode(), False)
        if pm.last_fail is not None:
            fn, msg = pm.last_fail
            log(f"preset FAILED: {Path(fn).name} — {msg}")
        self.instances[name] = pm
        self.instances.move_to_end(name)
        self._frames_since[name] = 0
        while len(self.instances) > self.max_instances:
            old_name, old_pm = self.instances.popitem(last=False)
            self._frames_since.pop(old_name, None)
            self._checked.discard(old_name)
            try:
                old_pm.destroy()
            except Exception:
                pass
        return pm

    def _instance_for(self, name, width, height, now):
        pm = self.instances.get(name)
        if pm is not None:
            self.instances.move_to_end(name)
            return pm
        # Needs a fresh instance (a compile). Throttle creation so a burst
        # can't storm V3D; while deferring, render the most-recent existing
        # instance so the frame isn't black (it fills in within a frame or two).
        if self.instances and (now - self._last_create) < self.create_interval:
            return next(reversed(self.instances.values()))
        self._last_create = now
        return self._make_instance(name, width, height)

    def _feed_audio(self, pm):
        if self.mic is not None and self.mic.alive():
            chunk = self.mic.latest()
            if chunk is not None:
                pcm, frames = chunk
                pm.pm.projectm_pcm_add_int16(pm.handle, pcm, frames,
                                             PROJECTM_STEREO)
                return
        # ~120 BPM kick + hat-ish noise so presets pulse without a mic.
        frames = AUDIO_RATE // 30
        t = self._synth_t + np.arange(frames) / AUDIO_RATE
        self._synth_t = float(t[-1]) + 1.0 / AUDIO_RATE
        beat = (t * 2.0) % 1.0
        kick = np.sin(2 * np.pi * 55 * t) * np.exp(-beat * 9.0)
        hat = np.random.default_rng(int(self._synth_t * 1000)).standard_normal(
            frames) * 0.12 * np.exp(-((t * 4.0) % 1.0) * 14.0)
        mono = np.clip((kick * 0.8 + hat) * 0.7, -1.0, 1.0)
        stereo = np.empty(frames * 2, dtype=np.int16)
        stereo[0::2] = stereo[1::2] = (mono * 32767).astype(np.int16)
        pm.pm.projectm_pcm_add_int16(
            pm.handle, stereo.ctypes.data_as(POINTER(c_int16)), frames,
            PROJECTM_STEREO)

    def render(self, name, width, height, param_x=0.5):
        if name not in PROJECTM_GENERATORS:
            raise ValueError(f"unknown projectM preset: {name}")
        self._ensure_ready(width, height)
        now = time.time()
        pm = self._instance_for(name, width, height, now)
        self._apply_size(width, height)
        sens = 0.5 + float(param_x) * 1.5   # param_x knob → 0.5..2.0
        pm.pm.projectm_set_beat_sensitivity(pm.handle, sens)
        self._feed_audio(pm)
        g = self.gl
        # Released projectM (<=v4.1.6) renders to the default framebuffer
        # (FBO 0 = our pbuffer); a master build renders into our own FBO.
        target = self.fbo.value if self._uses_fbo else 0
        g.glBindFramebuffer(GL_FRAMEBUFFER, target)
        g.glViewport(0, 0, width, height)
        if self._uses_fbo:
            pm.render_fbo(pm.handle, target)
        else:
            pm.pm.projectm_opengl_render_frame(pm.handle)
        g.glBindFramebuffer(GL_FRAMEBUFFER, target)
        g.glPixelStorei(GL_PACK_ALIGNMENT, 1)
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        g.glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE,
                       rgba.ctypes.data_as(c_void_p))
        # Sanity-log each preset once it's had a few frames to settle.
        if name in self.instances and name not in self._checked:
            self._frames_since[name] = self._frames_since.get(name, 0) + 1
            if self._frames_since[name] >= 8:
                self._checked.add(name)
                err = g.glGetError()
                mean = float(rgba[:, :, :3].mean())
                tag = "BLACK" if mean < 1.0 else f"ok(mean={mean:.1f})"
                gltag = "" if err == 0 else f" glGetError=0x{err:04x}"
                log(f"render {name}: {tag}{gltag}")
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
