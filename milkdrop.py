"""MilkDrop-style feedback visualizer for VJ-pi.

Built on the proven Spike-E engine (pygame GL window + moderngl ping-pong
FBOs, GLES 3.0, RGBA8 — one GL context, V3D-safe). Each preset is a
feedback fragment shader: it samples the PREVIOUS frame at a warped UV,
fades it, and adds new light on top, so motion accumulates into the
flowing MilkDrop look.

Runs under the VENV python (pygame + moderngl + numpy), its own process.
The GL window takes keyboard focus, so the wireless keyboard drives it
directly:

    n / ] / Space  next preset      p / [   previous preset
    a              toggle auto-cycle (every ~18s)
    f              cycle feedback resolution (speed vs detail)
    Esc            quit

Run:
    ./venv/bin/python milkdrop.py [--display 1] [--res 1280x720]

Adding a preset = add a fragment shader to PRESETS. Each receives:
  sampler2D u_prev  (previous frame)   float u_t (seconds)   vec2 v_uv (0..1)
A preset that fails to compile is skipped (doesn't kill the show).
"""
import argparse
import sys
import time

import numpy as np
import pygame

VS = """#version 300 es
precision highp float;
layout(location = 0) in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

FS_BLIT = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
void main() { frag = texture(u_tex, v_uv); }
"""

