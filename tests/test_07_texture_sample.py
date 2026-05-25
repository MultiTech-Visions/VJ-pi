"""Test 07: Upload a known BGR texture, sample it in a shader,
render to an FBO, read back. PASS if the FBO matches what we uploaded.

This is the exact path the main pipeline uses for clip frames
(`upload_clip` → texture upload → `draw_clip_base` → blit_swizzle
shader → FBO). If THIS fails but tests 05 and 06 pass, the bug is
specifically in texture sampling / format handling on V3D.
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

# Build a recognisable BGR test pattern: top-left quadrant pure red
# (RGB 255,0,0; BGR bytes 0,0,255), top-right pure green, bottom-left
# pure blue, bottom-right pure white. We upload as BGR (cv2 order)
# and let the shader swizzle to RGB.
pattern = np.zeros((H, W, 3), dtype=np.uint8)
pattern[:H//2, :W//2] = (0, 0, 255)   # BGR red
pattern[:H//2, W//2:] = (0, 255, 0)   # BGR green
pattern[H//2:, :W//2] = (255, 0, 0)   # BGR blue
pattern[H//2:, W//2:] = (255, 255, 255)  # white
save_rgb(np.flip(pattern, axis=2), "07_input_pattern")  # save the input as RGB for reference

# Upload as 4-channel texture (RGBA8 internal format). RGB8 textures
# are spec-required to be sampleable in GLES 3.0, but V3D has been
# observed to give zero samples on RGB8 — RGBA8 is the safe path. We
# pad the BGR pattern with a constant alpha byte.
pattern_bgra = np.dstack([pattern, np.full(pattern.shape[:2], 255, dtype=np.uint8)])
src_tex = ctx.texture((W, H), 4, dtype="f1")
src_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
src_tex.repeat_x = False
src_tex.repeat_y = False
src_tex.write(pattern_bgra.tobytes())

VS = """#version 300 es
precision highp float;
layout(location = 0) in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""
# Same blit_swizzle shader the main pipeline uses for clip frames:
# Y-flip so cv2 top-row → GL top, and swap BGR → RGB.
FS = """#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_src;
void main() {
    vec4 c = texture(u_src, vec2(v_uv.x, 1.0 - v_uv.y));
    frag = vec4(c.bgr, 1.0);
}
"""

prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
prog["u_src"].value = 0

verts = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype=np.float32)
vbo = ctx.buffer(verts.tobytes())
vao = ctx.vertex_array(prog, [(vbo, "2f", "in_pos")])

fbo = make_fbo(ctx, (W, H))
fbo.use()
ctx.viewport = (0, 0, W, H)
ctx.clear(0.0, 0.0, 0.0, 1.0)
src_tex.use(location=0)
vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

img = read_fbo(fbo, (W, H))
save_rgb(img, "07_texture_sample")

# Sample one pixel from each quadrant of the OUTPUT and check it
# matches what we expect after BGR→RGB swizzle + Y-flip:
#   input top-left (BGR red, byte order 0,0,255) → top-left of output
#   should display as RGB (255,0,0) — but the shader Y-flips, so
#   what we drew at canvas top-left came from texture sample at
#   uv.y=1 (the BOTTOM of the input texture).
#   With the input texture stored cv2-style (row 0 at top, ends up
#   at GL-bottom after upload), sampling at v_uv.y=1 → GL uv.y=0 →
#   GL-bottom = cv2 row 0 = canvas top of input. So output top-left
#   should match input top-left.
tl = img[H//4, W//4].tolist()
tr = img[H//4, 3*W//4].tolist()
bl = img[3*H//4, W//4].tolist()
br = img[3*H//4, 3*W//4].tolist()
print(f"output top-left:     {tl}  (expected ~[255,   0,   0]  red)")
print(f"output top-right:    {tr}  (expected ~[  0, 255,   0]  green)")
print(f"output bottom-left:  {bl}  (expected ~[  0,   0, 255]  blue)")
print(f"output bottom-right: {br}  (expected ~[255, 255, 255]  white)")

def near(rgb, expected, tol=40):
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(rgb, expected))

ok = (near(tl, (255,0,0)) and near(tr, (0,255,0))
      and near(bl, (0,0,255)) and near(br, (255,255,255)))

if ok:
    print("[PASS] texture sample reproduces the uploaded pattern")
    sys.exit(0)
print("[FAIL] texture sample output doesn't match uploaded pattern")
print("       → V3D may not sample RGB8 textures correctly, or the")
print("         Y-flip / BGR-swizzle math is wrong on this driver.")
sys.exit(1)
