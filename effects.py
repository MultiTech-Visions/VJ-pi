import math
import os
import random
from collections import OrderedDict
from pathlib import Path

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


# ── Spatial grid cache ─────────────────────────────────────────────────
#
# Most generatives need an `np.indices((h, w), dtype=np.float32)` grid
# every frame. At 1280×720 that's a 5.5 MB allocation thrown away each
# render. Cache the grids per-resolution at module level — they're
# spatial constants that never change once the output size is set.
# Stays bounded because the engine only changes resolution at startup.

_GRID_CACHE = {}

# kaleidoscope's remap maps depend only on (w, h, segments) — never on the
# pixels — so cache them. The trig (sqrt/arctan2/cos/sin over a full grid) is
# the expensive, GIL-bound part; caching reduces each frame to a single
# cv2.remap (GIL-free, so it also parallelises in threaded mapping mode).
_KALEIDO_CACHE = {}


def _grid(w, h):
    key = (w, h)
    g = _GRID_CACHE.get(key)
    if g is None:
        y, x = np.indices((h, w), dtype=np.float32)
        # Return read-only views; the caller multiplies into a new array
        # rather than mutating these in place.
        y.setflags(write=False)
        x.setflags(write=False)
        g = (y, x)
        _GRID_CACHE[key] = g
    return g


# ── Generative base layers ─────────────────────────────────────────────

def plasma(ctx):
    w, h, t = ctx.w, ctx.h, ctx.t
    y0, x0 = _grid(w, h)
    x = x0 * (8.0 / w)
    y = y0 * (8.0 / h)
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
    y0, x0 = _grid(w, h)
    dx, dy = x0 - cx, y0 - cy
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
    y, x = _grid(w, h)
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
    y, x = _grid(w, h)
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
    y, x = _grid(w, h)
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
    y, x = _grid(w, h)
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


# ── Table-top photo slideshow ──────────────────────────────────────────
#
# A camera pans (forever, to the right, with a gentle sinusoidal "curve")
# across an infinite dark-wood table. Photos from assets/images/ drop onto
# the surface one after another — each slightly overlapping the last, at a
# small random rotation that slowly drifts — landing with a soft shadow.
# Near-top-down view, so photos stay rectangular and a single cv2.warpAffine
# stamps each one (drawn once, painter's-order far→near). Pure CPU/numpy/cv2
# — no GL, no V3D risk; it's a sibling to the GPU `cube` slideshow but the
# many-overlapping-photos layout would be a per-pixel loop on the GPU (the
# V3D-stalling "splatting" trap), whereas on the CPU it's O(photos).
#
# Everything is a deterministic function of ctx.t and the slot index, so it
# never jitters and is resumable: slot i sits at a hashed position/rotation
# and shows shuffled image i % N. Drop timing is keyed to where the slot is
# on screen, so a photo always drops just as it slides in from the right.

_TABLE_HERE = Path(__file__).resolve().parent
_TABLE_IMAGES_DIR = _TABLE_HERE / "assets" / "images"

# Card geometry (base resolution; on-screen size is reached by the affine
# `scale`, so the decoded card is cached once per image, not per size).
_CARD_LONG = 360          # longest side of the decoded photo, px
_CARD_BORDER = 14         # white matte border baked around the photo, px
_CARD_MATTE = (236, 234, 227)
_CARD_CACHE_MAX = 64      # LRU cap on decoded cards (~0.4 MB each)

# Shadow under a landed photo.
_SH_STRENGTH = 0.5
_SH_BLUR = 17             # odd Gaussian kernel
_SH_OFF = 8              # down-right offset at scale 1


def _table_list_images():
    if not _TABLE_IMAGES_DIR.exists():
        return []
    paths = []
    for suffix in ("*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.JPEG"):
        paths.extend(_TABLE_IMAGES_DIR.glob(suffix))
    return sorted(str(p) for p in paths)


def _smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


