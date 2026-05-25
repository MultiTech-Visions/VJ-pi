"""Test 01: Can we get a working OpenGL/GLES context at all?

No rendering. Just opens a window, asks SDL for a GLES 3.0 context,
asks moderngl to wrap it, dumps every version string the driver
reports. PASS if all of that succeeds. FAIL means we don't have GL.
"""
import os
import sys

# Run from the project root so `tests/_common.py` is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import init_window

screen, ctx = init_window(size=(320, 240))

print(f"GL_RENDERER:                  {ctx.info.get('GL_RENDERER', '?')}")
print(f"GL_VERSION:                   {ctx.info.get('GL_VERSION', '?')}")
print(f"GL_SHADING_LANGUAGE_VERSION:  {ctx.info.get('GL_SHADING_LANGUAGE_VERSION', '?')}")
print(f"GL_VENDOR:                    {ctx.info.get('GL_VENDOR', '?')}")
print(f"GL_MAX_TEXTURE_SIZE:          {ctx.info.get('GL_MAX_TEXTURE_SIZE', '?')}")

print("[PASS] context created and version queries returned")
sys.exit(0)
