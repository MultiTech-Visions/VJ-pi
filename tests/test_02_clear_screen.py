"""Test 02: Can we clear the default framebuffer to a colour?

Opens a 320×240 GLES window, clears the back buffer to magenta
(1,0,1), reads the back buffer back via moderngl, saves the readback
as tests/output/02_clear_screen.png. PASS if mean pixel ≈ magenta.

Sanity check that ctx.clear actually paints pixels in the visible
framebuffer. If THIS fails, you can't even get a colour onscreen.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pygame
from _common import init_window, read_screen, save_rgb, is_near

W, H = 320, 240
screen, ctx = init_window(size=(W, H))

print(f"GL: {ctx.info.get('GL_VERSION', '?')}")

# Clear back buffer to magenta.
ctx.screen.use()
ctx.viewport = (0, 0, W, H)
ctx.clear(1.0, 0.0, 1.0, 1.0)

# Read BEFORE flipping — flip swaps so the back buffer becomes undefined.
img = read_screen(ctx, (W, H))
pygame.display.flip()

mean = img.mean(axis=(0, 1)).astype(int).tolist()
save_rgb(img, "02_clear_screen")

print(f"mean RGB:  {mean}")
print(f"expected:  [255, 0, 255]  (magenta)")

if is_near(mean, (255, 0, 255), tol=30):
    print("[PASS] screen clear produces magenta")
    sys.exit(0)
print("[FAIL] screen clear did NOT produce magenta")
sys.exit(1)
