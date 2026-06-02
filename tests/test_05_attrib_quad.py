"""Test 05: Render the same fullscreen quad WITH an `in_pos` attribute.

Mirrors what the main app does: vertex shader declares
`layout(location = 0) in vec2 in_pos`, the VAO binds a VBO to
attribute `in_pos`, draws as TRIANGLE_STRIP. PASS if the FBO is
filled with the same gradient as test 04.

If test 04 PASSES and this FAILS, V3D is mis-binding the vertex
attribute and `glBindAttribLocation` (or sticking with gl_VertexID
in the main pipeline) is the right fix.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import moderngl
from _common import init_window, read_fbo, save_rgb, make_fbo

W, H = 320, 240
screen, ctx = init_window(size=(W, H))

print(f"GL: {ctx.info.get('GL_VERSION', '?')}")

# Vertex shader with an attribute, explicit layout(location=0).
VS = """#version 300 es
precision highp float;
layout(location = 0) in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""
FS = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
void main() {
    frag = vec4(v_uv.x, v_uv.y, 0.5, 1.0);
}
"""

prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
# Inspect what moderngl thinks `in_pos` location is — if this prints
# anything other than 0, V3D's introspection has overridden the
# explicit layout qualifier.
try:
    attr = prog["in_pos"]
    print(f"prog['in_pos'].location = {attr.location}")
except Exception as exc:  # noqa: BLE001
    print(f"prog['in_pos'] introspection failed: {exc!r}")

verts = np.array([
    -1.0, -1.0,
     1.0, -1.0,
    -1.0,  1.0,
     1.0,  1.0,
], dtype=np.float32)
vbo = ctx.buffer(verts.tobytes())
vao = ctx.vertex_array(prog, [(vbo, "2f", "in_pos")])

fbo = make_fbo(ctx, (W, H))
fbo.use()
ctx.viewport = (0, 0, W, H)
ctx.clear(0.0, 0.0, 0.0, 1.0)
vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

img = read_fbo(fbo, (W, H))
mean = img.mean(axis=(0, 1)).astype(int).tolist()
nonzero = int((img != 0).sum())
save_rgb(img, "05_attrib_quad")

print(f"mean RGB:        {mean}")
print(f"expected approx: [128, 128, 128]")
print(f"nonzero pixels:  {nonzero} / {W*H*3}")

if nonzero > (W * H * 3) // 2:
    print("[PASS] in_pos attribute correctly bound — main pipeline path is sound")
    sys.exit(0)
print("[FAIL] attribute quad produced no fragments")
print("       → V3D is mis-binding the vertex attribute. Compare with test 04 —")
print("         if 04 passed and 05 failed, this is THE bug. Fix is glBindAttribLocation")
print("         before link, or rewrite the main pipeline to use gl_VertexID.")
sys.exit(1)
