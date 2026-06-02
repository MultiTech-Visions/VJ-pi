"""Spike E — MilkDrop-style FEEDBACK on V3D (the trippy-visuals test).

This is the one that decides whether Winamp/MilkDrop-style visuals are
possible on the Pi. MilkDrop's whole look = a FEEDBACK BUFFER: every frame
warps the PREVIOUS frame and draws new stuff on top, so motion accumulates
into flowing trails. GStreamer's glshader can't do that (single pass, no
previous-frame input), so this runs our OWN GL renderer with ping-pong
framebuffers.

Runs under the VENV python (pygame + moderngl + numpy) — a separate
process with exactly ONE GL context, so the V3D dual-context rule holds.
Mirrors the proven-on-V3D patterns from the old gpu.py: GLES 3.0,
#version 300 es shaders, explicit layout(location), RGBA8 FBOs only.

It opens a GL window on the projector and runs a feedback loop: warp +
fade the previous frame, add a moving blob, repeat. You should see
flowing, smearing trails — the MilkDrop signature. fps is printed each
second.

Run (venv python — has pygame/moderngl):
    ./venv/bin/python tests/spike_e_feedback.py [--display 1] [--res 1280x720]

Controls: Esc or close the window to quit.

REPORT BACK: did you see flowing trails (not just a moving dot)? The fps.
Any errors.
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

# Feedback: sample the PREVIOUS frame at a warped+zoomed+rotated UV, fade
# it, then add a moving coloured blob on top. The warp is what turns a dot
# into flowing trails.
FS_FEEDBACK = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_prev;
uniform float u_t;
void main() {
    vec2 c = v_uv - 0.5;
    float ang = 0.012 + 0.010 * sin(u_t * 0.20);   // slow rotation
    float s = sin(ang), co = cos(ang);
    c = mat2(co, -s, s, co) * c;
    c *= 0.985;                                     // zoom -> outward flow
    vec3 prev = texture(u_prev, c + 0.5).rgb * 0.962;   // fade -> trails decay

    // new content: a couple of moving blobs, colour-cycling
    vec2 p = v_uv - 0.5;
    float e = 0.0;
    for (int i = 0; i < 2; i++) {
        float fi = float(i);
        vec2 pos = 0.30 * vec2(sin(u_t * (1.1 + fi) + fi * 2.1),
                               cos(u_t * (1.4 + fi) - fi * 1.7));
        e += smoothstep(0.05, 0.0, length(p - pos));
    }
    vec3 col = 0.5 + 0.5 * cos(u_t + vec3(0.0, 2.094, 4.188));
    frag = vec4(max(prev, e * col), 1.0);
}
"""

FS_BLIT = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
void main() { frag = texture(u_tex, v_uv); }
"""


def open_window(display_idx):
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 0)
    pygame.display.gl_set_attribute(
        pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_ES)
    pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME
    try:
        sizes = pygame.display.get_desktop_sizes()
        size = sizes[display_idx]
    except (pygame.error, IndexError, AttributeError):
        size = (1280, 720)
    try:
        screen = pygame.display.set_mode(size, flags, display=display_idx)
    except (pygame.error, TypeError):
        print(f"[spike-e] display={display_idx} failed; using default",
              flush=True)
        screen = pygame.display.set_mode(size, flags)
    # Distinct title so the labwc 'OpenGL Renderer' fullscreen rule does NOT
    # grab this window — pygame already sized it to the projector.
    pygame.display.set_caption("VJ feedback spike")
    return screen, size


def make_fbo(ctx, mgl, w, h):
    tex = ctx.texture((w, h), 4)            # RGBA8 (V3D-safe; RGB8 -> zeros)
    tex.filter = (mgl.LINEAR, mgl.LINEAR)
    tex.repeat_x = False
    tex.repeat_y = False
    fbo = ctx.framebuffer(color_attachments=[tex])
    fbo.clear(0.0, 0.0, 0.0, 1.0)
    return fbo, tex


def main():
    ap = argparse.ArgumentParser(description="Spike E: MilkDrop feedback")
    ap.add_argument("--display", type=int, default=1,
                    help="display index (1 = projector per Start VJ.sh)")
    ap.add_argument("--res", default="1280x720",
                    help="feedback buffer resolution (upscaled to the window)")
    args = ap.parse_args()
    w, h = (int(x) for x in args.res.lower().split("x"))

    screen, (win_w, win_h) = open_window(args.display)
    try:
        import moderngl
    except ImportError:
        print("[spike-e] moderngl not installed in the venv. The launcher "
              "should install it; or: ./venv/bin/pip install moderngl",
              flush=True)
        return 1
    ctx = moderngl.create_context()
    print(f"[spike-e] GL: {ctx.info.get('GL_RENDERER', '?')} | "
          f"{ctx.info.get('GL_VERSION', '?')}", flush=True)
    print(f"[spike-e] window {win_w}x{win_h} (display {args.display}), "
          f"feedback {w}x{h}", flush=True)

    quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
    vbo = ctx.buffer(quad.tobytes())
    fb_prog = ctx.program(vertex_shader=VS, fragment_shader=FS_FEEDBACK)
    blit_prog = ctx.program(vertex_shader=VS, fragment_shader=FS_BLIT)
    fb_vao = ctx.vertex_array(fb_prog, [(vbo, "2f", "in_pos")])
    blit_vao = ctx.vertex_array(blit_prog, [(vbo, "2f", "in_pos")])

    fbo_a, tex_a = make_fbo(ctx, moderngl, w, h)
    fbo_b, tex_b = make_fbo(ctx, moderngl, w, h)
    src_fbo, src_tex, dst_fbo, dst_tex = fbo_a, tex_a, fbo_b, tex_b

    def set_uniform(prog, name, value):
        try:
            prog[name].value = value
        except KeyError:
            pass

    print("[spike-e] running — watch for flowing TRAILS. Esc to quit.",
          flush=True)
    t0 = time.perf_counter()
    last_beat = t0
    frames = 0
    clock = pygame.time.Clock()
    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                running = False
        now = time.perf_counter()
        t = now - t0

        # feedback pass: src (prev) -> dst
        dst_fbo.use()
        ctx.viewport = (0, 0, w, h)
        src_tex.use(location=0)
        set_uniform(fb_prog, "u_prev", 0)
        set_uniform(fb_prog, "u_t", t)
        fb_vao.render(moderngl.TRIANGLE_STRIP)

        # blit dst -> projector window
        ctx.screen.use()
        ctx.viewport = (0, 0, win_w, win_h)
        dst_tex.use(location=0)
        set_uniform(blit_prog, "u_tex", 0)
        blit_vao.render(moderngl.TRIANGLE_STRIP)
        pygame.display.flip()

        src_fbo, src_tex, dst_fbo, dst_tex = dst_fbo, dst_tex, src_fbo, src_tex

        frames += 1
        if now - last_beat >= 1.0:
            print(f"[spike-e] {frames / (now - last_beat):5.1f} fps", flush=True)
            frames = 0
            last_beat = now
        clock.tick(120)

    pygame.quit()
    print("[spike-e] DONE.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
