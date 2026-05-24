import numpy as np
import cv2


class EffectContext:
    """Per-frame state passed to effects.

    `px`, `py` are user-tuned parameters in 0..1 (driven by the arrow keys
    on the bluetooth keyboard — see Engine.update_params_from_keys).
    """

    def __init__(self, w, h, t, params):
        self.w = w
        self.h = h
        self.t = t
        self.px, self.py = params  # 0..1


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


def starfield(ctx, density=220):
    """Classic flying-through-space starfield.

    Stars are spawned at the centre, fly outward along a fixed angle, fade in
    as they near the edge, and respawn once they leave the frame. py adjusts
    travel speed.
    """
    w, h, t = ctx.w, ctx.h, ctx.t
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    cx, cy = w * 0.5, h * 0.5
    max_r = float(np.sqrt(cx * cx + cy * cy))

    rng = np.random.default_rng(42)
    angles = rng.uniform(0.0, 2.0 * np.pi, size=density).astype(np.float32)
    seeds = rng.uniform(0.0, 1.0, size=density).astype(np.float32)
    base_speed = rng.uniform(0.12, 0.40, size=density).astype(np.float32)
    speed = base_speed * (0.4 + ctx.py * 2.0)

    depth = (seeds + t * speed) % 1.0
    r = depth * max_r * 1.3
    xs = (cx + np.cos(angles) * r)
    ys = (cy + np.sin(angles) * r)

    sizes = np.maximum(1, (depth * 3.5 + 0.5).astype(np.int32))
    bright = (255 * np.minimum(depth * 1.6, 1.0)).astype(np.int32)

    for i in range(density):
        xi, yi = int(xs[i]), int(ys[i])
        if 0 <= xi < w and 0 <= yi < h:
            b = int(bright[i])
            cv2.circle(frame, (xi, yi), int(sizes[i]), (b, b, b), -1)
    return frame


def warp(ctx, count=120):
    """Hyperspace streaks radiating from the centre. px adjusts speed."""
    w, h, t = ctx.w, ctx.h, ctx.t
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    cx, cy = w * 0.5, h * 0.5
    max_r = float(np.sqrt(cx * cx + cy * cy)) * 1.2
    speed = 0.4 + ctx.px * 2.0

    rng = np.random.default_rng(11)
    angles = rng.uniform(0.0, 2.0 * np.pi, size=count).astype(np.float32)
    phases = rng.uniform(0.0, 1.0, size=count).astype(np.float32)

    depth = (phases + t * speed) % 1.0
    r_end = depth * max_r
    r_start = np.maximum(r_end - max_r * 0.18, 0.0)

    cos_a, sin_a = np.cos(angles), np.sin(angles)
    x1 = (cx + cos_a * r_start).astype(np.int32)
    y1 = (cy + sin_a * r_start).astype(np.int32)
    x2 = (cx + cos_a * r_end).astype(np.int32)
    y2 = (cy + sin_a * r_end).astype(np.int32)
    bright = (255 * depth).astype(np.int32)

    for i in range(count):
        b = int(bright[i])
        cv2.line(frame, (int(x1[i]), int(y1[i])), (int(x2[i]), int(y2[i])),
                 (b, b, b), 1, cv2.LINE_AA)
    return frame


