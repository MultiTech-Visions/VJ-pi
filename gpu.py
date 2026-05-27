"""GPU offload for individual generators.

Designed to coexist with — not replace — the CPU pipeline. A standalone
EGL context (no pygame window dependency) runs one fragment shader per
ported generator, reads the result back to RAM, and hands it to the
existing compose path. Anything that doesn't port stays on the CPU.

The previous all-or-nothing GPU branch failed because (a) every shader
shared FBO ping-pong state so one bug killed the whole pipeline, and
(b) the GL context was bound to pygame's OPENGL window, which collided
with the SDL2 Renderer the HUD uses. This module sidesteps both: a
standalone EGL context that doesn't know SDL exists, and one
independent program / FBO per generator — `tunnel` failing doesn't
touch `plasma`.

V3D-safe defaults baked in (from the previous attempt's post-mortem):
  * Standalone EGL context — no clash with the SDL2 Renderer windows.
  * RGBA8 FBO only. GLES 3.0 only spec-requires RGBA8 as
    color-renderable; V3D silently produces zeros for RGB8 FBOs.
  * RGBA readback only. V3D returns zeros for glReadPixels(GL_RGB).
  * Explicit layout(location) on every vertex attribute. V3D's GLES
    attribute introspection has returned wrong locations in the past;
    explicit layouts make compile-time binding authoritative.
  * GLES 3.0 shader profile (#version 300 es + precision qualifier).
  * ctx.finish() before readback to drain the V3D tile pipeline.

Public API:
  gpu.render(name, w, h, t, px, py) -> np.ndarray | None
"""
import numpy as np


# Lazy-init sentinel:
#   None  = not tried yet
#   False = tried and failed, never retry
#   else  = a working _GpuBackend instance
_BACKEND = None


# Fullscreen quad vertex shader. UV maps in NDC -1..+1 → 0..1 with no
# Y-flip: bottom-up glReadPixels combined with this UV puts FBO row 0
# (the bottom row) at the same place the CPU pipeline puts image row 0
# (the top row of cv2/numpy frames). Net result: the readback array
# comes out top-down already, with no per-frame numpy stride flip.
VS_FULLSCREEN = """#version 300 es
precision highp float;
layout(location = 0) in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


HSV2RGB = """
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
"""


# Port of effects.tunnel — checkerboard-on-a-tunnel pattern with hue
# cycling. CPU version uses cv2.HSV→RGB with H in 0..180; this uses
# normalized 0..1 HSV via the standard hsv2rgb helper. Visually
# identical at any reasonable sampling rate.
FS_TUNNEL = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
uniform vec2 u_res;
""" + HSV2RGB + """
const float PI = 3.14159265358979323846;
void main() {
    vec2 pix = v_uv * u_res - u_res * 0.5;
    float r = length(pix) + 1.0;
    float a = atan(pix.y, pix.x);
    float u = fract(200.0 / r + u_t * 2.0);
    float v_ = (a / PI + 1.0) * 0.5;
    float chk = float((int(u * 8.0) + int(v_ * 16.0)) & 1);
    float hue = fract(v_ + u_t / 6.0);
    frag = vec4(hsv2rgb(vec3(hue, 1.0, chk)), 1.0);
}
"""


# name → fragment shader. One entry per ported generator. Adding the
# next one is: append here, wrap the CPU function in effects.py to try
# GPU first.
_FRAGMENT_SHADERS = {
    "tunnel": FS_TUNNEL,
}


