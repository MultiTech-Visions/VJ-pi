"""GPU render backend for the VJ engine.

Wraps a moderngl context around pygame's OpenGL window and owns every
shader, framebuffer, and vertex buffer the render pipeline needs. The
public surface used by `engine.py` is the `Renderer` class — call
`begin_frame()`, push base/FX/overlay/hits through it, finish with
`present(...)`, and optionally `readback()` for the HUD preview.

Pi 5 design notes:
  * The VideoCore VII GPU does OpenGL 3.3 on the open V3D Mesa driver,
    so the shaders target `#version 140` (no GL ES profile gymnastics).
  * Generatives are pure fragment shaders rasterized over a single
    full-screen triangle pair — zero CPU per-pixel work.
  * The FX chain is a ping-pong between two same-size FBO textures so
    we can stack arbitrarily many effects without re-allocating.
  * Clip frames come off OpenCV as BGR uint8; we upload them straight
    to a texture and swizzle BGR→RGB inside the sampling shader. The
    old `cv2.cvtColor` per frame is gone.
  * `cv2.warpPerspective` in mapping mode is replaced by computing the
    inverse 3x3 homography per quad and sampling the source texture
    from a single screen-aligned quad shader pass — one draw call per
    space, no full-frame allocations.
  * Readback to numpy only happens when the HUD is open or we're in
    mapping/edit mode (those need a CPU frame for the preview / for
    cv2-drawn edit overlays). In live performance with HUD closed the
    pipeline is fully GPU-resident.
"""
import math
import time

import numpy as np
import moderngl
import pygame


# ── Vertex shaders ───────────────────────────────────────────────────

# Full-screen triangle-strip quad. The vertex shader passes UV in [0,1].
VS_FULLSCREEN = """
#version 140
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# Same as above but flips Y so a texture uploaded top-row-first reads
# right-side-up. Used by the present pass.
VS_FULLSCREEN_FLIP = """
#version 140
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = vec2(in_pos.x * 0.5 + 0.5, 1.0 - (in_pos.y * 0.5 + 0.5));
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# Point sprite vertex shader for star/warp primitives. Per-vertex
# size + brightness packed alongside the position.
VS_POINTS = """
#version 140
in vec2 in_pos;
in float in_size;
in float in_bright;
out float v_bright;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    gl_PointSize = max(1.0, in_size);
    v_bright = in_bright;
}
"""

# Line vertex shader with per-vertex brightness (used by warp streaks).
VS_LINES = """
#version 140
in vec2 in_pos;
in float in_bright;
out float v_bright;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    v_bright = in_bright;
}
"""

# Solid-color line vertex shader (lissajous + edit overlays).
VS_LINE_SOLID = """
#version 140
in vec2 in_pos;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


# ── Fragment shader fragments shared across programs ─────────────────

HSV2RGB = """
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
"""

PI_DEFS = """
const float PI = 3.14159265358979323846;
"""


# ── Generative fragment shaders ──────────────────────────────────────

FS_PLASMA = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
""" + HSV2RGB + """
void main() {
    vec2 p = v_uv * 8.0;
    float t = u_t;
    float v = (sin(p.x + t) + sin(p.y + t*1.3)
             + sin((p.x + p.y) * 0.5 + t * 0.7)
             + sin(sqrt(p.x*p.x + p.y*p.y) + t * 1.7)) * 0.25;
    v = (v + 1.0) * 0.5;
    float hue = fract(v + t / 9.0);
    frag = vec4(hsv2rgb(vec3(hue, 1.0, 1.0)), 1.0);
}
"""

FS_TUNNEL = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
uniform vec2 u_res;
""" + HSV2RGB + PI_DEFS + """
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

FS_WAVES = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
uniform vec2 u_res;
uniform float u_px;
""" + HSV2RGB + PI_DEFS + """
void main() {
    vec2 pix = v_uv * u_res;
    vec2 c1 = vec2(u_res.x * 0.3 + sin(u_t * 0.5) * u_res.x * 0.15,
                   u_res.y * 0.5 + cos(u_t * 0.4) * u_res.y * 0.2);
    vec2 c2 = vec2(u_res.x * 0.7 + cos(u_t * 0.6) * u_res.x * 0.15,
                   u_res.y * 0.5 + sin(u_t * 0.45) * u_res.y * 0.2);
    float period = 22.0 + u_px * 60.0;
    float r1 = length(pix - c1) / period;
    float r2 = length(pix - c2) / period;
    float v = (sin(r1 * PI * 2.0 - u_t * 2.0)
             + sin(r2 * PI * 2.0 + u_t * 1.5)) * 0.25 + 0.5;
    float hue = fract(v + u_t / 10.0);
    frag = vec4(hsv2rgb(vec3(hue, 220.0/255.0, v)), 1.0);
}
"""

FS_CELLS = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
uniform vec2 u_res;
uniform float u_px;
""" + HSV2RGB + PI_DEFS + """
void main() {
    vec2 pix = v_uv * u_res;
    float scale = 0.018 + u_px * 0.04;
    float u = pix.x * scale + sin(pix.y * scale * 0.6 + u_t) * 0.4;
    float v = pix.y * scale + cos(pix.x * scale * 0.6 + u_t * 1.1) * 0.4;
    float pat = abs(sin(u * PI) * sin(v * PI));
    pat = pow(pat, 0.6);
    float hue = fract((u * 28.0 + u_t * 14.0) / 180.0);
    frag = vec4(hsv2rgb(vec3(hue, 210.0/255.0, pat)), 1.0);
}
"""

