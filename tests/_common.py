"""Shared setup for the diagnostic tests.

Every test calls `init_window()` to open a small GLES window with the
same attributes Start VJ.sh uses, and `save_rgb(img, name)` to dump a
PNG to tests/output/. Kept tiny on purpose — each test is meant to
be readable from top to bottom in one screen.
"""
import os
import sys
import pygame
import numpy as np


def init_window(size=(320, 240)):
    """Open a GLES 3.0 OpenGL window. Returns (screen, ctx). Exits the
    process if context creation fails — the test runner will record
    this as the failure for that test."""
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 0)
    pygame.display.gl_set_attribute(
        pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_ES,
    )
    pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
    try:
        screen = pygame.display.set_mode(size, pygame.OPENGL | pygame.DOUBLEBUF)
    except pygame.error as exc:
        print(f"[FAIL] pygame.display.set_mode raised: {exc!r}")
        sys.exit(1)

    try:
        import moderngl
        ctx = moderngl.create_context(require=300)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] moderngl.create_context raised: {exc!r}")
        sys.exit(1)

    return screen, ctx


def save_rgb(img, name):
    """Save an (H, W, 3) uint8 RGB array to tests/output/<name>.png."""
    import cv2
    out_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.png")
    cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    print(f"saved: {path}")
    return path


def read_screen(ctx, size):
    """Read the default framebuffer back as an (H, W, 3) uint8 RGB
    array, top-down (GL is bottom-up natively, so we flip).

    Reads 4 components (RGBA) and drops alpha rather than reading 3
    components: the GLES 3.0 spec only requires GL_RGBA + UNSIGNED_BYTE
    to be supported by glReadPixels — GL_RGB + UNSIGNED_BYTE is optional
    and V3D 7.1 doesn't appear to support it (silent all-zero reads).
    Calling ctx.finish() first to make sure the GPU has actually
    completed prior draws / clears before we read.
    """
    w, h = size
    ctx.finish()
    data = ctx.screen.read(components=4, alignment=1, dtype="f1")
    img = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
    return np.ascontiguousarray(img[::-1, :, :3])


def read_fbo(fbo, size):
    """Same shape as read_screen but for a moderngl framebuffer."""
    w, h = size
    fbo.ctx.finish()
    data = fbo.read(components=4, alignment=1, dtype="f1")
    img = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
    return np.ascontiguousarray(img[::-1, :, :3])


def make_fbo(ctx, size):
    """Build an FBO with a 4-component (RGBA8) colour attachment.

    GLES 3.0 doesn't require GL_RGB8 to be color-renderable, and V3D
    7.1 doesn't support it as a render target — FBO creation with
    RGB8 succeeds silently but writes / clears produce undefined
    (typically zero) results. GL_RGBA8 IS spec-mandated as color-
    renderable, so every FBO in the test suite (and the main pipeline)
    uses 4 components.
    """
    import moderngl
    w, h = size
    tex = ctx.texture((w, h), 4, dtype="f1")
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.repeat_x = False
    tex.repeat_y = False
    return ctx.framebuffer(color_attachments=[tex])


def is_near(mean_rgb, expected_rgb, tol=40):
    """True if every channel of mean_rgb is within `tol` of expected."""
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(mean_rgb, expected_rgb))