# Shared GLSL prelude for presets (palette helper).
PRELUDE = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_prev;
uniform float u_t;
vec3 pal(float x) {            // smooth cosine palette
    return 0.5 + 0.5 * cos(6.2831853 * (x + vec3(0.0, 0.33, 0.67)));
}
"""

# --- presets -----------------------------------------------------------

# All presets are FULL-FRAME: a per-pixel base pattern fills the whole
# screen, blended ~80/20 with the warped+faded previous frame, so the
# feedback adds flowing motion while the screen stays full of colour.

# "flow": full-frame plasma flowing through a gentle rotate+zoom feedback.
P_FLOW = PRELUDE + """
void main() {
    vec2 uv = v_uv, c = uv - 0.5;
    float ang = 0.020 * sin(u_t * 0.20);
    float s = sin(ang), co = cos(ang);
    vec3 prev = texture(u_prev, (mat2(co, -s, s, co) * c) * 0.99 + 0.5).rgb;
    float pl = sin(uv.x * 9.0 + u_t)
             + sin(uv.y * 9.0 + u_t * 1.2)
             + sin((uv.x + uv.y) * 7.0 + u_t * 0.8)
             + sin(length(c) * 14.0 - u_t * 2.0);
    vec3 base = pal(pl * 0.12 + u_t * 0.03);
    frag = vec4(mix(prev * 0.97, base, 0.18), 1.0);
}
"""

# "mandala": full-frame kaleidoscope — N-fold mirrored pattern over the
# whole screen, with a slow rotating feedback. The symmetric MilkDrop look.
P_MANDALA = PRELUDE + """
const float SYM = 6.0;
void main() {
    vec2 uv = v_uv, c = uv - 0.5;
    float ang = 0.015 * sin(u_t * 0.25);
    float s = sin(ang), co = cos(ang);
    vec3 prev = texture(u_prev, (mat2(co, -s, s, co) * c) * 0.99 + 0.5).rgb;
    float a = atan(c.y, c.x), r = length(c);
    float seg = 6.2831853 / SYM;
    a = abs(mod(a, seg) - seg * 0.5);
    vec2 k = vec2(cos(a), sin(a)) * r;
    float pat = sin(k.x * 22.0 + u_t)
              + sin(k.y * 22.0 - u_t * 1.3)
              + sin(r * 30.0 - u_t * 2.0);
    vec3 base = pal(pat * 0.12 + r + u_t * 0.04);
    frag = vec4(mix(prev * 0.965, base, 0.20), 1.0);
}
"""

# "tunnel": full-frame radial pattern + strong zoom -> flying through an
# endless tunnel.
P_TUNNEL = PRELUDE + """
void main() {
    vec2 uv = v_uv, c = uv - 0.5;
    float rot = 0.04, s = sin(rot), co = cos(rot);
    vec3 prev = texture(u_prev, (mat2(co, -s, s, co) * c) * 0.96 + 0.5).rgb;
    float r = length(c), a = atan(c.y, c.x);
    float pat = sin(1.0 / (r + 0.05) * 4.0 - u_t * 3.0) + sin(a * 8.0 + u_t);
    vec3 base = pal(pat * 0.15 + r * 2.0 + u_t * 0.05);
    frag = vec4(mix(prev * 0.95, base, 0.22), 1.0);
}
"""

# "swirl": full-frame angular pattern dragged by a radius-dependent vortex.
P_SWIRL = PRELUDE + """
void main() {
    vec2 uv = v_uv, c = uv - 0.5;
    float r = length(c);
    float ang = 0.15 / (r + 0.18), s = sin(ang), co = cos(ang);
    vec3 prev = texture(u_prev, (mat2(co, -s, s, co) * c) * 0.992 + 0.5).rgb;
    float a = atan(c.y, c.x);
    float pat = sin(a * 5.0 + r * 18.0 - u_t * 2.0) + sin(r * 24.0 + u_t);
    vec3 base = pal(pat * 0.13 + a * 0.10 + u_t * 0.05);
    frag = vec4(mix(prev * 0.96, base, 0.20), 1.0);
}
"""

PRESETS = {
    "flow": P_FLOW,
    "mandala": P_MANDALA,
    "tunnel": P_TUNNEL,
    "swirl": P_SWIRL,
}
PRESET_ORDER = ["flow", "mandala", "tunnel", "swirl"]
RES_CHOICES = [(960, 540), (1280, 720), (1600, 900)]


def open_window(display_idx):
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 0)
    pygame.display.gl_set_attribute(
        pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_ES)
    pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME
    try:
        size = pygame.display.get_desktop_sizes()[display_idx]
    except (pygame.error, IndexError, AttributeError):
        size = (1280, 720)
    # vsync=1 caps to the display refresh and lets the GPU idle between
    # frames (the projector is ~24Hz). Fall back if a kwarg isn't supported.
    for kwargs in ({"display": display_idx, "vsync": 1},
                   {"display": display_idx},
                   {}):
        try:
            pygame.display.set_mode(size, flags, **kwargs)
            break
        except (pygame.error, TypeError):
            continue
    pygame.display.set_caption("VJ MilkDrop")
    return size


class Engine:
    def __init__(self, ctx, mgl, res):
        self.ctx = ctx
        self.mgl = mgl
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        self.vbo = ctx.buffer(quad.tobytes())
        self.blit = ctx.program(vertex_shader=VS, fragment_shader=FS_BLIT)
        self.blit_vao = ctx.vertex_array(self.blit, [(self.vbo, "2f", "in_pos")])
        self._cache = {}          # name -> (prog, vao) or None if it failed
        self.set_res(res)

    def set_res(self, res):
        self.res = res
        self.fbo_a, self.tex_a = self._make_fbo(res)
        self.fbo_b, self.tex_b = self._make_fbo(res)

    def _make_fbo(self, res):
        tex = self.ctx.texture(res, 4)        # RGBA8 only on V3D
        tex.filter = (self.mgl.LINEAR, self.mgl.LINEAR)
        tex.repeat_x = False
        tex.repeat_y = False
        fbo = self.ctx.framebuffer(color_attachments=[tex])
        fbo.clear(0.0, 0.0, 0.0, 1.0)
        return fbo, tex

    def preset(self, name):
        if name not in self._cache:
            try:
                prog = self.ctx.program(vertex_shader=VS,
                                        fragment_shader=PRESETS[name])
                vao = self.ctx.vertex_array(prog, [(self.vbo, "2f", "in_pos")])
                self._cache[name] = (prog, vao)
                print(f"[milkdrop] preset '{name}' compiled", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[milkdrop] preset '{name}' FAILED: {exc}", flush=True)
                self._cache[name] = None
        return self._cache[name]

    def render(self, name, t, win_size):
        item = self.preset(name)
        if item is None:
            return
        prog, vao = item
        w, h = self.res
        # feedback pass: prev (a) -> b
        self.fbo_b.use()
        self.ctx.viewport = (0, 0, w, h)
        self.tex_a.use(location=0)
        for k, v in (("u_prev", 0), ("u_t", t)):
            try:
                prog[k].value = v
            except KeyError:
                pass
        vao.render(self.mgl.TRIANGLE_STRIP)
        # show on the projector
        self.ctx.screen.use()
        self.ctx.viewport = (0, 0, win_size[0], win_size[1])
        self.tex_b.use(location=0)
        try:
            self.blit["u_tex"].value = 0
        except KeyError:
            pass
        self.blit_vao.render(self.mgl.TRIANGLE_STRIP)
        # swap ping-pong
        self.fbo_a, self.fbo_b = self.fbo_b, self.fbo_a
        self.tex_a, self.tex_b = self.tex_b, self.tex_a


def main():
    ap = argparse.ArgumentParser(description="MilkDrop-style visualizer")
    ap.add_argument("--display", type=int, default=1,
                    help="display index (1 = projector per Start VJ.sh)")
    ap.add_argument("--res", default="1280x720")
    ap.add_argument("--fps", type=int, default=30,
                    help="frame cap (projector is ~24Hz; 30 idles the GPU)")
    args = ap.parse_args()
    res0 = tuple(int(x) for x in args.res.lower().split("x"))

    win_size = open_window(args.display)
    try:
        import moderngl
    except ImportError:
        print("[milkdrop] moderngl not in venv: ./venv/bin/pip install moderngl",
              flush=True)
        return 1
    ctx = moderngl.create_context(require=300)
    print(f"[milkdrop] GL {ctx.info.get('GL_VERSION', '?')} on "
          f"{ctx.info.get('GL_RENDERER', '?')}", flush=True)

    res_idx = RES_CHOICES.index(res0) if res0 in RES_CHOICES else 1
    eng = Engine(ctx, moderngl, RES_CHOICES[res_idx])

    idx = 0
    auto = False
    auto_period = 18.0
    last_switch = time.perf_counter()
    t0 = time.perf_counter()
    last_beat = t0
    frames = 0
    clock = pygame.time.Clock()
    print("[milkdrop] keys: n/]/space=next  p/[=prev  a=auto  f=res  Esc=quit",
          flush=True)
    print(f"[milkdrop] preset: {PRESET_ORDER[idx]}", flush=True)

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                k = ev.key
                if k == pygame.K_ESCAPE:
                    running = False
                elif k in (pygame.K_n, pygame.K_RIGHTBRACKET, pygame.K_SPACE):
                    idx = (idx + 1) % len(PRESET_ORDER)
                    last_switch = time.perf_counter()
                    print(f"[milkdrop] preset: {PRESET_ORDER[idx]}", flush=True)
                elif k in (pygame.K_p, pygame.K_LEFTBRACKET):
                    idx = (idx - 1) % len(PRESET_ORDER)
                    last_switch = time.perf_counter()
                    print(f"[milkdrop] preset: {PRESET_ORDER[idx]}", flush=True)
                elif k == pygame.K_a:
                    auto = not auto
                    print(f"[milkdrop] auto-cycle {'on' if auto else 'off'}",
                          flush=True)
                elif k == pygame.K_f:
                    res_idx = (res_idx + 1) % len(RES_CHOICES)
                    eng.set_res(RES_CHOICES[res_idx])
                    print(f"[milkdrop] feedback res {RES_CHOICES[res_idx]}",
                          flush=True)

        now = time.perf_counter()
        if auto and now - last_switch >= auto_period:
            idx = (idx + 1) % len(PRESET_ORDER)
            last_switch = now
            print(f"[milkdrop] preset: {PRESET_ORDER[idx]}", flush=True)

        eng.render(PRESET_ORDER[idx], now - t0, win_size)
        pygame.display.flip()

        frames += 1
        if now - last_beat >= 1.0:
            print(f"[milkdrop] {frames / (now - last_beat):5.1f} fps", flush=True)
            frames = 0
            last_beat = now
        clock.tick(args.fps)      # cap -> GPU idles between frames

    pygame.quit()
    print("[milkdrop] DONE.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
