import numpy as np
import cv2


class EffectContext:
    """Per-frame state passed to effects."""

    def __init__(self, w, h, t, mouse_norm):
        self.w = w
        self.h = h
        self.t = t
        self.mx, self.my = mouse_norm  # 0..1


# ── Generative base layers ─────────────────────────────────────────────

def plasma(ctx):
    w, h, t = ctx.w, ctx.h, ctx.t
    y, x = np.indices((h, w), dtype=np.float32)
    x = x / w * 8.0
    y = y / h * 8.0
    v = (np.sin(x + t) + np.sin(y + t * 1.3)
         + np.sin((x + y) * 0.5 + t * 0.7)
         + np.sin(np.sqrt(x * x + y * y) + t * 1.7)) * 0.25
    v = (v + 1.0) * 0.5
    hue = ((v * 180.0 + t * 20.0) % 180.0).astype(np.uint8)
    sat = np.full_like(hue, 255)
    val = np.full_like(hue, 255)
    hsv = np.stack([hue, sat, val], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def tunnel(ctx):
    w, h, t = ctx.w, ctx.h, ctx.t
    cx, cy = w * 0.5, h * 0.5
    y, x = np.indices((h, w), dtype=np.float32)
    dx, dy = x - cx, y - cy
    r = np.sqrt(dx * dx + dy * dy) + 1.0
    a = np.arctan2(dy, dx)
    u = (200.0 / r + t * 2.0) % 1.0
    v_ = (a / np.pi + 1.0) * 0.5
    chk = (((u * 8).astype(np.int32) + (v_ * 16).astype(np.int32)) % 2).astype(np.uint8) * 255
    hue = ((v_ * 180.0 + t * 30.0) % 180.0).astype(np.uint8)
    sat = np.full_like(hue, 255)
    hsv = np.stack([hue, sat, chk], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def starfield(ctx, density=200):
    """Pseudo-random twinkling stars."""
    w, h, t = ctx.w, ctx.h, ctx.t
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    rng = np.random.default_rng(42)
    xs = rng.integers(0, w, size=density)
    ys = rng.integers(0, h, size=density)
    phases = rng.uniform(0, 6.28, size=density)
    brights = ((np.sin(t * 2 + phases) + 1) * 0.5 * 255).astype(np.uint8)
    frame[ys, xs] = brights[:, None]
    # Slight bloom
    return cv2.GaussianBlur(frame, (3, 3), 0)


# ── Frame-transforming effects ─────────────────────────────────────────

def kaleidoscope(src, segments=6):
    h, w = src.shape[:2]
    cx, cy = w * 0.5, h * 0.5
    y, x = np.indices((h, w), dtype=np.float32)
    dx, dy = x - cx, y - cy
    r = np.sqrt(dx * dx + dy * dy)
    a = np.arctan2(dy, dx)
    seg_a = 2 * np.pi / max(1, segments)
    a = np.abs((a % seg_a) - seg_a * 0.5)
    nx = (cx + r * np.cos(a)).astype(np.float32)
    ny = (cy + r * np.sin(a)).astype(np.float32)
    return cv2.remap(src, nx, ny, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def mirror_h(src):
    h, w = src.shape[:2]
    half = src[:, : w // 2]
    return np.concatenate([half, half[:, ::-1]], axis=1)


def feedback_blend(prev, new_frame, zoom=1.02, rotate=0.5, fade=0.92):
    if prev is None:
        return new_frame.copy()
    h, w = new_frame.shape[:2]
    M = cv2.getRotationMatrix2D((w * 0.5, h * 0.5), rotate, zoom)
    warped = cv2.warpAffine(prev, M, (w, h), borderMode=cv2.BORDER_REFLECT)
    warped = (warped.astype(np.float32) * fade).astype(np.uint8)
    return cv2.addWeighted(warped, 1.0, new_frame, 1.0, 0)


def rgb_split(src, offset=8):
    r = np.roll(src[:, :, 0], -offset, axis=1)
    g = src[:, :, 1]
    b = np.roll(src[:, :, 2], offset, axis=1)
    return np.stack([r, g, b], axis=-1)


def invert(src):
    return 255 - src


def posterize(src, levels=4):
    step = max(1, 256 // levels)
    return (src // step) * step


def edges(src):
    gray = cv2.cvtColor(src, cv2.COLOR_RGB2GRAY)
    e = cv2.Canny(gray, 80, 160)
    return cv2.cvtColor(e, cv2.COLOR_GRAY2RGB)


# ── Compositing ────────────────────────────────────────────────────────

def screen_blend(a, b):
    """1 - (1-a)(1-b). Brights pop; great for fire/sparks pre-keyed to black."""
    af = a.astype(np.float32) / 255.0
    bf = b.astype(np.float32) / 255.0
    return ((1.0 - (1.0 - af) * (1.0 - bf)) * 255.0).astype(np.uint8)