FS_MOIRE = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
uniform vec2 u_res;
uniform float u_px;
""" + HSV2RGB + """
void main() {
    vec2 pix = v_uv * u_res;
    vec2 c = u_res * 0.5;
    float off_x = u_res.x * 0.10;
    float off_y = u_res.y * 0.10;
    vec2 c1 = c + vec2(sin(u_t * 0.5) * off_x, cos(u_t * 0.4) * off_y);
    vec2 c2 = c - vec2(sin(u_t * 0.5) * off_x, cos(u_t * 0.4) * off_y);
    float spacing = 5.0 + u_px * 18.0;
    float r1 = length(pix - c1) / spacing;
    float r2 = length(pix - c2) / spacing;
    float pat = (sin(r1 + u_t * 2.0) + sin(r2 - u_t * 1.5)) * 0.25 + 0.5;
    float hue = fract((pat * 180.0 + u_t * 22.0) / 180.0);
    frag = vec4(hsv2rgb(vec3(hue, 210.0/255.0, pat)), 1.0);
}
"""

FS_METABALLS = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
uniform vec2 u_res;
uniform float u_py;
""" + HSV2RGB + """
const int BALLS = 6;
void main() {
    vec2 pix = v_uv * u_res;
    float field = 0.0;
    float influence = (u_res.x * 26.0) * (0.5 + u_py * 1.8);
    for (int i = 0; i < BALLS; i++) {
        float phase = float(i) * 6.2831853 / float(BALLS);
        vec2 b = u_res * 0.5
               + vec2(cos(u_t * 0.5 + phase * 1.3) * u_res.x * 0.35,
                      sin(u_t * 0.7 + phase * 1.7) * u_res.y * 0.35);
        vec2 d = pix - b;
        float r2 = dot(d, d) + 1.0;
        field += influence / r2;
    }
    float intensity = clamp(field / 2.5, 0.0, 1.0);
    float hue = fract((intensity * 80.0 + u_t * 20.0) / 180.0);
    frag = vec4(hsv2rgb(vec3(hue, 230.0/255.0, intensity)), 1.0);
}
"""

# Fragment shader for point-sprite stars + warp lines — both colour to
# the per-vertex brightness, with stars masked to a disc.
FS_POINTS_DISC = """
#version 140
in float v_bright;
out vec4 frag;
void main() {
    vec2 d = gl_PointCoord - 0.5;
    if (dot(d, d) > 0.25) discard;
    frag = vec4(vec3(v_bright), 1.0);
}
"""

FS_LINE_BRIGHT = """
#version 140
in float v_bright;
out vec4 frag;
void main() {
    frag = vec4(vec3(v_bright), 1.0);
}
"""

FS_LINE_SOLID = """
#version 140
out vec4 frag;
uniform vec3 u_color;
void main() {
    frag = vec4(u_color, 1.0);
}
"""


# ── FX fragment shaders ──────────────────────────────────────────────

# Common header for FX shaders that sample a source texture.
FS_FX_HEADER = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_src;
"""

FS_COPY = FS_FX_HEADER + """
void main() {
    frag = texture(u_src, v_uv);
}
"""

# Sampling helper: clips come up as BGR uint8 textures (we skip
# cvtColor on the CPU). u_swizzle_bgr=1 means swap RB. Used by both
# the base-clip blit and the overlay blit.
FS_BLIT_SWIZZLE = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_src;
uniform int u_swizzle_bgr;
void main() {
    vec4 c = texture(u_src, vec2(v_uv.x, 1.0 - v_uv.y));
    frag = (u_swizzle_bgr == 1) ? vec4(c.bgr, c.a) : c;
}
"""

FS_KALEIDOSCOPE = FS_FX_HEADER + PI_DEFS + """
uniform float u_segments;
void main() {
    vec2 p = v_uv - 0.5;
    float r = length(p);
    float a = atan(p.y, p.x);
    float seg = 2.0 * PI / max(1.0, u_segments);
    a = abs(mod(a, seg) - seg * 0.5);
    vec2 uv = vec2(0.5 + r * cos(a), 0.5 + r * sin(a));
    // Mirror at edges to mimic cv2.BORDER_REFLECT behaviour.
    uv = abs(uv);
    uv = 1.0 - abs(1.0 - mod(uv, 2.0));
    frag = texture(u_src, uv);
}
"""

FS_MIRROR_H = FS_FX_HEADER + """
void main() {
    float x = v_uv.x < 0.5 ? v_uv.x : 1.0 - v_uv.x;
    frag = texture(u_src, vec2(x * 2.0 * 0.5, v_uv.y));
}
"""
# (mirror: left half mirrored to right half — matches mirror_h() which
# took src[:, :w/2] then concatenated with the same flipped.)

FS_RGB_SPLIT = FS_FX_HEADER + """
uniform vec2 u_offset;  // (offset_in_uv_x, 0)
void main() {
    float r = texture(u_src, v_uv + vec2( u_offset.x, 0.0)).r;
    float g = texture(u_src, v_uv).g;
    float b = texture(u_src, v_uv + vec2(-u_offset.x, 0.0)).b;
    frag = vec4(r, g, b, 1.0);
}
"""

FS_POSTERIZE = FS_FX_HEADER + """
uniform float u_levels;
void main() {
    vec4 c = texture(u_src, v_uv);
    float step = floor(256.0 / max(1.0, u_levels));
    vec3 rgb = floor(c.rgb * 255.0 / step) * step / 255.0;
    frag = vec4(rgb, 1.0);
}
"""

FS_INVERT = FS_FX_HEADER + """
void main() {
    vec4 c = texture(u_src, v_uv);
    frag = vec4(1.0 - c.rgb, 1.0);
}
"""

# Sobel edge detect → grayscale output, matches cv2.Canny visually
# (single-channel edge map blown back out to RGB).
FS_EDGES = FS_FX_HEADER + """
uniform vec2 u_texel;
float lum(vec3 c) { return dot(c, vec3(0.2989, 0.5870, 0.1140)); }
void main() {
    float tl = lum(texture(u_src, v_uv + vec2(-u_texel.x, -u_texel.y)).rgb);
    float  t = lum(texture(u_src, v_uv + vec2(        0.0, -u_texel.y)).rgb);
    float tr = lum(texture(u_src, v_uv + vec2( u_texel.x, -u_texel.y)).rgb);
    float  l = lum(texture(u_src, v_uv + vec2(-u_texel.x,        0.0)).rgb);
    float  r = lum(texture(u_src, v_uv + vec2( u_texel.x,        0.0)).rgb);
    float bl = lum(texture(u_src, v_uv + vec2(-u_texel.x,  u_texel.y)).rgb);
    float  b = lum(texture(u_src, v_uv + vec2(        0.0,  u_texel.y)).rgb);
    float br = lum(texture(u_src, v_uv + vec2( u_texel.x,  u_texel.y)).rgb);
    float gx = -tl - 2.0*l - bl + tr + 2.0*r + br;
    float gy = -tl - 2.0*t - tr + bl + 2.0*b + br;
    float e = clamp(sqrt(gx * gx + gy * gy), 0.0, 1.0);
    // Threshold to a binary-ish edge map so the look matches Canny.
    float v = e > 0.3 ? 1.0 : 0.0;
    frag = vec4(vec3(v), 1.0);
}
"""