def waves(ctx):
    """Two-source colourful ripples / interference pattern."""
    w, h, t = ctx.w, ctx.h, ctx.t
    y, x = np.indices((h, w), dtype=np.float32)
    cx1 = w * 0.3 + np.sin(t * 0.5) * w * 0.15
    cy1 = h * 0.5 + np.cos(t * 0.4) * h * 0.2
    cx2 = w * 0.7 + np.cos(t * 0.6) * w * 0.15
    cy2 = h * 0.5 + np.sin(t * 0.45) * h * 0.2
    period = 22.0 + ctx.px * 60.0

    r1 = np.sqrt((x - cx1) ** 2 + (y - cy1) ** 2) / period
    r2 = np.sqrt((x - cx2) ** 2 + (y - cy2) ** 2) / period
    v = (np.sin(r1 * np.pi * 2 - t * 2) + np.sin(r2 * np.pi * 2 + t * 1.5)) * 0.25 + 0.5

    hue = ((v * 180.0 + t * 18.0) % 180.0).astype(np.uint8)
    sat = np.full_like(hue, 220)
    val = (v * 255).astype(np.uint8)
    hsv = np.stack([hue, sat, val], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def cells(ctx):
    """Organic pulsing cellular pattern (animated quasi-voronoi)."""
    w, h, t = ctx.w, ctx.h, ctx.t
    y, x = np.indices((h, w), dtype=np.float32)
    scale = 0.018 + ctx.px * 0.04
    u = x * scale + np.sin(y * scale * 0.6 + t) * 0.4
    v = y * scale + np.cos(x * scale * 0.6 + t * 1.1) * 0.4
    pat = np.abs(np.sin(u * np.pi) * np.sin(v * np.pi))
    pat = pat ** 0.6  # punch up midtones

    hue = ((u * 28.0 + t * 14.0) % 180.0).astype(np.uint8)
    sat = np.full_like(hue, 210)
    val = (pat * 255).astype(np.uint8)
    hsv = np.stack([hue, sat, val], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def lissajous(ctx):
    """Oscilloscope-style Lissajous curve. px/py select harmonic ratios."""
    w, h, t = ctx.w, ctx.h, ctx.t
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    cx, cy = w * 0.5, h * 0.5
    rx, ry = w * 0.42, h * 0.42
    a = 2 + int(ctx.px * 7)
    b = 3 + int(ctx.py * 7)
    n = 1400
    phi = np.linspace(0, 2 * np.pi, n, dtype=np.float32)
    xs = cx + np.sin(phi * a + t * 0.7) * rx
    ys = cy + np.sin(phi * b + t * 1.1) * ry
    pts = np.stack([xs, ys], axis=-1).reshape(-1, 1, 2).astype(np.int32)

    hue_val = int((t * 24.0) % 180.0)
    color_hsv = np.array([[[hue_val, 220, 255]]], dtype=np.uint8)
    color = cv2.cvtColor(color_hsv, cv2.COLOR_HSV2RGB)[0, 0]
    col = (int(color[0]), int(color[1]), int(color[2]))
    cv2.polylines(frame, [pts], False, col, 2, cv2.LINE_AA)
    # Subtle bloom so the trace glows like a real CRT.
    return cv2.GaussianBlur(frame, (5, 5), 0)


def moire(ctx):
    """Concentric-ring moiré from two slowly orbiting sources."""
    w, h, t = ctx.w, ctx.h, ctx.t
    cx, cy = w * 0.5, h * 0.5
    y, x = np.indices((h, w), dtype=np.float32)
    off_x = w * 0.10
    off_y = h * 0.10
    cx1 = cx + np.sin(t * 0.5) * off_x
    cy1 = cy + np.cos(t * 0.4) * off_y
    cx2 = cx - np.sin(t * 0.5) * off_x
    cy2 = cy - np.cos(t * 0.4) * off_y

    spacing = 5.0 + ctx.px * 18.0
    r1 = np.sqrt((x - cx1) ** 2 + (y - cy1) ** 2) / spacing
    r2 = np.sqrt((x - cx2) ** 2 + (y - cy2) ** 2) / spacing
    pat = (np.sin(r1 + t * 2.0) + np.sin(r2 - t * 1.5)) * 0.25 + 0.5

    hue = ((pat * 180.0 + t * 22.0) % 180.0).astype(np.uint8)
    sat = np.full_like(hue, 210)
    val = (pat * 255).astype(np.uint8)
    hsv = np.stack([hue, sat, val], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def metaballs(ctx, n=6):
    """Classic sum-of-fields metaballs — organic merging blobs."""
    w, h, t = ctx.w, ctx.h, ctx.t
    y, x = np.indices((h, w), dtype=np.float32)
    field = np.zeros((h, w), dtype=np.float32)
    influence = (w * 26.0) * (0.5 + ctx.py * 1.8)

    for i in range(n):
        phase = i * 6.2831853 / n
        bx = w * 0.5 + np.cos(t * 0.5 + phase * 1.3) * w * 0.35
        by = h * 0.5 + np.sin(t * 0.7 + phase * 1.7) * h * 0.35
        r2 = (x - bx) ** 2 + (y - by) ** 2 + 1.0
        field += influence / r2

    intensity = np.clip(field / 2.5, 0.0, 1.0)
    hue = ((intensity * 80.0 + t * 20.0) % 180.0).astype(np.uint8)
    sat = np.full_like(hue, 230)
    val = (intensity * 255).astype(np.uint8)
    hsv = np.stack([hue, sat, val], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


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
