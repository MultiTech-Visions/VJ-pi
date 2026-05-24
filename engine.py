import os
import time

import pygame
import numpy as np
import cv2

from clips import ClipPool
from effects import (
    EffectContext, plasma, tunnel, starfield, warp, waves, cells,
    lissajous, moire, metaballs,
    kaleidoscope, mirror_h, feedback_blend, rgb_split,
    invert, posterize, edges, screen_blend,
)


GENERATIVES = [
    "plasma", "tunnel", "starfield",
    "warp", "waves", "cells",
    "lissajous", "moire", "metaballs",
]

GENERATIVE_FNS = {
    "plasma": plasma,
    "tunnel": tunnel,
    "starfield": starfield,
    "warp": warp,
    "waves": waves,
    "cells": cells,
    "lissajous": lissajous,
    "moire": moire,
    "metaballs": metaballs,
}

FX_TOGGLES = [
    "kaleido", "mirror", "feedback",
    "invert", "posterize", "edges", "rgb_split",
]

# How fast the arrow keys move param_x / param_y (units of 0..1 per second).
PARAM_RATE = 0.6


class Engine:
    def __init__(self, cfg, screen):
        self.cfg = cfg
        self.screen = screen
        self.w, self.h = cfg.width, cfg.height
        self.clock = pygame.time.Clock()
        self.start_time = time.time()

        self.clips = ClipPool(cfg.clips_dir, (self.w, self.h))
        self.overlays = ClipPool(cfg.overlays_dir, (self.w, self.h))

        self.active_generative = None
        self.fx_state = {fx: False for fx in FX_TOGGLES}
        self.hit_type = None
        self.hit_frames_left = 0
        self.blackout = False
        self.freeze = False
        self.frozen_frame = None
        self.prev_frame = None

        # Arrow-key driven FX parameters, 0..1. Replaces mouse XY.
        self.param_x = 0.5
        self.param_y = 0.5

        self.running = True

    # ── Public actions ────────────────────────────────────────────────

    def select_clip(self, idx):
        if idx < len(self.clips):
            self.clips.select(idx)
            self.active_generative = None

    def toggle_overlay(self, idx):
        if idx >= len(self.overlays):
            return
        if self.overlays.active_idx == idx:
            self.overlays.deselect()
        else:
            self.overlays.select(idx)

    def select_generative(self, idx):
        if idx >= len(GENERATIVES):
            return
        name = GENERATIVES[idx]
        if self.active_generative == name:
            self.active_generative = None
        else:
            self.active_generative = name
            self.clips.deselect()

    def fire_hit(self, kind, frames=5):
        self.hit_type = kind
        self.hit_frames_left = frames

    def toggle_fx(self, name):
        if name in self.fx_state:
            self.fx_state[name] = not self.fx_state[name]

    def kill_all(self):
        for k in self.fx_state:
            self.fx_state[k] = False
        self.hit_frames_left = 0
        self.blackout = False
        self.freeze = False
        self.overlays.deselect()

    def toggle_blackout(self):
        self.blackout = not self.blackout

    def toggle_freeze(self):
        self.freeze = not self.freeze
        if self.freeze:
            self.frozen_frame = self.prev_frame.copy() if self.prev_frame is not None else None

    def quit(self):
        self.running = False

    def switch_output_display(self, new_idx):
        """Re-open the fullscreen output window on a different display."""
        try:
            num = pygame.display.get_num_displays()
        except pygame.error:
            num = 1
        if new_idx < 0 or new_idx >= num or new_idx == self.cfg.display:
            return
        self.cfg.display = new_idx
        flags = pygame.FULLSCREEN | pygame.SCALED if self.cfg.fullscreen else 0
        try:
            self.screen = pygame.display.set_mode(
                (self.w, self.h), flags, display=new_idx
            )
        except TypeError:
            os.environ["SDL_VIDEO_FULLSCREEN_DISPLAY"] = str(new_idx)
            self.screen = pygame.display.set_mode((self.w, self.h), flags)
        pygame.display.set_caption("pi-paint VJ — Output")
        pygame.mouse.set_visible(not self.cfg.fullscreen)

    def update_params_from_keys(self, dt):
        """Arrow keys nudge param_x / param_y while held."""
        keys = pygame.key.get_pressed()
        step = PARAM_RATE * dt
        if keys[pygame.K_LEFT]:
            self.param_x = max(0.0, self.param_x - step)
        if keys[pygame.K_RIGHT]:
            self.param_x = min(1.0, self.param_x + step)
        if keys[pygame.K_UP]:
            self.param_y = max(0.0, self.param_y - step)
        if keys[pygame.K_DOWN]:
            self.param_y = min(1.0, self.param_y + step)

    # ── Render pipeline ───────────────────────────────────────────────

    def _build_base(self, ctx):
        clip_frame = self.clips.read()
        if clip_frame is not None:
            return clip_frame
        fn = GENERATIVE_FNS.get(self.active_generative)
        if fn is not None:
            return fn(ctx)
        return np.zeros((self.h, self.w, 3), dtype=np.uint8)

    def _apply_fx(self, frame, ctx):
        s = self.fx_state
        if s["kaleido"]:
            segs = int(3 + ctx.px * 9)
            frame = kaleidoscope(frame, segments=segs)
        if s["mirror"]:
            frame = mirror_h(frame)
        if s["rgb_split"]:
            frame = rgb_split(frame, offset=int(4 + ctx.px * 20))
        if s["posterize"]:
            frame = posterize(frame, levels=int(2 + ctx.py * 6))
        if s["edges"]:
            frame = edges(frame)
        if s["invert"]:
            frame = invert(frame)
        if s["feedback"]:
            zoom = 1.0 + ctx.px * 0.08
            rot = (ctx.py - 0.5) * 4.0
            frame = feedback_blend(self.prev_frame, frame, zoom=zoom, rotate=rot)
        return frame

    def _apply_overlay(self, frame):
        ov = self.overlays.read()
        if ov is not None:
            frame = screen_blend(frame, ov)
        return frame

    def _apply_hits(self, frame):
        if self.hit_frames_left <= 0:
            return frame
        n = self.hit_frames_left
        self.hit_frames_left -= 1
        if self.hit_type == "strobe":
            return np.full_like(frame, 255)
        if self.hit_type == "black_flash":
            return np.zeros_like(frame)
        if self.hit_type == "invert_flash":
            return invert(frame)
        if self.hit_type == "zoom_punch":
            scale = 1.0 + 0.25 * (n / 5.0)
            M = cv2.getRotationMatrix2D((self.w * 0.5, self.h * 0.5), 0, scale)
            return cv2.warpAffine(frame, M, (self.w, self.h))
        if self.hit_type == "rgb_smash":
            return rgb_split(frame, offset=28)
        return frame

    def compose_frame(self):
        """Build the next output frame without blitting."""
        if self.blackout:
            frame = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        elif self.freeze and self.frozen_frame is not None:
            frame = self.frozen_frame
        else:
            ctx = EffectContext(
                self.w, self.h, time.time() - self.start_time,
                (self.param_x, self.param_y),
            )
            frame = self._build_base(ctx)
            frame = self._apply_fx(frame, ctx)
            frame = self._apply_overlay(frame)
            frame = self._apply_hits(frame)

        if not self.freeze:
            self.prev_frame = frame
        return frame

    def blit_to_output(self, frame):
        surface = pygame.image.frombuffer(frame.tobytes(), (self.w, self.h), "RGB")
        if self.screen.get_size() != (self.w, self.h):
            surface = pygame.transform.scale(surface, self.screen.get_size())
        self.screen.blit(surface, (0, 0))
        pygame.display.flip()

    def render(self):
        """Convenience: compose + blit (used when there is no control window)."""
        frame = self.compose_frame()
        self.blit_to_output(frame)
        return frame

    def run(self, control=None):
        from keymap import dispatch
        last_t = time.time()
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN:
                    dispatch(self, event.key, event.mod)
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP,
                                    pygame.MOUSEMOTION):
                    if control is not None:
                        control.handle_event(event)
            now = time.time()
            dt = now - last_t
            last_t = now
            self.update_params_from_keys(dt)
            frame = self.compose_frame()
            self.blit_to_output(frame)
            if control is not None:
                control.render(frame)
            self.clock.tick(self.cfg.fps)
        self.clips.release_all()
        self.overlays.release_all()
