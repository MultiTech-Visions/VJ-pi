"""Test 03: Can we clear an offscreen FBO to a colour?

Same as test 02 but writes to a moderngl framebuffer instead of the
default one, then reads that FBO's texture back. PASS if the readback
is magenta.

The main app's pipeline composes everything into FBOs (ping-pong
chain) and only blits the final FBO to the screen. If FBO clears
don't work, NOTHING the main app draws can land anywhere.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import init_window, read_fbo, save_rgb, is_near, make_fbo

W, H = 320, 240
screen, ctx = init_window(size=(W, H))

print(f"GL: {ctx.info.get('GL_VERSION', '?')}")

# RGBA8 colour attachment (see make_fbo() for the V3D / GLES 3.0
# spec rationale — RGB8 isn't required to be color-renderable).
fbo = make_fbo(ctx, (W, H))
print(f"FBO created: w={W} h={H} components=4 dtype=f1 (RGBA8)")

# Clear it.
fbo.use()
ctx.viewport = (0, 0, W, H)
ctx.clear(1.0, 0.0, 1.0, 1.0)

img = read_fbo(fbo, (W, H))
mean = img.mean(axis=(0, 1)).astype(int).tolist()
save_rgb(img, "03_clear_fbo")

print(f"mean RGB:  {mean}")
print(f"expected:  [255, 0, 255]  (magenta)")

if is_near(mean, (255, 0, 255), tol=30):
    print("[PASS] FBO clear produces magenta")
    sys.exit(0)
print("[FAIL] FBO clear did NOT produce magenta even with RGBA8")
print("       → readback path itself is broken (driver / moderngl / Wayland).")
sys.exit(1)