FS_FEEDBACK = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_src;   // current frame
uniform sampler2D u_prev;  // previous composited frame
uniform float u_zoom;
uniform float u_rotate;    // radians
uniform float u_fade;
void main() {
    vec2 c = vec2(0.5);
    vec2 p = v_uv - c;
    float cs = cos(u_rotate);
    float sn = sin(u_rotate);
    p = mat2(cs, -sn, sn, cs) * p / u_zoom;
    vec2 prev_uv = p + c;
    vec3 prev_col = vec3(0.0);
    if (prev_uv.x >= 0.0 && prev_uv.x <= 1.0
        && prev_uv.y >= 0.0 && prev_uv.y <= 1.0) {
        prev_col = texture(u_prev, prev_uv).rgb * u_fade;
    }
    vec3 cur = texture(u_src, v_uv).rgb;
    frag = vec4(max(cur, prev_col), 1.0);
}
"""

# Screen blend: 1 - (1-a)(1-b). Overlay clip is uploaded as BGR.
FS_SCREEN_BLEND = """
#version 140
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_base;
uniform sampler2D u_overlay;
uniform int u_overlay_bgr;
void main() {
    vec4 a = texture(u_base, v_uv);
    vec4 b = texture(u_overlay, vec2(v_uv.x, 1.0 - v_uv.y));
    vec3 ov = (u_overlay_bgr == 1) ? b.bgr : b.rgb;
    frag = vec4(1.0 - (1.0 - a.rgb) * (1.0 - ov), 1.0);
}
"""

# Zoom-punch hit: sample source at a UV scaled around the centre.
FS_ZOOM_PUNCH = FS_FX_HEADER + """
uniform float u_scale;
void main() {
    vec2 p = (v_uv - 0.5) / u_scale + 0.5;
    frag = texture(u_src, p);
}
"""

# Solid colour shader for the strobe/black-flash hits.
FS_SOLID = """
#version 140
out vec4 frag;
uniform vec3 u_color;
void main() {
    frag = vec4(u_color, 1.0);
}
"""


# ── Mapping warp shader ──────────────────────────────────────────────

# Vertex shader for a destination quad (the projected space) drawn in
# clip-space. The fragment computes UV by inverse-homography from
# screen pixel coords. Vertices are passed in NDC already.
VS_QUAD_NDC = """
#version 140
in vec2 in_pos;        // NDC, the dest quad corners (4 verts as TRIANGLE_FAN)
out vec2 v_pix;        // pixel coord in output FBO
uniform vec2 u_res;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    v_pix = vec2((in_pos.x * 0.5 + 0.5) * u_res.x,
                 (1.0 - (in_pos.y * 0.5 + 0.5)) * u_res.y);
}
"""

# Inverse-homography fragment: maps dest pixel → source UV, samples the
# group's source texture. Discards fragments outside [0,1]² so the
# fragment outside the quad's projection of the source rectangle goes
# transparent (no colour spill outside the actual quad).
#
# Note the `1.0 - uv.y` flip when sampling — the source FBO has its
# canvas-top content at high GL-t (we rendered into it with NDC y=+1 at
# the visual top), but our homography was set up with src_uv (0,0) =
# canvas-top-left (cv2 convention). Without the flip the warped output
# would render upside-down.
FS_MAPPING_WARP = """
#version 140
in vec2 v_pix;
out vec4 frag;
uniform sampler2D u_src;
uniform mat3 u_inv_h;
void main() {
    vec3 h = u_inv_h * vec3(v_pix, 1.0);
    vec2 uv = h.xy / h.z;
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) discard;
    frag = vec4(texture(u_src, vec2(uv.x, 1.0 - uv.y)).rgb, 1.0);
}
"""

# Windows-into-video fragment: each space-quad is rasterised in NDC
# while the source texture is treated as a single rectangle laid down
# across the canvas at (u_dst_xy, u_dst_size). Each fragment computes
# its sample UV from canvas-pixel position so spaces in the same
# group reveal different parts of ONE underlying video plane — the
# multi-window mapping behaviour. Same Y-flip as FS_MAPPING_WARP.
FS_MAPPING_WINDOW = """
#version 140
in vec2 v_pix;
out vec4 frag;
uniform sampler2D u_src;
uniform vec2 u_dst_xy;     // top-left of the video rect on canvas, pixels
uniform vec2 u_dst_size;   // width/height of the video rect, pixels
void main() {
    vec2 uv = (v_pix - u_dst_xy) / u_dst_size;
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) discard;
    frag = vec4(texture(u_src, vec2(uv.x, 1.0 - uv.y)).rgb, 1.0);
}
"""


# ──────────────────────────────────────────────────────────────────────
# moderngl plumbing
# ──────────────────────────────────────────────────────────────────────


class Renderer:
    """Owns the moderngl context, every shader program, FBOs, and the
    full-screen quad buffer. Drives one render pipeline.

    Lifecycle:
        r = Renderer(render_w, render_h)
        each frame:
            r.begin_frame()
            r.draw_<base>(...)
            r.apply_fx_chain(fx_flags, params)
            r.apply_overlay_texture(tex)
            r.apply_hit(...)
            cpu_frame = r.readback()  # optional
            r.present(display_size)
    """

    def __init__(self, render_w, render_h):
        self.w = render_w
        self.h = render_h
        # moderngl.create_context() grabs whatever GL context pygame just
        # made current — must be called AFTER pygame.display.set_mode(OPENGL).
        # Pi 5's V3D Mesa driver reports OpenGL 3.1 by default (GLSL 1.40);
        # we pass require=310 so moderngl accepts it. The shaders are
        # written to `#version 140`, which is GLSL 1.40 and is the highest
        # version supported by a 3.1 context — every feature we use
        # (in/out qualifiers, texture(), mat3, bitwise &, gl_PointSize
        # with GL_PROGRAM_POINT_SIZE) exists in 1.40 so this works on
        # both 3.1 and 3.3+ drivers.
        self.ctx = moderngl.create_context(require=310)
        try:
            self.ctx.enable(moderngl.PROGRAM_POINT_SIZE)
        except (KeyError, moderngl.Error):
            pass

        # Fullscreen quad: 2 triangles as a strip → 4 verts in NDC.
        quad_verts = np.array([
            -1.0, -1.0,
             1.0, -1.0,
            -1.0,  1.0,
             1.0,  1.0,
        ], dtype=np.float32)
        self._quad_vbo = self.ctx.buffer(quad_verts.tobytes())

        # Compile all programs.
        self._programs = {}
        self._vaos = {}
        self._compile_all()

        # Two ping-pong FBOs at render resolution for the FX chain.
        self._fbo_a = self._make_fbo(self.w, self.h)
        self._fbo_b = self._make_fbo(self.w, self.h)
        # The "current" FBO holds the latest composited frame. After every
        # pass we swap (read from current, write to other, then make other
        # the new current).
        self._current = self._fbo_a
        self._other = self._fbo_b

        # Feedback trail buffer: keeps the previous frame around so the
        # feedback FX can sample it. Updated only when feedback is active.
        self._prev_fbo = self._make_fbo(self.w, self.h)

        # Persistent textures for clip / overlay frames. (h, w) for cv2
        # numpy; moderngl wants (w, h).
        self._clip_tex = self.ctx.texture((self.w, self.h), 3, dtype="f1")
        self._clip_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._clip_tex.repeat_x = False
        self._clip_tex.repeat_y = False
        self._overlay_tex = self.ctx.texture((self.w, self.h), 3, dtype="f1")
        self._overlay_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._overlay_tex.repeat_x = False
        self._overlay_tex.repeat_y = False

        # CPU-side frame uploaded by the "freeze frame" feature; refreshed
        # on toggle, drawn into the pipeline as the base each frame.
        self._frozen_tex = self.ctx.texture((self.w, self.h), 3, dtype="f1")
        self._frozen_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Primitive VBOs grown as needed for starfield / warp / lissajous /
        # edit overlays. Allocated once and re-uploaded each frame at
        # whatever size the data needs.
        self._dyn_buffers = {}
        self._dyn_vaos = {}

        # Readback target: a contiguous numpy buffer reused each frame so
        # we don't churn allocations.
        self._readback = np.empty((self.h, self.w, 3), dtype=np.uint8)

        # Hooked up by the engine once it knows the actual screen size.
        self._screen_size = (self.w, self.h)

        print(f"[vj.gpu] moderngl ctx: {self.ctx.info.get('GL_RENDERER', '?')} | "
              f"{self.ctx.info.get('GL_VERSION', '?')}")

    # ── Compile ──────────────────────────────────────────────────────

    def _prog(self, name, vs, fs):
        p = self.ctx.program(vertex_shader=vs, fragment_shader=fs)
        self._programs[name] = p
        return p

    def _compile_all(self):
        # Generative + simple-quad programs share the fullscreen VS.
        for name, fs in [
            ("plasma", FS_PLASMA), ("tunnel", FS_TUNNEL),
            ("waves", FS_WAVES), ("cells", FS_CELLS),
            ("moire", FS_MOIRE), ("metaballs", FS_METABALLS),
            ("copy", FS_COPY),
            ("kaleido", FS_KALEIDOSCOPE),
            ("mirror_h", FS_MIRROR_H),
            ("rgb_split", FS_RGB_SPLIT),
            ("posterize", FS_POSTERIZE),
            ("invert", FS_INVERT),
            ("edges", FS_EDGES),
            ("feedback", FS_FEEDBACK),
            ("screen_blend", FS_SCREEN_BLEND),
            ("zoom_punch", FS_ZOOM_PUNCH),
            ("solid", FS_SOLID),
            ("blit_swizzle", FS_BLIT_SWIZZLE),
        ]:
            self._prog(name, VS_FULLSCREEN, fs)
        # Present pass uses the standard fullscreen VS (no Y flip): our
        # ping-pong FBOs are rendered with NDC y=+1 at canvas-top, so
        # sampling at v_uv.y=1 returns canvas-top — that lines up with
        # screen-top under the standard fullscreen VS.
        self._prog("present", VS_FULLSCREEN, FS_COPY)

        # Mapping warp uses a per-quad NDC VS + the inverse-homography FS.
        self._prog("warp", VS_QUAD_NDC, FS_MAPPING_WARP)
        # Windows mode: per-space quad rasterises with canvas-pixel UV.
        self._prog("warp_window", VS_QUAD_NDC, FS_MAPPING_WINDOW)

        # Primitive shaders.
        self._prog("points_disc", VS_POINTS, FS_POINTS_DISC)
        self._prog("lines_bright", VS_LINES, FS_LINE_BRIGHT)
        self._prog("line_solid", VS_LINE_SOLID, FS_LINE_SOLID)

        # Build the static fullscreen VAO once per program that uses it.
        for name in ("plasma", "tunnel", "waves", "cells", "moire",
                     "metaballs", "copy", "kaleido", "mirror_h", "rgb_split",
                     "posterize", "invert", "edges", "feedback",
                     "screen_blend", "zoom_punch", "blit_swizzle", "present"):
            self._vaos[name] = self.ctx.vertex_array(
                self._programs[name],
                [(self._quad_vbo, "2f", "in_pos")],
            )
        # Solid uses a fullscreen quad too but doesn't reference in_pos
        # directly via the FS; binding is still by in_pos in the VS, so
        # same VAO shape.
        self._vaos["solid"] = self.ctx.vertex_array(
            self._programs["solid"],
            [(self._quad_vbo, "2f", "in_pos")],
        )

    def _make_fbo(self, w, h):
        tex = self.ctx.texture((w, h), 3, dtype="f1")
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = False
        tex.repeat_y = False
        fbo = self.ctx.framebuffer(color_attachments=[tex])
        # Attach the texture by reference so we can sample it elsewhere.
        return fbo

    # ── FBO ping-pong helpers ────────────────────────────────────────

    def _swap_after_pass(self):
        self._current, self._other = self._other, self._current

    def _bind_dest(self, fbo):
        fbo.use()
        self.ctx.viewport = (0, 0, self.w, self.h)

    # ── Begin / present ──────────────────────────────────────────────

    def set_screen_size(self, size):
        self._screen_size = (max(1, int(size[0])), max(1, int(size[1])))

    def begin_frame(self):
        """Clear the current FBO to black. Every base layer pass writes
        a full-frame quad to `_current`, so a clear here makes sure any
        unwritten pixels stay black."""
        self._bind_dest(self._current)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)

    def current_texture(self):
        return self._current.color_attachments[0]

    # ── Texture upload (clips / overlays / frozen) ───────────────────

    def _upload_into(self, tex, frame_bgr):
        """Resize-if-needed + write into a texture. frame_bgr is HxWx3
        uint8 (BGR or RGB — sampled with optional swizzle)."""
        if frame_bgr is None:
            return False
        if frame_bgr.shape[0] != self.h or frame_bgr.shape[1] != self.w:
            # Caller is expected to have resized; defensive fallback.
            import cv2
            frame_bgr = cv2.resize(frame_bgr, (self.w, self.h))
        if not frame_bgr.flags["C_CONTIGUOUS"]:
            frame_bgr = np.ascontiguousarray(frame_bgr)
        tex.write(frame_bgr.tobytes())
        return True

    def upload_clip(self, frame_bgr):
        return self._upload_into(self._clip_tex, frame_bgr)

    def upload_overlay(self, frame_bgr):
        return self._upload_into(self._overlay_tex, frame_bgr)

    def upload_frozen(self, frame_rgb):
        return self._upload_into(self._frozen_tex, frame_rgb)

    # ── Base layer draws ─────────────────────────────────────────────

    def _draw_quad(self, name):
        self._vaos[name].render(mode=moderngl.TRIANGLE_STRIP)

    def draw_clip_base(self):
        """Blit the most-recently-uploaded clip into the current FBO,
        swizzling BGR→RGB on the fly."""
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._clip_tex.use(location=0)
        p = self._programs["blit_swizzle"]
        p["u_src"].value = 0
        p["u_swizzle_bgr"].value = 1
        self._draw_quad("blit_swizzle")
        self._swap_after_pass()

    def draw_frozen_base(self):
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._frozen_tex.use(location=0)
        p = self._programs["blit_swizzle"]
        p["u_src"].value = 0
        # Frozen frames come from a CPU readback which was already RGB
        # (we read from the output FBO) — so no swizzle.
        p["u_swizzle_bgr"].value = 0
        self._draw_quad("blit_swizzle")
        self._swap_after_pass()

    def draw_solid_base(self, rgb=(0.0, 0.0, 0.0)):
        self._bind_dest(self._other)
        self.ctx.clear(rgb[0], rgb[1], rgb[2], 1.0)
        self._swap_after_pass()

    _GEN_NAMES = {"plasma", "tunnel", "waves", "cells", "moire", "metaballs"}

    def draw_generative(self, name, t, params):
        """Render a generative by shader name. Primitive generatives
        (starfield, warp, lissajous) come in via draw_primitive_*."""
        if name in self._GEN_NAMES:
            self._bind_dest(self._other)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            p = self._programs[name]
            p["u_t"].value = float(t)
            if "u_res" in p:
                p["u_res"].value = (float(self.w), float(self.h))
            if "u_px" in p:
                p["u_px"].value = float(params[0])
            if "u_py" in p:
                p["u_py"].value = float(params[1])
            self._draw_quad(name)
            self._swap_after_pass()
        elif name == "starfield":
            self._draw_starfield(t, params)
        elif name == "warp":
            self._draw_warp(t, params)
        elif name == "lissajous":
            self._draw_lissajous(t, params)
        else:
            # Unknown — leave the current FBO as-is (black from begin_frame).
            pass

    # ── Primitive generatives ────────────────────────────────────────

    def _ensure_dyn(self, key, layout, prog_name):
        """Reuse a dynamic buffer + VAO by key. Returns (buffer, vao)."""
        if key not in self._dyn_buffers:
            buf = self.ctx.buffer(reserve=4)
            self._dyn_buffers[key] = buf
            self._dyn_vaos[key] = self.ctx.vertex_array(
                self._programs[prog_name], [(buf, *layout)]
            )
        return self._dyn_buffers[key], self._dyn_vaos[key]

    def _draw_starfield(self, t, params, density=220):
        rng = np.random.default_rng(42)
        angles = rng.uniform(0.0, 2.0 * np.pi, size=density).astype(np.float32)
        seeds = rng.uniform(0.0, 1.0, size=density).astype(np.float32)
        base_speed = rng.uniform(0.12, 0.40, size=density).astype(np.float32)
        speed = base_speed * (0.4 + float(params[1]) * 2.0)
        depth = (seeds + t * speed) % 1.0
        cx, cy = self.w * 0.5, self.h * 0.5
        max_r = float(np.sqrt(cx * cx + cy * cy)) * 1.3
        r = depth * max_r
        xs_px = cx + np.cos(angles) * r
        ys_px = cy + np.sin(angles) * r
        # To NDC: x_ndc = 2*x/w - 1; y_ndc = 1 - 2*y/h (Y up)
        xs_ndc = 2.0 * xs_px / self.w - 1.0
        ys_ndc = 1.0 - 2.0 * ys_px / self.h
        sizes = np.maximum(1.5, depth * 6.0 + 1.0).astype(np.float32)
        bright = np.clip(depth * 1.6, 0.0, 1.0).astype(np.float32)
        verts = np.column_stack([xs_ndc.astype(np.float32),
                                 ys_ndc.astype(np.float32),
                                 sizes, bright]).reshape(-1)
        self._render_points(verts, count=density)

    def _draw_warp(self, t, params, count=120):
        rng = np.random.default_rng(11)
        angles = rng.uniform(0.0, 2.0 * np.pi, size=count).astype(np.float32)
        phases = rng.uniform(0.0, 1.0, size=count).astype(np.float32)
        speed = 0.4 + float(params[0]) * 2.0
        depth = (phases + t * speed) % 1.0
        cx, cy = self.w * 0.5, self.h * 0.5
        max_r = float(np.sqrt(cx * cx + cy * cy)) * 1.2
        r_end = depth * max_r
        r_start = np.maximum(r_end - max_r * 0.18, 0.0)
        cos_a, sin_a = np.cos(angles), np.sin(angles)
        x1_px = cx + cos_a * r_start
        y1_px = cy + sin_a * r_start
        x2_px = cx + cos_a * r_end
        y2_px = cy + sin_a * r_end
        x1n = 2.0 * x1_px / self.w - 1.0
        y1n = 1.0 - 2.0 * y1_px / self.h
        x2n = 2.0 * x2_px / self.w - 1.0
        y2n = 1.0 - 2.0 * y2_px / self.h
        bright = np.clip(depth, 0.0, 1.0).astype(np.float32)
        # Interleave: 2 verts per line (start, end), same brightness on both.
        verts = np.empty((count * 2, 3), dtype=np.float32)
        verts[0::2, 0] = x1n
        verts[0::2, 1] = y1n
        verts[0::2, 2] = bright
        verts[1::2, 0] = x2n
        verts[1::2, 1] = y2n
        verts[1::2, 2] = bright
        self._render_lines_bright(verts.reshape(-1), count=count * 2)

    def _draw_lissajous(self, t, params, n=1400):
        rx, ry = 0.84, 0.84  # ratio of half-screen (matches w*0.42 / h*0.42)
        a = 2 + int(float(params[0]) * 7)
        b = 3 + int(float(params[1]) * 7)
        phi = np.linspace(0.0, 2.0 * np.pi, n).astype(np.float32)
        xs = np.sin(phi * a + t * 0.7) * rx
        ys = np.sin(phi * b + t * 1.1) * ry
        verts = np.column_stack([xs, -ys]).astype(np.float32).reshape(-1)
        # Hue from time, value=1 sat=220/255 → convert in-Python so the
        # solid-colour shader can take a single uniform.
        hue = (t * 24.0 % 180.0) / 180.0
        rgb = _hsv_to_rgb_unit(hue, 220.0 / 255.0, 1.0)
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        buf, vao = self._ensure_dyn("lissajous", ("2f", "in_pos"), "line_solid")
        if buf.size < verts.nbytes:
            buf.orphan(verts.nbytes)
        buf.write(verts.tobytes())
        self._programs["line_solid"]["u_color"].value = rgb
        # GL_LINE_STRIP isn't anti-aliased in core profile, so the line
        # will look 1-px crisp; the original cv2 version applied a 5x5
        # gaussian blur for a CRT glow. We can approximate by drawing the
        # strip twice with slight offsets, but the cleaner path on GPU is
        # to leave it crisp — Pi 5's projector hides 1-px aliasing well.
        vao.render(mode=moderngl.LINE_STRIP, vertices=n)
        self._swap_after_pass()

    def _render_points(self, interleaved, count):
        """Stars: x_ndc, y_ndc, size, bright (4 floats per point)."""
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        buf, vao = self._ensure_dyn(
            "points", ("2f 1f 1f", "in_pos", "in_size", "in_bright"),
            "points_disc",
        )
        data = np.ascontiguousarray(interleaved, dtype=np.float32)
        if buf.size < data.nbytes:
            buf.orphan(data.nbytes)
        buf.write(data.tobytes())
        vao.render(mode=moderngl.POINTS, vertices=count)
        self._swap_after_pass()

    def _render_lines_bright(self, interleaved, count):
        """Warp streaks: x_ndc, y_ndc, bright (3 floats per vert)."""
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        buf, vao = self._ensure_dyn(
            "lines_bright", ("2f 1f", "in_pos", "in_bright"),
            "lines_bright",
        )
        data = np.ascontiguousarray(interleaved, dtype=np.float32)
        if buf.size < data.nbytes:
            buf.orphan(data.nbytes)
        buf.write(data.tobytes())
        vao.render(mode=moderngl.LINES, vertices=count)
        self._swap_after_pass()

    # ── FX chain ─────────────────────────────────────────────────────

    def fx_kaleidoscope(self, segments):
        self._fx_pass("kaleido", lambda p: p.__setitem__("u_segments", float(segments)))

    def fx_mirror_h(self):
        self._fx_pass("mirror_h")

    def fx_rgb_split(self, offset_px):
        self._fx_pass(
            "rgb_split",
            lambda p: p.__setitem__("u_offset", (offset_px / self.w, 0.0)),
        )

    def fx_posterize(self, levels):
        self._fx_pass("posterize", lambda p: p.__setitem__("u_levels", float(levels)))

    def fx_invert(self):
        self._fx_pass("invert")

    def fx_edges(self):
        self._fx_pass(
            "edges",
            lambda p: p.__setitem__("u_texel", (1.0 / self.w, 1.0 / self.h)),
        )

    def fx_feedback(self, zoom, rotate_deg):
        """Sample the cached _prev_fbo, fade it, max-blend with current.
        Updates _prev_fbo from the new current frame for the next call."""
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.current_texture().use(location=0)
        self._prev_fbo.color_attachments[0].use(location=1)
        p = self._programs["feedback"]
        p["u_src"].value = 0
        p["u_prev"].value = 1
        p["u_zoom"].value = float(zoom)
        p["u_rotate"].value = math.radians(float(rotate_deg))
        p["u_fade"].value = 0.92
        self._draw_quad("feedback")
        self._swap_after_pass()

    def update_feedback_trail(self):
        """Copy the latest composited frame into the trail FBO. Called
        once at the end of every frame that had feedback active, so the
        next frame's feedback FX has something to sample."""
        self._bind_dest(self._prev_fbo)
        self.ctx.viewport = (0, 0, self.w, self.h)
        self.current_texture().use(location=0)
        self._programs["copy"]["u_src"].value = 0
        self._draw_quad("copy")

    def _fx_pass(self, name, setup=None):
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.current_texture().use(location=0)
        p = self._programs[name]
        p["u_src"].value = 0
        if setup is not None:
            setup(p)
        self._draw_quad(name)
        self._swap_after_pass()

    # ── Overlay ──────────────────────────────────────────────────────

    def apply_overlay_screen_blend(self):
        """Composite the most-recent overlay upload over the current FBO
        with screen-blend math. No-op if upload_overlay() wasn't called
        this frame — callers gate that themselves."""
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.current_texture().use(location=0)
        self._overlay_tex.use(location=1)
        p = self._programs["screen_blend"]
        p["u_base"].value = 0
        p["u_overlay"].value = 1
        p["u_overlay_bgr"].value = 1
        self._draw_quad("screen_blend")
        self._swap_after_pass()

    # ── Hits ─────────────────────────────────────────────────────────

    def hit_strobe(self):
        self._bind_dest(self._other)
        self.ctx.clear(1.0, 1.0, 1.0, 1.0)
        self._swap_after_pass()

    def hit_black(self):
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._swap_after_pass()

    def hit_invert(self):
        self.fx_invert()

    def hit_rgb_smash(self):
        self.fx_rgb_split(28)

    def hit_zoom_punch(self, scale):
        self._bind_dest(self._other)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.current_texture().use(location=0)
        p = self._programs["zoom_punch"]
        p["u_src"].value = 0
        p["u_scale"].value = float(scale)
        self._draw_quad("zoom_punch")
        self._swap_after_pass()

    # ── Mapping warp ─────────────────────────────────────────────────

    def warp_source_into_quad(self, source_tex, dst_corners_norm):
        """Render `source_tex` warped into the projector-space quad.

        `dst_corners_norm`: (4, 2) array in [0,1] coords of the output
        frame, corner order TL→TR→BR→BL. We compute the inverse
        homography from dest pixels → source UV and rasterize ONLY the
        dest quad in NDC, so unaffected pixels of the canvas keep
        their existing colour.

        Caller must have `begin_frame()`d earlier; this draws into the
        same `_current` FBO additively.
        """
        # Compute inverse homography (3x3) from dest pixel coords to src UV.
        sw, sh = self.w, self.h
        dst_px = np.array(
            [[dst_corners_norm[i, 0] * sw, dst_corners_norm[i, 1] * sh]
             for i in range(4)], dtype=np.float64,
        )
        src_uv = np.array(
            [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], dtype=np.float64,
        )
        H = _homography(dst_px, src_uv)
        if H is None:
            return  # Degenerate quad

        # Dest quad in NDC for rasterization. corners are top-left origin
        # in pixel space; NDC y goes up.
        ndc = np.empty((4, 2), dtype=np.float32)
        ndc[:, 0] = 2.0 * dst_px[:, 0] / sw - 1.0
        ndc[:, 1] = 1.0 - 2.0 * dst_px[:, 1] / sh
        # TRIANGLE_FAN with verts in TL, TR, BR, BL order covers the quad.

        # Bind dest FBO without clearing — we're additively painting quads.
        self._current.use()
        self.ctx.viewport = (0, 0, sw, sh)
        source_tex.use(location=0)
        buf, vao = self._ensure_dyn(
            "warp_quad", ("2f", "in_pos"), "warp",
        )
        if buf.size < ndc.nbytes:
            buf.orphan(ndc.nbytes)
        buf.write(ndc.tobytes())
        p = self._programs["warp"]
        p["u_src"].value = 0
        p["u_res"].value = (float(sw), float(sh))
        p["u_inv_h"].write(H.astype("f4").T.tobytes())  # column-major
        vao.render(mode=moderngl.TRIANGLE_FAN, vertices=4)

    def warp_source_into_window(self, source_tex, dst_corners_norm,
                                dst_xy, dst_size):
        """Stamp `source_tex` onto the canvas as a single rectangle at
        (dst_xy, dst_size), but only paint inside the quad defined by
        `dst_corners_norm` (a 4×2 in [0,1] canvas coords).

        Used by mapping-mode fit_mode in {window, fit, fill}: every
        space in a group calls this with the same dst_xy/dst_size, so
        each space is a window onto one underlying video plane. Spaces
        that overlap the same canvas pixel sample the same source UV,
        so multi-window groups stay visually continuous.

        Caller is expected to have already called `begin_frame()` and
        composed prior groups; this paints additively into _current.
        """
        sw, sh = self.w, self.h
        # Bounding NDC quad — we rasterise only the dst quad, the FS
        # discards fragments outside the video rect on top of that.
        ndc = np.empty((4, 2), dtype=np.float32)
        ndc[:, 0] = 2.0 * dst_corners_norm[:, 0] - 1.0
        ndc[:, 1] = 1.0 - 2.0 * dst_corners_norm[:, 1]

        self._current.use()
        self.ctx.viewport = (0, 0, sw, sh)
        source_tex.use(location=0)
        buf, vao = self._ensure_dyn(
            "warp_window_quad", ("2f", "in_pos"), "warp_window",
        )
        if buf.size < ndc.nbytes:
            buf.orphan(ndc.nbytes)
        buf.write(ndc.tobytes())
        p = self._programs["warp_window"]
        p["u_src"].value = 0
        p["u_res"].value = (float(sw), float(sh))
        p["u_dst_xy"].value = (float(dst_xy[0]), float(dst_xy[1]))
        p["u_dst_size"].value = (float(dst_size[0]), float(dst_size[1]))
        vao.render(mode=moderngl.TRIANGLE_FAN, vertices=4)

    # ── Aux: render a generative / clip / FX into an ARBITRARY FBO ──

    def make_group_fbo(self):
        """Allocate a fresh same-size FBO for a mapping group's source
        composition. Caller is responsible for releasing (or just let GC
        collect when it goes out of scope)."""
        return self._make_fbo(self.w, self.h)

    def begin_into_aux(self, prev_source_fbo=None):
        """Redirect the ping-pong target to a transient FBO pair used by
        mapping-mode group composition. `prev_source_fbo`, when given,
        becomes the previous-frame buffer the feedback shader samples
        for THIS group — caller is responsible for keeping it stable
        across frames (one FBO per group). When omitted, a shared
        zero-init buffer is used and feedback has nothing useful to
        sample on the first frame of a new group."""
        self._saved_pair = (self._current, self._other, self._prev_fbo)
        # Use a pair of internal aux FBOs for ping-pong. Allocated once
        # and reused across groups (composition is sequential, never
        # overlapping, so two aux buffers are sufficient even for many
        # groups).
        if not hasattr(self, "_aux_a") or self._aux_a is None:
            self._aux_a = self._make_fbo(self.w, self.h)
        if not hasattr(self, "_aux_b") or self._aux_b is None:
            self._aux_b = self._make_fbo(self.w, self.h)
        self._current = self._aux_a
        self._other = self._aux_b
        if prev_source_fbo is not None:
            self._prev_fbo = prev_source_fbo
        else:
            if not hasattr(self, "_aux_prev_default") or self._aux_prev_default is None:
                self._aux_prev_default = self._make_fbo(self.w, self.h)
            self._prev_fbo = self._aux_prev_default
        self._bind_dest(self._current)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)

    def finish_aux(self):
        """Restore the main ping-pong pair after group composition."""
        self._current, self._other, self._prev_fbo = self._saved_pair
        self._saved_pair = None

    def copy_current_to(self, target_fbo):
        """Blit the current FBO's texture into `target_fbo`. Used to
        persist a per-group source into its dedicated FBO so subsequent
        warp passes can sample from it, and to seed the next-frame
        feedback trail for groups that have feedback enabled."""
        source_tex = self.current_texture()
        target_fbo.use()
        self.ctx.viewport = (0, 0, self.w, self.h)
        source_tex.use(location=0)
        self._programs["copy"]["u_src"].value = 0
        self._draw_quad("copy")
        # Restore the current FBO binding so subsequent passes write to
        # the right place (target_fbo was bound for this one pass only).
        self._bind_dest(self._current)

    # ── Readback ─────────────────────────────────────────────────────

    def readback(self):
        """Read the current composited frame back to a numpy uint8 RGB
        array. Reuses one buffer to avoid per-frame allocations.
        Returns a view; callers MUST copy if they want to retain it
        past the next readback() call."""
        data = self._current.read(components=3, alignment=1, dtype="f1")
        # moderngl read returns bottom-up; flip to top-down to match
        # the cv2-style frames the rest of the codebase expects.
        buf = np.frombuffer(data, dtype=np.uint8).reshape(self.h, self.w, 3)
        np.copyto(self._readback, buf[::-1])
        return self._readback

    # ── Present ──────────────────────────────────────────────────────

    def present(self):
        """Draw the current FBO's texture to the default framebuffer,
        sampling it to the screen size with GPU bilinear. Caller is
        responsible for pygame.display.flip()."""
        self.ctx.screen.use()
        sw, sh = self._screen_size
        self.ctx.viewport = (0, 0, sw, sh)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.current_texture().use(location=0)
        self._programs["present"]["u_src"].value = 0
        self._draw_quad("present")

    def present_cpu_frame(self, frame_rgb):
        """Upload a CPU RGB frame and present it to the screen.
        Used by paths that needed to draw cv2 overlays (mapping edit
        mode, etc.) on the CPU readback."""
        if frame_rgb is None:
            self.present()
            return
        if frame_rgb.shape[0] != self.h or frame_rgb.shape[1] != self.w:
            import cv2
            frame_rgb = cv2.resize(frame_rgb, (self.w, self.h))
        if not frame_rgb.flags["C_CONTIGUOUS"]:
            frame_rgb = np.ascontiguousarray(frame_rgb)
        # Reuse the frozen texture slot — it's a same-size RGB texture.
        self._frozen_tex.write(frame_rgb.tobytes())
        self.ctx.screen.use()
        sw, sh = self._screen_size
        self.ctx.viewport = (0, 0, sw, sh)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._frozen_tex.use(location=0)
        # Use blit_swizzle in passthrough mode (already RGB).
        p = self._programs["blit_swizzle"]
        p["u_src"].value = 0
        p["u_swizzle_bgr"].value = 0
        self._draw_quad("blit_swizzle")


