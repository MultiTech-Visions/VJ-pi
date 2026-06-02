"""Test 04: Render a fullscreen quad WITHOUT any vertex attributes.

Uses gl_VertexID to synthesise NDC corners inside the vertex shader
itself, so the draw is entirely free of vertex-attribute binding.
PASS if the FBO is filled with the green-tinted gradient the
fragment shader computes from v_uv.

If THIS works but test 05 (the same thing with an `in vec2 in_pos`
attribute) FAILS, the smoking gun is V3D mis-binding vertex attribute
locations — exactly the failure mode the readback diagnostics
pointed at.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import moderngl
from _common import init_window, read_fbo, save_rgb, make_fbo

W, H = 320, 240
screen, ctx = init_window(size=(W, H))

print(f"GL: {ctx.info.get('GL_VERSION', '?')}")

# Vertex shader: 4 corner positions baked into the shader itself, no
# `in` attributes at all. Pick the right corner via gl_VertexID.
VS = """#version 300 es
precision highp float;
out vec2 v_uv;
const vec2 CORNERS[4] = vec2[4](
    vec2(-1.0, -1.0),
    vec2( 1.0, -1.0),
    vec2(-1.0,  1.0),
    vec2( 1.0,  1.0)
);
void main() {
    vec2 p = CORNERS[gl_VertexID];
    v_uv = p * 0.5 + 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
FS = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
void main() {
    // Recognisable gradient — uv.x → R, uv.y → G, constant B.
    frag = vec4(v_uv.x, v_uv.y, 0.5, 1.0);
}
"""

prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
print(f"program compiled: {len(prog._members) if hasattr(prog, '_members') else '?'} members")

# A VAO with NO buffers attached — just tells GL to invoke the VS
# `vertices=4` times with gl_VertexID = 0..3.
vao = ctx.vertex_array(prog, [])

fbo = make_fbo(ctx, (W, H))
fbo.use()
ctx.viewport = (0, 0, W, H)
ctx.clear(0.0, 0.0, 0.0, 1.0)
vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

img = read_fbo(fbo, (W, H))
mean = img.mean(axis=(0, 1)).astype(int).tolist()
nonzero = int((img != 0).sum())
save_rgb(img, "04_noattrib_quad")

print(f"mean RGB:        {mean}")
print(f"expected approx: [128, 128, 128]  (avg of x/y gradient + 0.5 B)")
print(f"nonzero pixels:  {nonzero} / {W*H*3}")

if nonzero > (W * H * 3) // 2:
    print("[PASS] gl_VertexID quad rasterised — basic shader pipeline works")
    sys.exit(0)
print("[FAIL] gl_VertexID quad produced no fragments")
print("       → shader rasterisation itself is broken on this driver.")
sys.exit(1)
