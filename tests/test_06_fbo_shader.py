"""Test 06: Render the actual plasma shader (same source as the main
app's gpu.py) to an FBO and read it back.

This is the closest test to what the main pipeline does for a
generative pattern. PASS if the FBO ends up with the colourful plasma
pattern (mean far from zero, channels not all equal).

If tests 04 and 05 pass but THIS fails, plasma's shader specifically
has an issue on V3D (precision, builtin function quirk, etc).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import moderngl
from _common import init_window, read_fbo, save_rgb

W, H = 320, 240
screen, ctx = init_window(size=(W, H))

print(f"GL: {ctx.info.get('GL_VERSION', '?')}")

VS = """#version 300 es
precision highp float;
layout(location = 0) in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# Verbatim copy of the plasma body from gpu.py's FS_PLASMA so we're
# testing the exact same shader the main pipeline uses.
FS = """#version 300 es
precision highp float;
precision highp int;
in vec2 v_uv;
out vec4 frag;
uniform float u_t;
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
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

prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
prog["u_t"].value = 1.5

verts = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype=np.float32)
vbo = ctx.buffer(verts.tobytes())
vao = ctx.vertex_array(prog, [(vbo, "2f", "in_pos")])

tex = ctx.texture((W, H), 3, dtype="f1")
fbo = ctx.framebuffer(color_attachments=[tex])
fbo.use()
ctx.viewport = (0, 0, W, H)
ctx.clear(0.0, 0.0, 0.0, 1.0)
vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

img = read_fbo(fbo, (W, H))
mean = img.mean(axis=(0, 1)).astype(int).tolist()
nonzero = int((img != 0).sum())
save_rgb(img, "06_fbo_shader")

print(f"mean RGB:       {mean}")
print(f"nonzero pixels: {nonzero} / {W*H*3}")
print(f"channel max:    R={img[:,:,0].max()} G={img[:,:,1].max()} B={img[:,:,2].max()}")

if nonzero > (W * H * 3) // 2 and max(img.max(axis=(0,1))) > 200:
    print("[PASS] plasma shader rendered into FBO")
    sys.exit(0)
print("[FAIL] plasma shader produced no fragments / wrong colours")
sys.exit(1)
