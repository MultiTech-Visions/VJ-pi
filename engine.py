import os
import random
import time

import pygame
import numpy as np
import cv2

from clips import ClipPool
from mapping import MappingManager
from state import load_state, save_state
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

# How many favorite slots per pool (matches the number / QWERTY rows).
FAV_SLOTS = 10


def _coerce_favs(value):
    """Sanity-check a favourites list from disk into a length-FAV_SLOTS list."""
    if not isinstance(value, list):
        return [None] * FAV_SLOTS
    padded = (value + [None] * FAV_SLOTS)[:FAV_SLOTS]
    return [v if isinstance(v, str) else None for v in padded]


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

        # Display picker state lives on the engine so both the HUD click
        # handler and the keyboard shortcuts can drive it.
        try:
            self.num_displays = max(1, pygame.display.get_num_displays())
        except pygame.error:
            self.num_displays = 1
        self.pending_display = cfg.display

        # Favourite slots (1-0 for clips, Q-P for overlays). Saved by
        # filename stem so they survive re-orderings of the library.
        persisted = load_state()
        self.clip_favorites = _coerce_favs(persisted.get("clip_favorites"))
        self.overlay_favorites = _coerce_favs(persisted.get("overlay_favorites"))

        # Projection-mapping mode. When enabled, the render pipeline draws
        # each group's content into its spaces' quads on a black canvas;
        # most live-action keys (clip / gen / FX / params / favourites)
        # route to the selected group instead of global state.
        self.mapping = MappingManager(persisted.get("mapping"))
        self.mode = "mapping" if self.mapping.enabled else "live"
        # Hide the cursor only in clean live fullscreen — in mapping mode
        # the operator needs to drag corners around in the HUD preview.
        self._mapping_persist_dirty = False

        # Autopilot — engaged by double-tapping Enter, disengaged by any
        # other key press. While engaged, the engine drives clip changes,
        # FX toggling and param drift on jittered intervals.
        self.auto_mode = False
        self.auto_clip_interval = 8.0   # seconds between clip changes
        self.auto_fx_interval   = 4.0   # seconds between FX toggles
        self.last_enter_t = 0.0
        self._auto_next_clip_at    = 0.0
        self._auto_next_fx_at      = 0.0
        self._auto_next_param_at   = 0.0
        self._auto_next_overlay_at = 0.0
        self._auto_target_x = 0.5
        self._auto_target_y = 0.5

        self.running = True

    # ── Mapping-mode action routing ───────────────────────────────────

    def _in_mapping(self):
        return self.mode == "mapping" and self.mapping.selected_group() is not None

    def _persist_mapping(self):
        """Queue a save of mapping config — actually written after the
        next frame so we don't hit disk on every key."""
        self._mapping_persist_dirty = True

    def _flush_mapping_persist(self):
        if not self._mapping_persist_dirty:
            return
        self._mapping_persist_dirty = False
        state = load_state()
        state["mapping"] = self.mapping.to_dict()
        save_state(state)

    # ── Public actions ────────────────────────────────────────────────

    def select_clip(self, idx):
        if self._in_mapping():
            if 0 <= idx < len(self.clips):
                g = self.mapping.selected_group()
                g.content_kind = "clip"
                g.clip_stem = self.clips.name(idx)
                self.clips.ensure_open(idx)
                self._persist_mapping()
            return
        if idx < len(self.clips):
            self.clips.select(idx)
            self.active_generative = None

    def toggle_overlay(self, idx):
        if idx >= len(self.overlays):
            return
        if self._in_mapping():
            g = self.mapping.selected_group()
            new_stem = self.overlays.name(idx)
            if g.overlay_stem == new_stem:
                g.overlay_stem = None
            else:
                g.overlay_stem = new_stem
                self.overlays.ensure_open(idx)
            self._persist_mapping()
            return
        if self.overlays.active_idx == idx:
            self.overlays.deselect()
        else:
            self.overlays.select(idx)

    def browse_clips(self, action, arg=None):
        if self._in_mapping():
            self._browse_for_group("clip", action, arg)
            return
        self._browse(self.clips, action, arg)
        if self.clips.active_idx is not None:
            self.active_generative = None

    def browse_overlays(self, action, arg=None):
        if self._in_mapping():
            self._browse_for_group("overlay", action, arg)
            return
        self._browse(self.overlays, action, arg)

    def _browse_for_group(self, which, action, arg):
        g = self.mapping.selected_group()
        if g is None:
            return
        pool = self.clips if which == "clip" else self.overlays
        if len(pool) == 0:
            return
        stem_attr = "clip_stem" if which == "clip" else "overlay_stem"
        cur = pool.find_by_stem(getattr(g, stem_attr))
        if action == "step":
            if cur is None:
                new_idx = 0 if arg >= 0 else len(pool) - 1
            else:
                new_idx = (cur + arg) % len(pool)
        elif action == "first":
            new_idx = 0
        elif action == "last":
            new_idx = len(pool) - 1
        elif action == "random":
            import random
            new_idx = random.randrange(len(pool))
        elif action == "off":
            setattr(g, stem_attr, None)
            if which == "clip":
                g.content_kind = "blackout"
            self._persist_mapping()
            return
        else:
            return
        setattr(g, stem_attr, pool.name(new_idx))
        if which == "clip":
            g.content_kind = "clip"
        pool.ensure_open(new_idx)
        self._persist_mapping()

    @staticmethod
    def _browse(pool, action, arg):
        if action == "step":
            pool.step(arg)
        elif action == "first":
            pool.first()
        elif action == "last":
            pool.last()
        elif action == "random":
            pool.pick_random()
        elif action == "off":
            pool.deselect()

    def select_generative(self, idx):
        if idx >= len(GENERATIVES):
            return
        name = GENERATIVES[idx]
        if self._in_mapping():
            g = self.mapping.selected_group()
            if g.content_kind == "generative" and g.gen_name == name:
                g.content_kind = "blackout"
                g.gen_name = None
            else:
                g.content_kind = "generative"
                g.gen_name = name
            self._persist_mapping()
            return
        if self.active_generative == name:
            self.active_generative = None
        else:
            self.active_generative = name
            self.clips.deselect()

    def fire_hit(self, kind, frames=5):
        # Hits stay global — they're a panic-button visual smash.
        self.hit_type = kind
        self.hit_frames_left = frames

    def toggle_fx(self, name):
        if self._in_mapping():
            g = self.mapping.selected_group()
            g.fx_state[name] = not g.fx_state.get(name, False)
            self._persist_mapping()
            return
        if name in self.fx_state:
            self.fx_state[name] = not self.fx_state[name]

    def kill_all(self):
        """Panic key: clear FX, hits, overlay, generative, blackout, freeze
        — but keep the current clip playing so the output doesn't suddenly
        drop to black mid-set. Use `0` (long-press while playing nothing)
        or the `-` / `=` cycle keys to change/clear the clip itself.

        In mapping mode we only touch the SELECTED group's FX + overlay so
        the operator's symmetric setup isn't blown away — global blackout
        / freeze / hits still get cleared either way.
        """
        self.hit_frames_left = 0
        self.blackout = False
        self.freeze = False
        if self._in_mapping():
            g = self.mapping.selected_group()
            for k in list(g.fx_state):
                g.fx_state[k] = False
            g.overlay_stem = None
            self._persist_mapping()
            return
        for k in self.fx_state:
            self.fx_state[k] = False
        self.overlays.deselect()
        self.active_generative = None

    # ── Favourites ────────────────────────────────────────────────────

    def play_clip_favorite(self, slot):
        if not 0 <= slot < len(self.clip_favorites):
            return
        stem = self.clip_favorites[slot]
        if stem is None:
            return
        idx = self.clips.find_by_stem(stem)
        if idx is None:
            return
        if self._in_mapping():
            g = self.mapping.selected_group()
            g.content_kind = "clip"
            g.clip_stem = stem
            self.clips.ensure_open(idx)
            self._persist_mapping()
            return
        self.clips.select(idx)
        self.active_generative = None

    def save_clip_favorite(self, slot):
        """Long-press handler. With nothing playing, clears the slot."""
        if not 0 <= slot < len(self.clip_favorites):
            return
        if self._in_mapping():
            g = self.mapping.selected_group()
            self.clip_favorites[slot] = (
                g.clip_stem if g.content_kind == "clip" else None
            )
            self._persist_favorites()
            return
        if self.clips.active_idx is None:
            self.clip_favorites[slot] = None
        else:
            self.clip_favorites[slot] = self.clips.name(self.clips.active_idx)
        self._persist_favorites()

    def play_overlay_favorite(self, slot):
        """Tap toggles the overlay (off when re-tapped on the same one)."""
        if not 0 <= slot < len(self.overlay_favorites):
            return
        stem = self.overlay_favorites[slot]
        if stem is None:
            return
        idx = self.overlays.find_by_stem(stem)
        if idx is None:
            return
        if self._in_mapping():
            g = self.mapping.selected_group()
            if g.overlay_stem == stem:
                g.overlay_stem = None
            else:
                g.overlay_stem = stem
                self.overlays.ensure_open(idx)
            self._persist_mapping()
            return
        if self.overlays.active_idx == idx:
            self.overlays.deselect()
        else:
            self.overlays.select(idx)

    def save_overlay_favorite(self, slot):
        if not 0 <= slot < len(self.overlay_favorites):
            return
        if self._in_mapping():
            g = self.mapping.selected_group()
            self.overlay_favorites[slot] = g.overlay_stem
            self._persist_favorites()
            return
        if self.overlays.active_idx is None:
            self.overlay_favorites[slot] = None
        else:
            self.overlay_favorites[slot] = self.overlays.name(self.overlays.active_idx)
        self._persist_favorites()

    def _persist_favorites(self):
        state = load_state()
        state["clip_favorites"] = self.clip_favorites
        state["overlay_favorites"] = self.overlay_favorites
        save_state(state)

    # ── Autopilot ─────────────────────────────────────────────────────

    def engage_auto(self):
        if self.auto_mode:
            return
        self.auto_mode = True
        now = time.time()
        # First clip change waits a beat so the operator can see what's
        # happening; FX changes start a touch later.
        self._auto_next_clip_at    = now + 1.0
        self._auto_next_fx_at      = now + self.auto_fx_interval
        self._auto_next_param_at   = now
        self._auto_next_overlay_at = now + random.uniform(10, 25)
        print(f"[vj] AUTOPILOT engaged — clip every {self.auto_clip_interval:.1f}s, "
              f"fx every {self.auto_fx_interval:.1f}s")

    def disengage_auto(self):
        if not self.auto_mode:
            return
        self.auto_mode = False
        print("[vj] autopilot disengaged")

    def update_auto(self, now):
        if not self.auto_mode:
            return

        # Base layer cycling: usually a clip, sometimes a generative.
        if now >= self._auto_next_clip_at:
            if len(self.clips) > 0 and random.random() < 0.75:
                self.clips.pick_random()
                self.active_generative = None
            elif GENERATIVES:
                self.active_generative = random.choice(GENERATIVES)
                self.clips.deselect()
            self._auto_next_clip_at = now + self.auto_clip_interval * random.uniform(0.6, 1.7)

        # FX toggling — keep total active count manageable.
        if now >= self._auto_next_fx_at:
            active_on = [k for k, v in self.fx_state.items() if v]
            active_off = [k for k, v in self.fx_state.items() if not v]
            if active_on and (len(active_on) >= 3 or random.random() < 0.45):
                self.fx_state[random.choice(active_on)] = False
            elif active_off:
                self.fx_state[random.choice(active_off)] = True
            self._auto_next_fx_at = now + self.auto_fx_interval * random.uniform(0.5, 1.8)

        # Drift PARAM X/Y toward fresh random targets every few seconds.
        if now >= self._auto_next_param_at:
            self._auto_target_x = random.random()
            self._auto_target_y = random.random()
            self._auto_next_param_at = now + random.uniform(2.0, 5.0)
        lerp = 0.04
        self.param_x += (self._auto_target_x - self.param_x) * lerp
        self.param_y += (self._auto_target_y - self.param_y) * lerp

        # NOTE: autopilot deliberately does NOT fire punch-in hits
        # (strobe / black_flash / invert_flash / zoom_punch / rgb_smash).
        # They're seizure / migraine risks for people on hallucinogens —
        # only the operator can decide to fire them with Z/X/C/V/B.

        # Occasional overlay swap / clear.
        if now >= self._auto_next_overlay_at and len(self.overlays) > 0:
            if random.random() < 0.55:
                self.overlays.pick_random()
            else:
                self.overlays.deselect()
            self._auto_next_overlay_at = now + random.uniform(10, 30)

    def toggle_blackout(self):
        self.blackout = not self.blackout

    def toggle_freeze(self):
        self.freeze = not self.freeze
        if self.freeze:
            self.frozen_frame = self.prev_frame.copy() if self.prev_frame is not None else None

    def quit(self):
        self.running = False

    def cycle_pending_display(self):
        if self.num_displays <= 1:
            return
        self.pending_display = (self.pending_display + 1) % self.num_displays

    def apply_pending_display(self):
        if self.pending_display != self.cfg.display:
            self.switch_output_display(self.pending_display)

    def switch_output_display(self, new_idx):
        """Move the output window to a different display, live.

        Uses the borderless-windowed approach (NOFRAME + display=N + size
        matching the target display) because SDL2's fullscreen flags are
        broken for multi-monitor switching — see
        https://github.com/libsdl-org/SDL/issues/3192. A regular borderless
        window targeted at `display=N` works on every SDL build and can
        be re-issued any number of times.
        """
        if new_idx < 0 or new_idx >= self.num_displays:
            return
        self.cfg.display = new_idx
        self.pending_display = new_idx

        state = load_state()
        state["output_display"] = new_idx
        save_state(state)

        if self.cfg.fullscreen:
            try:
                sizes = pygame.display.get_desktop_sizes()
                dw, dh = sizes[new_idx]
            except (pygame.error, IndexError, AttributeError):
                dw, dh = self.w, self.h
            flags = pygame.NOFRAME
            size = (dw, dh)
        else:
            flags = 0
            size = (self.w, self.h)

        try:
            self.screen = pygame.display.set_mode(size, flags, display=new_idx)
            pygame.display.set_caption("pi-paint VJ — Output")
            pygame.mouse.set_visible(not self.cfg.fullscreen)
            print(f"[vj] output → display {new_idx} ({size[0]}x{size[1]}, "
                  f"borderless={self.cfg.fullscreen})")
        except (TypeError, pygame.error, ValueError) as exc:
            print(f"[vj] switch_output_display({new_idx}) failed: {exc!r}")

    def update_held_hits(self, hit_keys_map):
        """Sustain a punch-in hit while its key is held down.

        compose_frame() decrements hit_frames_left each frame; we top it up
        as long as the corresponding key is pressed. Two frames of headroom
        means an in-flight hit gracefully finishes the frame after release.
        """
        keys = pygame.key.get_pressed()
        for k, hit_name in hit_keys_map.items():
            if keys[k]:
                self.hit_type = hit_name
                if self.hit_frames_left < 2:
                    self.hit_frames_left = 2
                return  # one hit at a time

    def update_params_from_keys(self, dt):
        """Arrows: tune PARAM X/Y in manual mode, tune auto rates in autopilot.

        In mapping/edit mode arrows are ignored entirely — the operator's
        laying out spaces, not tweaking visuals.
        """
        if self.mode == "mapping" and self.mapping.edit_mode:
            return
        keys = pygame.key.get_pressed()
        if self.auto_mode:
            # Up = faster clip changes, Down = slower
            # Right = faster FX changes, Left = slower
            if keys[pygame.K_UP]:
                self.auto_clip_interval = max(1.0, self.auto_clip_interval - dt * 4.0)
            if keys[pygame.K_DOWN]:
                self.auto_clip_interval = min(60.0, self.auto_clip_interval + dt * 4.0)
            if keys[pygame.K_RIGHT]:
                self.auto_fx_interval   = max(0.5, self.auto_fx_interval   - dt * 3.0)
            if keys[pygame.K_LEFT]:
                self.auto_fx_interval   = min(30.0, self.auto_fx_interval  + dt * 3.0)
            return

        step = PARAM_RATE * dt
        if self._in_mapping():
            g = self.mapping.selected_group()
            if keys[pygame.K_LEFT]:
                g.param_x = max(0.0, g.param_x - step)
            if keys[pygame.K_RIGHT]:
                g.param_x = min(1.0, g.param_x + step)
            if keys[pygame.K_UP]:
                g.param_y = max(0.0, g.param_y - step)
            if keys[pygame.K_DOWN]:
                g.param_y = min(1.0, g.param_y + step)
            if (keys[pygame.K_LEFT] or keys[pygame.K_RIGHT]
                    or keys[pygame.K_UP] or keys[pygame.K_DOWN]):
                self._persist_mapping()
            return
        if keys[pygame.K_LEFT]:
            self.param_x = max(0.0, self.param_x - step)
        if keys[pygame.K_RIGHT]:
            self.param_x = min(1.0, self.param_x + step)
        if keys[pygame.K_UP]:
            self.param_y = max(0.0, self.param_y - step)
        if keys[pygame.K_DOWN]:
            self.param_y = min(1.0, self.param_y + step)

    # ── Mapping-mode actions ──────────────────────────────────────────

    def toggle_mapping_mode(self):
        if self.mode == "mapping":
            self.mode = "live"
            self.mapping.enabled = False
            self.mapping.edit_mode = False
            self.mapping.drag = None
            self.mapping.bind_armed = False
        else:
            self.mode = "mapping"
            self.mapping.enabled = True
            # Detect a "pristine" mapping config (the default single
            # fullscreen blackout group with no real content) and start in
            # EDIT mode with an empty canvas so the operator can immediately
            # drag rectangles to define their spaces.
            pristine = (len(self.mapping.groups) == 1
                        and self.mapping.groups[0].content_kind == "blackout"
                        and self.mapping.groups[0].clip_stem is None
                        and self.mapping.groups[0].gen_name is None
                        and len(self.mapping.groups[0].spaces) == 1
                        and self.mapping.groups[0].spaces[0].corners
                            == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
            if pristine:
                # Empty out the default fullscreen space — the user is going
                # to draw their own. Keep the group itself so there's always
                # somewhere for the first dragged rectangle to land.
                self.mapping.groups[0].spaces = []
                self.mapping.edit_mode = True
        self._persist_mapping()
        pygame.mouse.set_visible(True)

    def toggle_edit_mode(self):
        if self.mode != "mapping":
            return
        self.mapping.toggle_edit_mode()
        self._persist_mapping()

    def mapping_arm_bind(self):
        self.mapping.arm_bind()

    def mapping_delete_selected_space(self):
        self.mapping.delete_selected_space()
        self._persist_mapping()

    def mapping_unbind_selected_space(self):
        self.mapping.unbind_selected_space()
        self._persist_mapping()

    def mapping_cancel_drag(self):
        self.mapping.cancel_drag()
        self.mapping.bind_armed = False
        self.mapping.deselect_space()

    def cycle_mapping_group(self, step=1):
        self.mapping.cycle_selected(step)
        self._persist_mapping()

    def mapping_add_group(self):
        self.mapping.add_group()
        self._persist_mapping()

    def mapping_remove_group(self):
        self.mapping.remove_selected_group()
        self._persist_mapping()

    def mapping_add_space(self):
        self.mapping.add_space_to_selected()
        self._persist_mapping()

    def mapping_remove_space(self):
        self.mapping.remove_space_from_selected()
        self._persist_mapping()

    def mapping_cycle_grid(self):
        self.mapping.cycle_grid_for_selected()
        self._persist_mapping()

    def mapping_toggle_autopilot(self):
        self.mapping.toggle_autopilot_selected()
        self._persist_mapping()

    def mapping_cycle_autopilot_kind(self):
        self.mapping.cycle_autopilot_kind()
        self._persist_mapping()

    def mapping_adjust_autopilot_interval(self, delta):
        self.mapping.adjust_autopilot_interval(delta)
        self._persist_mapping()

    def mapping_toggle_borders(self):
        self.mapping.toggle_borders()
        self._persist_mapping()

    def mapping_adjust_border_intensity(self, delta):
        self.mapping.adjust_border_intensity(delta)
        self._persist_mapping()

    def mapping_adjust_border_thickness(self, delta):
        self.mapping.adjust_border_thickness(delta)
        self._persist_mapping()

    def mapping_cycle_border_color(self):
        self.mapping.cycle_border_color()
        self._persist_mapping()

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
        elif self.mode == "mapping":
            frame = self._compose_mapping_frame()
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

    # ── Mapping render pipeline ───────────────────────────────────────

    def _compose_mapping_frame(self):
        """Render projection-mapping: source per group, warp into spaces.
        In edit mode also draw outlines of every group + a highlight on
        the picked-for-edit space + the create-drag rubber-band, so the
        projector itself helps the operator align spaces to physical
        surfaces while editing."""
        w, h = self.w, self.h
        canvas = np.zeros((h, w, 3), dtype=np.uint8)
        now = time.time()
        self.mapping.tick_autopilot(self, now)
        for group in self.mapping.groups:
            if not group.spaces:
                continue
            source = self._compose_group_source(group, now)
            for space in group.spaces:
                self._warp_into_canvas(canvas, source, space.corners_px(w, h))
        canvas = self._apply_hits(canvas)
        if self.mapping.show_borders:
            self._draw_selection_border(canvas)
        if self.mapping.edit_mode:
            self._draw_edit_overlay(canvas)
        return canvas

    def _compose_group_source(self, group, now):
        """Build the unwarped source frame for one group (clip / gen / FX)."""
        w, h = self.w, self.h
        if group.content_kind == "clip" and group.clip_stem:
            idx = self.clips.find_by_stem(group.clip_stem)
            if idx is not None:
                self.clips.ensure_open(idx)
                frame = self.clips.read_at(idx)
                if frame is None:
                    frame = np.zeros((h, w, 3), dtype=np.uint8)
            else:
                frame = np.zeros((h, w, 3), dtype=np.uint8)
        elif group.content_kind == "generative" and group.gen_name:
            fn = GENERATIVE_FNS.get(group.gen_name)
            if fn is None:
                frame = np.zeros((h, w, 3), dtype=np.uint8)
            else:
                ctx = EffectContext(
                    w, h,
                    now - self.start_time + group._time_offset,
                    (group.param_x, group.param_y),
                )
                frame = fn(ctx)
        else:
            frame = np.zeros((h, w, 3), dtype=np.uint8)

        # Per-group FX chain
        if any(group.fx_state.values()):
            ctx = EffectContext(w, h,
                                now - self.start_time + group._time_offset,
                                (group.param_x, group.param_y))
            s = group.fx_state
            if s.get("kaleido"):
                frame = kaleidoscope(frame, segments=int(3 + ctx.px * 9))
            if s.get("mirror"):
                frame = mirror_h(frame)
            if s.get("rgb_split"):
                frame = rgb_split(frame, offset=int(4 + ctx.px * 20))
            if s.get("posterize"):
                frame = posterize(frame, levels=int(2 + ctx.py * 6))
            if s.get("edges"):
                frame = edges(frame)
            if s.get("invert"):
                frame = invert(frame)
            if s.get("feedback"):
                # Mapping-mode feedback uses the previous full canvas as
                # the trail buffer. Skip if we don't have one yet.
                if self.prev_frame is not None:
                    zoom = 1.0 + ctx.px * 0.08
                    rot = (ctx.py - 0.5) * 4.0
                    frame = feedback_blend(self.prev_frame, frame,
                                           zoom=zoom, rotate=rot)

        # Per-group overlay screen-blend
        if group.overlay_stem:
            ov_idx = self.overlays.find_by_stem(group.overlay_stem)
            if ov_idx is not None:
                self.overlays.ensure_open(ov_idx)
                ov = self.overlays.read_at(ov_idx)
                if ov is not None:
                    frame = screen_blend(frame, ov)

        return frame

    def _warp_into_canvas(self, canvas, source, dst_corners):
        """Warp `source` into the quad `dst_corners` on `canvas`.

        Pitfall: cv2.warpPerspective on the full canvas is expensive. We
        crop output to the quad's bounding box and apply a convex-poly mask
        so pixels outside the quad on the canvas stay black (no leak onto
        the wall) and other groups' pixels aren't trampled.
        """
        h, w = canvas.shape[:2]
        sh, sw = source.shape[:2]
        src_corners = np.array(
            [[0, 0], [sw, 0], [sw, sh], [0, sh]], dtype=np.float32
        )
        M = cv2.getPerspectiveTransform(src_corners, dst_corners.astype(np.float32))

        x_min = max(0, int(np.floor(dst_corners[:, 0].min())))
        x_max = min(w, int(np.ceil(dst_corners[:, 0].max())))
        y_min = max(0, int(np.floor(dst_corners[:, 1].min())))
        y_max = min(h, int(np.ceil(dst_corners[:, 1].max())))
        if x_max - x_min < 2 or y_max - y_min < 2:
            return  # Degenerate quad, skip silently.

        warped = cv2.warpPerspective(
            source, M, (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        mask = np.zeros((h, w), dtype=np.uint8)
        poly = dst_corners.astype(np.int32)
        cv2.fillConvexPoly(mask, poly, 255)
        sub_mask = mask[y_min:y_max, x_min:x_max]
        sub_warp = warped[y_min:y_max, x_min:x_max]
        sub_canvas = canvas[y_min:y_max, x_min:x_max]
        idx = sub_mask > 0
        sub_canvas[idx] = sub_warp[idx]
        canvas[y_min:y_max, x_min:x_max] = sub_canvas

    def _draw_selection_border(self, canvas):
        """Outline only the currently-selected group's spaces — keeps the
        rest of the projection clean (no light artefacts on the wall)."""
        sel = self.mapping.selected_group()
        if sel is None:
            return
        color = self.mapping.border_color_eff()
        thickness = self.mapping.border_thickness
        w, h = self.w, self.h
        for space in sel.spaces:
            pts = space.corners_px(w, h).astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(canvas, [pts], True, color, thickness, cv2.LINE_AA)

    def _draw_edit_overlay(self, canvas):
        """Edit-mode UI drawn onto the actual projector output: outlines
        on every group (dim) + the picked space (bright) + the in-flight
        create-drag rubber-band rectangle. Lets the operator align spaces
        to real-world features without looking at the HUD."""
        w, h = self.w, self.h
        for gi, group in enumerate(self.mapping.groups):
            for si, space in enumerate(group.spaces):
                pts = space.corners_px(w, h).astype(np.int32).reshape(-1, 1, 2)
                is_picked = (self.mapping.selected_space == (gi, si))
                color = (255, 240, 120) if is_picked else (90, 110, 140)
                thickness = 3 if is_picked else 1
                cv2.polylines(canvas, [pts], True, color, thickness, cv2.LINE_AA)
                # Draw corner handles on the picked space so the operator
                # can see where to grab.
                if is_picked:
                    for cx, cy in space.corners_px(w, h).astype(np.int32):
                        cv2.circle(canvas, (int(cx), int(cy)), 6, (20, 20, 30), -1)
                        cv2.circle(canvas, (int(cx), int(cy)), 5, (255, 240, 120), -1)
        # Rubber-band rectangle while dragging-to-create a new space.
        drag = self.mapping.drag
        if drag is not None and drag.get("kind") == "create":
            sx, sy = drag["start"]
            cx, cy = drag["current"]
            x0, x1 = int(min(sx, cx) * w), int(max(sx, cx) * w)
            y0, y1 = int(min(sy, cy) * h), int(max(sy, cy) * h)
            if x1 - x0 >= 2 and y1 - y0 >= 2:
                cv2.rectangle(canvas, (x0, y0), (x1, y1), (200, 255, 200), 1)

    def blit_to_output(self, frame):
        surface = pygame.image.frombuffer(frame.tobytes(), (self.w, self.h), "RGB")
        target_size = self.screen.get_size()
        if target_size != (self.w, self.h):
            # smoothscale (bilinear) instead of scale (nearest-neighbour)
            # — looks dramatically less pixelated when the render res
            # doesn't match the display res. smoothscale only supports
            # 24/32-bit surfaces, hence the fallback.
            try:
                surface = pygame.transform.smoothscale(surface, target_size)
            except (ValueError, pygame.error):
                surface = pygame.transform.scale(surface, target_size)
        self.screen.blit(surface, (0, 0))
        pygame.display.flip()

    def render(self):
        """Convenience: compose + blit (used when there is no control window)."""
        frame = self.compose_frame()
        self.blit_to_output(frame)
        return frame

    def run(self, control=None):
        from keymap import dispatch, NAV_KEYS, FAV_KEYS, HIT_KEYS, fav_tap, fav_long
        # Enable system key-repeat. We filter below so only NAV_KEYS
        # auto-fire on hold — toggle/hit keys still need a fresh press.
        pygame.key.set_repeat(350, 80)
        held_keys = set()
        # Favourite-key timing: tap = on release (< threshold), long-press
        # = held past threshold without release.
        fav_pressed_at = {}      # key → initial-press timestamp
        long_press_fired = set() # keys whose long-press action has fired
        LONG_PRESS_S = 0.5

        arrow_keys = (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN)

        last_t = time.time()
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

                elif event.type == pygame.KEYDOWN:
                    is_initial = event.key not in held_keys
                    held_keys.add(event.key)

                    # Autopilot Enter handling — engage (double-tap when off)
                    # or disengage (single tap when on). Enter itself never
                    # falls through to any further action.
                    if is_initial and event.key == pygame.K_RETURN:
                        now_t = time.time()
                        if self.auto_mode:
                            self.disengage_auto()
                            self.last_enter_t = 0.0
                        elif now_t - self.last_enter_t < 0.6:
                            self.engage_auto()
                            self.last_enter_t = 0.0
                        else:
                            self.last_enter_t = now_t
                        continue

                    # Any non-arrow key during autopilot returns control to
                    # the operator AND still performs its action — perfect
                    # for "ooh, hit B for an RGB smash right now".
                    if is_initial and self.auto_mode and event.key not in arrow_keys:
                        self.disengage_auto()

                    # In mapping/edit mode the operator is laying out spaces,
                    # not jamming — swallow the favourite long-press timing
                    # so taps on 1-0 / Q-P don't fire content actions.
                    editing = (self.mode == "mapping"
                               and self.mapping.edit_mode)
                    if event.key in FAV_KEYS and not editing:
                        if is_initial:
                            fav_pressed_at[event.key] = time.time()
                            long_press_fired.discard(event.key)
                        # Ignore auto-repeats for favourite keys.
                    elif is_initial or event.key in NAV_KEYS:
                        dispatch(self, event.key, event.mod)

                elif event.type == pygame.KEYUP:
                    if event.key in fav_pressed_at:
                        elapsed = time.time() - fav_pressed_at.pop(event.key)
                        if (event.key not in long_press_fired
                                and elapsed < LONG_PRESS_S
                                and not (self.mode == "mapping"
                                         and self.mapping.edit_mode)):
                            fav_tap(self, event.key)
                        long_press_fired.discard(event.key)
                    held_keys.discard(event.key)

                elif event.type == pygame.WINDOWFOCUSLOST:
                    held_keys.clear()
                    fav_pressed_at.clear()
                    long_press_fired.clear()

                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP,
                                    pygame.MOUSEMOTION):
                    if control is not None:
                        control.handle_event(event)

            # Per-frame long-press detection
            now = time.time()
            for k, t in list(fav_pressed_at.items()):
                if k in long_press_fired:
                    continue
                if now - t >= LONG_PRESS_S:
                    fav_long(self, k)
                    long_press_fired.add(k)

            dt = now - last_t
            last_t = now
            self.update_auto(now)
            self.update_params_from_keys(dt)
            self.update_held_hits(HIT_KEYS)
            frame = self.compose_frame()
            self.blit_to_output(frame)
            if control is not None:
                control.render(frame)
            self._flush_mapping_persist()
            self.clock.tick(self.cfg.fps)
        self._flush_mapping_persist()
        self.clips.release_all()
        self.overlays.release_all()