def _make_wood(w, h):
    """A seamless (tileable in both axes) dark-wood-grain panel sized to the
    canvas. Built once per resolution; the camera reveals it by np.roll, which
    wraps cleanly because every term is periodic over the tile."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u = xx / w * (2.0 * math.pi)
    v = yy / h * (2.0 * math.pi)
    # Fine grain runs horizontally (varies along y), waving slightly with x.
    grain = np.sin(v * 22.0 + np.sin(u * 2.0) * 0.7)
    # Broad rings/figure for the plank look.
    rings = np.sin(v * 5.0 + np.sin(u * 3.0) * 0.5)
    # Occasional darker plank streaks along the grain.
    streak = np.sin(u * 4.0) * 0.5 + np.sin(u * 7.0) * 0.25
    val = 0.55 + 0.18 * grain + 0.16 * rings + 0.06 * streak
    val = np.clip(val, 0.18, 1.0)
    # Dark walnut: deep brown that brightens with the grain value.
    base = np.array([34.0, 22.0, 14.0])      # RGB floor
    top = np.array([96.0, 60.0, 33.0])       # RGB at bright grain
    wood = base[None, None, :] + val[..., None] * (top - base)[None, None, :]
    return np.clip(wood, 0, 255).astype(np.uint8)


class _TableTop:
    """Stateful CPU generator: the only persistent state is the decoded-card
    LRU and the (stable) image shuffle; all motion is derived from ctx.t."""

    def __init__(self):
        self._cards = OrderedDict()   # path -> bordered RGB card (LRU)
        self._images = []
        self._shuffle = []
        self._last_scan_t = -1e9
        self._wood = None
        self._wood_size = None

    def _refresh_images(self, t):
        # Re-scan the folder at most ~once a second so the operator can drop
        # new photos in mid-show without restarting.
        if t - self._last_scan_t < 1.0 and self._images:
            return
        self._last_scan_t = t
        imgs = _table_list_images()
        if imgs != self._images:
            self._images = imgs
            order = list(range(len(imgs)))
            random.Random(1234567).shuffle(order)   # stable, reproducible
            self._shuffle = order

    def _card(self, path):
        card = self._cards.get(path)
        if card is not None:
            self._cards.move_to_end(path)
            return card
        img = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_4)
        if img is None:
            img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        ih, iw = img.shape[:2]
        if iw == 0 or ih == 0:
            return None
        scale = _CARD_LONG / float(max(iw, ih))
        nw = max(1, int(round(iw * scale)))
        nh = max(1, int(round(ih * scale)))
        photo = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        photo = cv2.cvtColor(photo, cv2.COLOR_BGR2RGB)
        b = _CARD_BORDER
        card = np.empty((nh + 2 * b, nw + 2 * b, 3), dtype=np.uint8)
        card[:] = _CARD_MATTE
        card[b:b + nh, b:b + nw] = photo
        self._cards[path] = card
        while len(self._cards) > _CARD_CACHE_MAX:
            self._cards.popitem(last=False)
        return card

    def _slot(self, i):
        """Deterministic per-slot layout: (rel_x, rel_y, base_angle, drift_ph).
        rel_x/rel_y are offsets in units of the photo target size."""
        r = random.Random((i * 2654435761) & 0xFFFFFFFF)
        rel_x = r.uniform(-0.10, 0.10)
        rel_y = r.uniform(-0.32, 0.32)
        ang = r.uniform(-0.22, 0.22)          # ~±12.5° base tilt
        ph = r.uniform(0.0, 6.2831853)
        return rel_x, rel_y, ang, ph

    def _stamp(self, canvas, card, cx, cy, angle, scale, alpha):
        H, W = canvas.shape[:2]
        ch, cw = card.shape[:2]
        ca, sa = math.cos(angle), math.sin(angle)
        cs, ss = scale * ca, scale * sa
        # Canvas-space corners of the (rotated, scaled) card.
        hw, hh = cw * 0.5, ch * 0.5
        local = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
        pts = np.empty((4, 2), dtype=np.float32)
        for k, (lx, ly) in enumerate(local):
            pts[k, 0] = cs * lx - ss * ly + cx
            pts[k, 1] = ss * lx + cs * ly + cy
        sdx = _SH_OFF * scale
        sdy = _SH_OFF * scale
        margin = int(_SH_BLUR + max(sdx, sdy) + 4)
        minx = int(math.floor(pts[:, 0].min())) - margin
        miny = int(math.floor(pts[:, 1].min())) - margin
        maxx = int(math.ceil(pts[:, 0].max())) + margin
        maxy = int(math.ceil(pts[:, 1].max())) + margin
        ox, oy = max(0, minx), max(0, miny)
        ex, ey = min(W, maxx), min(H, maxy)
        if ex <= ox or ey <= oy:
            return
        bw, bh = ex - ox, ey - oy
        # Affine: card image coords -> tile coords (canvas minus tile origin).
        M = np.array([
            [cs, -ss, cx - cs * hw + ss * hh - ox],
            [ss, cs, cy - ss * hw - cs * hh - oy],
        ], dtype=np.float32)
        tile = cv2.warpAffine(card, M, (bw, bh), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        poly = (pts - np.array([ox, oy], dtype=np.float32)).astype(np.int32)
        mask = np.zeros((bh, bw), dtype=np.uint8)
        cv2.fillConvexPoly(mask, poly, 255)
        roi = canvas[oy:ey, ox:ex]   # a writable view into the canvas

        # Soft drop shadow, offset down-right, laid down before the photo.
        # Darken in uint8 (saturating subtract) — no full-tile float pass.
        spoly = poly + np.array([int(sdx), int(sdy)], dtype=np.int32)
        smask = np.zeros((bh, bw), dtype=np.uint8)
        cv2.fillConvexPoly(smask, spoly, 255)
        smask = cv2.GaussianBlur(smask, (_SH_BLUR, _SH_BLUR), 0)
        dark = int(_SH_STRENGTH * alpha * 255)
        if dark > 0:
            sh = ((smask.astype(np.uint16) * dark) >> 8).astype(np.uint8)
            cv2.subtract(roi, cv2.cvtColor(sh, cv2.COLOR_GRAY2BGR), dst=roi)

        if alpha >= 0.999:
            # Landed photo: opaque — paste straight in, no blending.
            cv2.copyTo(tile, mask, roi)
        else:
            # Still dropping (only the freshest photo): fade in by alpha.
            a = (mask.astype(np.float32) * (alpha / 255.0))[..., None]
            roi[:] = (roi.astype(np.float32) * (1.0 - a)
                      + tile.astype(np.float32) * a).astype(np.uint8)

    def __call__(self, ctx):
        rw, rh, t = ctx.w, ctx.h, ctx.t
        self._refresh_images(t)

        # Render at a capped internal resolution and upscale (like the GPU
        # generators): the per-photo compositing is the cost, so rendering at
        # full 1080p/2K would crush the Pi's CPU. The wood + photos upscale
        # cleanly. VJ_TABLETOP_MAX_W tunes the cap (0 = render at full res).
        try:
            max_w = int(os.environ.get("VJ_TABLETOP_MAX_W", "720"))
        except ValueError:
            max_w = 720
        if max_w > 0 and rw > max_w:
            w = max_w
            h = max(2, int(round(rh * max_w / rw)))
            h -= h % 2
        else:
            w, h = rw, rh

        # Wood background, scrolled by the panning (and gently curving) camera.
        if self._wood_size != (w, h):
            self._wood = _make_wood(w, h)
            self._wood_size = (w, h)
        target = (0.24 + 0.12 * ctx.py) * min(w, h)   # photo longest side, px
        target = max(48.0, target)
        photo_scale = target / float(_CARD_LONG)
        spacing = target * 0.74                        # ~26% overlap
        # Camera path: pan right forever, plus a slow lateral "curve".
        pan_speed = (0.045 + 0.11 * ctx.px) * w        # world px / sec
        cam_x = pan_speed * t
        cam_y = 0.06 * h * math.sin(t * 0.23)
        ox = int(cam_x) % w
        oy = int(cam_y) % h
        # np.roll already returns a fresh, writable array — no .copy() needed.
        canvas = np.roll(self._wood, (-oy, -ox), axis=(0, 1))

        if not self._images:
            return canvas if (w, h) == (rw, rh) else cv2.resize(
                canvas, (rw, rh), interpolation=cv2.INTER_LINEAR)

        n = len(self._images)
        # Slot world-X = i*spacing; visible when its screen-x is on canvas.
        # screen_x = w*0.5 + (X_i - cam_x). Photos drop as they cross in from
        # the right edge.
        entry = w                       # screen-x where a photo first lands
        travel = 0.34 * w               # screen-x distance to finish landing
        i_lo = int(math.floor((cam_x - 0.5 * w - target) / spacing)) - 1
        i_hi = int(math.ceil((cam_x + 0.5 * w + target) / spacing)) + 1
        for i in range(i_lo, i_hi + 1):
            rel_x, rel_y, base_ang, ph = self._slot(i)
            world_x = (i + rel_x) * spacing
            screen_x = w * 0.5 + (world_x - cam_x)
            if screen_x > entry + target or screen_x < -target:
                continue
            dp = _smoothstep((entry - screen_x) / travel)
            if dp <= 0.0:
                continue
            path = self._images[self._shuffle[i % n]]
            card = self._card(path)
            if card is None:
                continue
            ease = _smoothstep(min(1.0, dp / 0.55))
            alpha = _smoothstep(min(1.0, dp / 0.45))
            drop_scale = photo_scale * (1.0 + 0.15 * (1.0 - ease))
            rise = (1.0 - ease) * 0.12 * target
            angle = base_ang + 0.05 * math.sin(t * 0.2 + ph)
            cx = screen_x
            cy = h * 0.5 + rel_y * h - rise
            self._stamp(canvas, card, cx, cy, angle, drop_scale, alpha)
        if (w, h) != (rw, rh):
            canvas = cv2.resize(canvas, (rw, rh), interpolation=cv2.INTER_LINEAR)
        return canvas


tabletop = _TableTop()


# ── Frame-transforming effects ─────────────────────────────────────────

def kaleidoscope(src, segments=6):
    h, w = src.shape[:2]
    segments = int(segments)
    key = (w, h, segments)
    maps = _KALEIDO_CACHE.get(key)
    if maps is None:
        cx, cy = w * 0.5, h * 0.5
        y, x = _grid(w, h)
        dx, dy = x - cx, y - cy
        r = np.sqrt(dx * dx + dy * dy)
        a = np.arctan2(dy, dx)
        seg_a = 2 * np.pi / max(1, segments)
        a = np.abs((a % seg_a) - seg_a * 0.5)
        nx = (cx + r * np.cos(a)).astype(np.float32)
        ny = (cy + r * np.sin(a)).astype(np.float32)
        # Pack to fixed-point (CV_16SC2 + interpolation table). cv2.remap is
        # ~1.2x faster on these than on float32 maps with no visible quality
        # loss, and they use less memory so more (size×segments) combos stay
        # cached as param_x animates the segment count.
        maps = cv2.convertMaps(nx, ny, cv2.CV_16SC2)
        # Bound the cache — segments animates with param_x, so a handful of
        # (size × segments) combos can accumulate over a long set.
        if len(_KALEIDO_CACHE) > 64:
            _KALEIDO_CACHE.clear()
        _KALEIDO_CACHE[key] = maps
    m1, m2 = maps
    return cv2.remap(src, m1, m2, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def mirror_h(src):
    h, w = src.shape[:2]
    half = src[:, : w // 2]
    return np.concatenate([half, half[:, ::-1]], axis=1)


def feedback_blend(prev, new_frame, zoom=1.02, rotate=0.5, fade=0.92):
    """Trails / mirroring feedback.

    Old behaviour was addWeighted(warped, 1.0, new, 1.0) — a literal sum
    that saturated bright content to pure white within a handful of
    frames. Max-blend on a faded warped copy preserves the trail look
    without ever exceeding 255, so we don't wash out. BORDER_CONSTANT=0
    also stops edge reflection from pumping extra brightness in from
    outside the frame.
    """
    if prev is None:
        return new_frame.copy()
    h, w = new_frame.shape[:2]
    M = cv2.getRotationMatrix2D((w * 0.5, h * 0.5), rotate, zoom)
    warped = cv2.warpAffine(prev, M, (w, h),
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=(0, 0, 0))
    warped = (warped.astype(np.float32) * fade).astype(np.uint8)
    return cv2.max(warped, new_frame)


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