class _GpuBackend:
    """One standalone EGL context + one program/VAO/FBO per generator.

    FBOs are sized lazily and rebuilt on size change — the engine
    renders generatives at gen_render_scale × canvas, so the size is
    stable for a session but not known at backend init.
    """

    def __init__(self, ctx, moderngl_module):
        self.ctx = ctx
        self._mgl = moderngl_module
        # Fullscreen quad as a triangle strip — 4 vertices, one draw call.
        quad = np.array([
            -1.0, -1.0,
             1.0, -1.0,
            -1.0,  1.0,
             1.0,  1.0,
        ], dtype=np.float32)
        self._quad_vbo = ctx.buffer(quad.tobytes())
        # name → (program, vao, fbo, (w, h))
        self._passes = {}

    def _ensure_pass(self, name, w, h):
        cached = self._passes.get(name)
        if cached is not None and cached[3] == (w, h):
            return cached
        if cached is None:
            prog = self.ctx.program(
                vertex_shader=VS_FULLSCREEN,
                fragment_shader=_FRAGMENT_SHADERS[name],
            )
            vao = self.ctx.vertex_array(
                prog, [(self._quad_vbo, "2f", "in_pos")]
            )
        else:
            prog, vao, old_fbo, _ = cached
            # FBO size changed (operator switched render resolution).
            # Release the old color attachment + framebuffer so we
            # don't leak GPU memory across resizes.
            for tex in old_fbo.color_attachments:
                tex.release()
            old_fbo.release()
        # RGBA8, not RGB8 — V3D silently renders zeros into RGB8 FBOs.
        tex = self.ctx.texture((w, h), 4, dtype="f1")
        fbo = self.ctx.framebuffer(color_attachments=[tex])
        entry = (prog, vao, fbo, (w, h))
        self._passes[name] = entry
        return entry

    def render(self, name, w, h, uniforms):
        prog, vao, fbo, _ = self._ensure_pass(name, w, h)
        # Quietly skip uniforms the shader doesn't actually declare —
        # callers pass the same dict for every generator.
        for k, v in uniforms.items():
            if k in prog:
                prog[k].value = v
        fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        vao.render(self._mgl.TRIANGLE_STRIP, vertices=4)
        # Drain the V3D tile pipeline before glReadPixels — the previous
        # branch's `readback returns zeros` debugging spiral started
        # with skipping this.
        self.ctx.finish()
        # RGBA readback only — see module docstring. Slice alpha off
        # CPU-side, then copy into a contiguous RGB array (the raw
        # framebuffer bytes get reused on the next read).
        raw = fbo.read(components=4, alignment=1)
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(arr[:, :, :3])


def _init():
    """Lazy init. Returns the backend, or None if GPU offload is
    unavailable on this system. Poisons the cache on failure so we
    don't retry every frame."""
    global _BACKEND
    if _BACKEND is False:
        return None
    if _BACKEND is not None:
        return _BACKEND
    # Operator kill switch: set VJ_NO_GPU=1 in the launcher (or the
    # environment) to skip moderngl entirely. Useful for isolating
    # the GPU path from any unrelated issue, or for reverting to the
    # known-good CPU build without uninstalling moderngl.
    import os
    if os.environ.get("VJ_NO_GPU"):
        print("[vj.gpu] VJ_NO_GPU set; generators stay on CPU")
        _BACKEND = False
        return None
    try:
        import moderngl
    except ImportError as exc:
        print(f"[vj.gpu] moderngl not installed ({exc}); "
              f"generators stay on CPU")
        _BACKEND = False
        return None
    try:
        # Standalone EGL context. Completely independent from SDL2's
        # GL state — no conflict with the output or HUD renderers.
        # require=300 = OpenGL ES 3.0 / desktop GL 3.0 (V3D's profile).
        ctx = moderngl.create_standalone_context(backend="egl", require=300)
    except Exception as exc:
        print(f"[vj.gpu] standalone EGL context unavailable ({exc!r}); "
              f"generators stay on CPU")
        _BACKEND = False
        return None
    backend = _GpuBackend(ctx, moderngl)
    # Smoke-test every registered shader at a tiny size — if V3D
    # rejects a precision qualifier or attribute layout we want to
    # learn now, not on the first frame.
    try:
        for name in _FRAGMENT_SHADERS:
            backend._ensure_pass(name, 64, 36)
    except Exception as exc:
        print(f"[vj.gpu] shader compile failed ({exc!r}); "
              f"generators stay on CPU")
        _BACKEND = False
        return None
    info = ctx.info
    print(f"[vj.gpu] {info.get('GL_RENDERER', '?')} | "
          f"{info.get('GL_VERSION', '?')} | "
          f"offloaded: {', '.join(sorted(_FRAGMENT_SHADERS))}")
    _BACKEND = backend
    return backend


def is_available():
    """True if the GPU backend initialized successfully."""
    return _init() is not None


def render(name, w, h, t, px, py):
    """Render a registered generator on the GPU.

    Returns an RGB uint8 (h, w, 3) numpy array, or None if the GPU path
    isn't usable on this system (caller should fall back to CPU). A
    per-frame render error (transient) returns None for that frame
    without poisoning the backend.
    """
    backend = _init()
    if backend is None or name not in _FRAGMENT_SHADERS:
        return None
    try:
        return backend.render(name, w, h, {
            "u_t": float(t),
            "u_res": (float(w), float(h)),
            "u_px": float(px),
            "u_py": float(py),
        })
    except Exception as exc:
        print(f"[vj.gpu] render({name}) failed: {exc!r}; "
              f"CPU fallback this frame")
        return None