# ── Math helpers ─────────────────────────────────────────────────────

def _homography(src_pts, dst_pts):
    """Compute the 3x3 homography mapping src_pts (4×2) → dst_pts (4×2).

    Standard DLT — kept inline so the renderer doesn't pull in cv2 just
    for one matrix solve. Returns None if the system is degenerate (the
    quad collapsed to a line).
    """
    A = np.zeros((8, 8), dtype=np.float64)
    b = np.zeros(8, dtype=np.float64)
    for i in range(4):
        x, y = src_pts[i]
        u, v = dst_pts[i]
        A[2 * i] = [x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y]
        A[2 * i + 1] = [0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y]
        b[2 * i] = u
        b[2 * i + 1] = v
    try:
        h = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None
    H = np.array([
        [h[0], h[1], h[2]],
        [h[3], h[4], h[5]],
        [h[6], h[7], 1.0],
    ], dtype=np.float64)
    return H


def _hsv_to_rgb_unit(h, s, v):
    """Standalone HSV→RGB for the lissajous solid colour. h/s/v in [0,1]."""
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i = i % 6
    if i == 0: return (v, t, p)
    if i == 1: return (q, v, p)
    if i == 2: return (p, v, t)
    if i == 3: return (p, q, v)
    if i == 4: return (t, p, v)
    return (v, p, q)
