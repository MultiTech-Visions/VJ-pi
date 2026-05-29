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
from gpu_generators import GpuGeneratorBridge
from shader_catalog import GPU_GENERATOR_ORDER


GENERATIVES = list(GPU_GENERATOR_ORDER)

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
    "invert", "posterize", "edges", "rgb_split", "melt",
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
        self.current_generator_idx = 0
        self._generator_activation_token = 0
        self.fx_state = {fx: False for fx in FX_TOGGLES}
        self.hit_type = None
        self.hit_frames_left = 0
        self.blackout = False
        self.freeze = False
        self.frozen_frame = None
        self.prev_frame = None

        # Melt FX: a generator's colour field warps the base layer per-pixel
        # so a clip ripples and liquefies along the pattern. The single GPU
        # worker holds one generator at a time, so when the base is itself a
        # generator we reuse that frame as the field rather than asking the
        # worker for a second one (which would thrash its pipeline rebuild).
        self.melt_source = "kaliset"
        self._base_was_generator = False
        self._last_gen_frame = None
        self._melt_grid_cache = {}

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

        # Favourite slots. Clips are saved by filename stem; generators
        # are saved by catalogue name.
        persisted = load_state()
        self.clip_favorites = _coerce_favs(persisted.get("clip_favorites"))
        self.overlay_favorites = _coerce_favs(persisted.get("overlay_favorites"))
        self.generator_favorites = _coerce_favs(
            persisted.get("generator_favorites")
        )
        if all(v is None for v in self.generator_favorites):
            self.generator_favorites = (
                list(GENERATIVES[:FAV_SLOTS]) + [None] * FAV_SLOTS
            )[:FAV_SLOTS]
        self.gpu_generators = GpuGeneratorBridge()

        # Projection-mapping mode. When enabled, the render pipeline draws
        # each group's content into its spaces' quads on a black canvas;
        # most live-action keys (clip / gen / FX / params / favourites)
        # route to the selected group instead of global state.
        self.mapping = MappingManager(persisted.get("mapping"))
        self._repair_mapping_media_refs()
        self.mode = "mapping" if self.mapping.enabled else "live"
        # Hide the cursor only in clean live fullscreen — in mapping mode
        # the operator needs to drag corners around in the HUD preview.
        self._mapping_persist_dirty = False
        # Per-group mask cache. Key = id(group); value = (signature, mask,
        # bbox). Invalidated when any of the group's space corners change.
        # Lets a static layout skip cv2.fillConvexPoly every frame — the
        # common case during a running set.
        self._group_mask_cache = {}

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

    def _repair_mapping_media_refs(self):
        """Retarget stale mapping media names after the clip library changes.

        Mapping state stores clip filename stems. If the library is rebuilt,
        old stems can point at files that no longer exist; without repair the
        group has valid geometry but a black source frame. Use the first
        current clip as the deterministic fallback so `-/=` can keep cycling
        from a real library position.
        """
        dirty = False
        fallback_clip = self.clips.name(0) if len(self.clips) else None
        for group in self.mapping.groups:
            if (group.content_kind == "clip"
                    and group.clip_stem
                    and self.clips.find_by_stem(group.clip_stem) is None):
                print(f"[vj] mapping: clip '{group.clip_stem}' not found")
                if fallback_clip is not None:
                    print(f"[vj] mapping: retargeting {group.name} to {fallback_clip}")
                    group.clip_stem = fallback_clip
                    self.clips.ensure_open(0)
                else:
                    group.clip_stem = None
                    group.content_kind = "blackout"
                dirty = True
            if (group.content_kind == "generative"
                    and group.gen_name
                    and group.gen_name not in GENERATIVES):
                print(f"[vj] mapping: generator '{group.gen_name}' not found")
                group.gen_name = GENERATIVES[0] if GENERATIVES else None
                if group.gen_name is None:
                    group.content_kind = "blackout"
                dirty = True
        if dirty:
            state = load_state()
            state["mapping"] = self.mapping.to_dict()
            save_state(state)

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
        print(f"[vj] mapping: {g.name} {which} → {pool.name(new_idx)}")
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
        self.current_generator_idx = idx
        self._generator_activation_token += 1
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

    def browse_generatives(self, step):
        if not GENERATIVES:
            return
        if self.active_generative in GENERATIVES:
            idx = GENERATIVES.index(self.active_generative)
        else:
            idx = self.current_generator_idx
        self.select_generative((idx + step) % len(GENERATIVES))

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
        """Panic key: clear FX, hits, overlay, blackout, freeze — but keep
        the current base layer (clip OR generator) playing so the output
        doesn't suddenly drop to black mid-set. Use `0` (long-press while
        playing nothing) or the `-` / `=` cycle keys to change/clear the
        clip, and `[` / `]` to change/clear the generator.

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
        # NOTE: deliberately do NOT clear self.active_generative — Esc keeps
        # the generator playing, same as it keeps a clip. Use [ / ] to change
        # or clear the generator.

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

    def play_generator_favorite(self, slot):
        if not 0 <= slot < len(self.generator_favorites):
            return
        name = self.generator_favorites[slot]
        if name not in GENERATIVES:
            return
        self.select_generative(GENERATIVES.index(name))

    def save_generator_favorite(self, slot):
        if not 0 <= slot < len(self.generator_favorites):
            return
        if self._in_mapping():
            g = self.mapping.selected_group()
            self.generator_favorites[slot] = (
                g.gen_name if g.content_kind == "generative" else None
            )
        else:
            self.generator_favorites[slot] = self.active_generative
        self._persist_favorites()

    def _persist_favorites(self):
        state = load_state()
        state["clip_favorites"] = self.clip_favorites
        state["overlay_favorites"] = self.overlay_favorites
        state["generator_favorites"] = self.generator_favorites
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
                self.current_generator_idx = random.randrange(len(GENERATIVES))
                self.active_generative = GENERATIVES[self.current_generator_idx]
                self._generator_activation_token += 1
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
            elif all(len(g.spaces) == 0 for g in self.mapping.groups):
                # Nothing to perform against — drop straight into EDIT so
                # the operator can immediately start drawing.
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

    def _handle_output_mouse_event(self, event):
        """Mouse input that landed on the OUTPUT (projector) window.

        Only meaningful in mapping/edit mode — it's the natural way to
        do projection mapping, pointing at physical features through the
        projection itself. Coords are screen-pixel; we normalize against
        the output window size since fullscreen rescales away from
        cfg.width/height."""
        if not (self.mode == "mapping" and self.mapping.edit_mode):
            return
        sw, sh = self.screen.get_size()
        m = self.mapping

        if event.type == pygame.MOUSEMOTION:
            pos = event.pos
            norm = (max(0.0, min(1.0, pos[0] / max(1, sw))),
                    max(0.0, min(1.0, pos[1] / max(1, sh))))
            if m.drag is not None:
                m.update_drag(norm)
            else:
                m.update_hover(norm)
            return

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if m.drag is not None:
                m.end_drag()
                self._persist_mapping()
            return

        if event.type != pygame.MOUSEBUTTONDOWN or event.button != 1:
            return
        pos = event.pos
        norm = (max(0.0, min(1.0, pos[0] / max(1, sw))),
                max(0.0, min(1.0, pos[1] / max(1, sh))))
        self._mapping_handle_click(norm)

    def _mapping_handle_click(self, norm):
        """Shared edit-mode click priority for both projector and HUD
        clicks: hover-toolbar button > shift-bind > corner > body > empty
        area → create."""
        m = self.mapping
        shift_held = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)

        # 1. Hover toolbar (× delete, + bind, ⊘ unbind, G group tag).
        btn = m.hit_test_hover_button(norm)
        if btn is not None:
            kind, gi, si = btn
            if kind == "delete":
                m.select_space(gi, si)
                m.delete_selected_space()
                self._persist_mapping()
            elif kind == "bind":
                m.bind_to_selected(gi, si)
                self._persist_mapping()
            elif kind == "unbind":
                m.select_space(gi, si)
                m.unbind_selected_space()
                self._persist_mapping()
            elif kind == "group":
                # Tap the group chip to make this space the picked one.
                m.select_space(gi, si)
                self._persist_mapping()
            return

        # 2. Keyboard-fallback Shift+click bind / bind-armed.
        if (shift_held or m.bind_armed) and m.selected_space is not None:
            hit = m.hit_test_space(norm)
            if hit is not None and hit != m.selected_space:
                if hit[0] != m.selected_space[0]:
                    m.bind_to_selected(*hit)
                    self._persist_mapping()
                else:
                    m.bind_armed = False
                return
            m.bind_armed = False

        # 3. Corner handle of the picked space.
        radius_norm = 0.025
        corner = m.hit_test_corner_of_selected_space(norm, radius_norm)
        if corner is not None:
            m.start_corner_drag(corner)
            return

        # 4. Click on any space's body → select + start move drag.
        hit = m.hit_test_space(norm)
        if hit is not None:
            m.select_space(*hit)
            m.start_move(*hit, norm)
            self._persist_mapping()
            return

        # 5. Empty area → drag a brand-new rectangle into existence.
        m.start_create(norm)

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

    # ── Frame controls (per-group zoom / pan / fit mode) ─────────────

    def mapping_cycle_fit_mode(self, step=1):
        self.mapping.cycle_fit_mode(step)
        self._persist_mapping()

    def mapping_adjust_zoom(self, factor):
        self.mapping.adjust_zoom(factor)
        self._persist_mapping()

    def mapping_adjust_pan(self, dx, dy):
        self.mapping.adjust_pan(dx, dy)
        self._persist_mapping()

    def mapping_reset_frame(self):
        self.mapping.reset_frame()
        self._persist_mapping()

    # ── Render pipeline ───────────────────────────────────────────────

    def _build_base(self, ctx):
        """Live-mode base layer. Clips read at canvas resolution; generatives
        render at the reduced internal resolution and get upscaled in
        compose_frame() before overlay/hits paint on top."""
        clip_frame = self.clips.read()
        if clip_frame is not None:
            self._base_was_generator = False
            return clip_frame
        if self.active_generative is not None:
            gw, gh = self._gen_render_size()
            frame = self._render_generative(
                self.active_generative, gw, gh, ctx.t, (ctx.px, ctx.py)
            )
            if frame is not None:
                self._base_was_generator = True
                self._last_gen_frame = frame
                return frame
        self._base_was_generator = False
        return np.zeros((self.h, self.w, 3), dtype=np.uint8)

    def _render_generative(self, name, width, height, t, params):
        token = self._generator_activation_token if name == "donut" else 0
        frame = self.gpu_generators.render(name, width, height, token=token)
        if frame is not None:
            return frame
        fn = GENERATIVE_FNS.get(name)
        if fn is None:
            return None
        return fn(EffectContext(width, height, t, params))

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

    def _melt_grid(self, w, h):
        g = self._melt_grid_cache.get((w, h))
        if g is None:
            yy, xx = np.indices((h, w), dtype=np.float32)
            g = (yy, xx)
            self._melt_grid_cache[(w, h)] = g
        return g

    def _apply_melt(self, frame, ctx):
        """Warp `frame` per-pixel using a generator's colour as a flow field.

        When the base is a clip we render the melt-source generator (the
        kaliset by default); when the base is already a generator we
        reuse its frame so the single GPU worker isn't asked for a second
        pattern. param_x dials the amount, from a shimmer to a full liquefy.
        """
        if self._base_was_generator and self._last_gen_frame is not None:
            field = self._last_gen_frame
        else:
            dw, dh = self._gen_render_size()
            field = self.gpu_generators.render(self.melt_source, dw, dh)
        if field is None:
            return frame
        h, w = frame.shape[:2]
        if field.shape[:2] != (h, w):
            field = cv2.resize(field, (w, h), interpolation=cv2.INTER_LINEAR)
        amp = 1.5 + ctx.px * 86.5
        off_x = (field[:, :, 0].astype(np.float32) / 255.0 - 0.5) * 2.0 * amp
        off_y = (field[:, :, 1].astype(np.float32) / 255.0 - 0.5) * 2.0 * amp
        yy, xx = self._melt_grid(w, h)
        return cv2.remap(frame, xx + off_x, yy + off_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REFLECT)

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
            # _build_base may return a sub-canvas frame (generatives render
            # at cfg.gen_render_scale). Upscale to canvas before overlay
            # and hits — those expect canvas-sized buffers.
            if frame.shape[:2] != (self.h, self.w):
                frame = cv2.resize(frame, (self.w, self.h),
                                   interpolation=cv2.INTER_LINEAR)
            if self.fx_state.get("melt"):
                frame = self._apply_melt(frame, ctx)
            frame = self._apply_overlay(frame)
            frame = self._apply_hits(frame)

        if not self.freeze:
            self.prev_frame = frame
        return frame

    # ── Mapping render pipeline ───────────────────────────────────────

    def _compose_mapping_frame(self):
        """Render projection-mapping: one source per group, painted onto
        the canvas under a mask built from the UNION of the group's
        spaces. Multiple spaces in one group act as multiple windows
        into the same video — they reveal different parts of one
        playing video, not separate copies of it.

        In edit mode also draw outlines of every group + a highlight on
        the picked-for-edit space + the create-drag rubber-band, so the
        projector itself helps the operator align spaces to physical
        surfaces while editing.
        """
        w, h = self.w, self.h
        canvas = np.zeros((h, w, 3), dtype=np.uint8)
        now = time.time()
        self.mapping.tick_autopilot(self, now)
        # Sweep stale mask cache entries every ~5 s. Cheap, and keeps
        # the cache tidy across hours of editing.
        if now - getattr(self, "_mask_cache_gc_at", 0.0) > 5.0:
            self._invalidate_mask_cache()
            self._mask_cache_gc_at = now
        for group in self.mapping.groups:
            # Skip groups with no spaces (no mask) AND skip groups
            # whose content is blackout (nothing to compose). Saves a
            # full-resolution generative call per blackout group.
            if not group.spaces:
                continue
            if (group.content_kind == "blackout"
                    or (group.content_kind == "clip" and not group.clip_stem)
                    or (group.content_kind == "generative" and not group.gen_name)):
                continue
            source = self._compose_group_source(group, now)
            self._place_group_into_canvas(canvas, source, group)
        canvas = self._apply_hits(canvas)
        if self.mapping.show_borders:
            self._draw_selection_border(canvas)
        if self.mapping.edit_mode:
            self._draw_edit_overlay(canvas)
        return canvas

    def _compose_group_source(self, group, now):
        """Build the unwarped source frame for one group (clip / gen / FX).

        Generative sources render at cfg.gen_render_scale × canvas (default
        0.5) — they're smooth procedural patterns, no detail lost from
        upscaling, but 4× fewer pixels under the per-frame sin/sqrt/etc.
        Clips stay at canvas resolution since they carry real detail.
        FX runs at whatever size the base was rendered at (kaleido / mirror
        / etc. adapt via frame.shape internally).
        """
        w, h = self.w, self.h
        is_clip = (group.content_kind == "clip" and group.clip_stem)
        if is_clip:
            idx = self.clips.find_by_stem(group.clip_stem)
            if idx is None and len(self.clips) > 0:
                idx = 0
                group.clip_stem = self.clips.name(idx)
                self._persist_mapping()
            if idx is None:
                frame = np.zeros((h, w, 3), dtype=np.uint8)
            else:
                self.clips.ensure_open(idx)
                frame = self.clips.read_at(idx)
                if frame is None:
                    frame = np.zeros((h, w, 3), dtype=np.uint8)
        elif group.content_kind == "generative" and group.gen_name:
            gw, gh = self._gen_render_size()
            frame = self._render_generative(
                group.gen_name,
                gw,
                gh,
                now - self.start_time + group._time_offset,
                (group.param_x, group.param_y),
            )
            if frame is None:
                frame = np.zeros((h, w, 3), dtype=np.uint8)
        else:
            frame = np.zeros((h, w, 3), dtype=np.uint8)

        fh, fw = frame.shape[:2]

        # Per-group FX chain. Runs at whatever size the base is — kaleido
        # uses frame.shape; mirror, posterize, rgb_split, edges, invert
        # all are shape-agnostic.
        if any(group.fx_state.values()):
            ctx = EffectContext(fw, fh,
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
                # the trail buffer. Skip if we don't have one yet OR if
                # dimensions don't match (avoids a feedback artefact on
                # a low-res generative pulling from the canvas-sized
                # prev_frame; the warp would inflate a thumbnail).
                if (self.prev_frame is not None
                        and self.prev_frame.shape[:2] == frame.shape[:2]):
                    zoom = 1.0 + ctx.px * 0.08
                    rot = (ctx.py - 0.5) * 4.0
                    frame = feedback_blend(self.prev_frame, frame,
                                           zoom=zoom, rotate=rot)

        # Per-group overlay screen-blend. Overlay pool reads at canvas
        # size; resize to match the source if the source was rendered
        # smaller (cheap — one resize at fw×fh).
        if group.overlay_stem:
            ov_idx = self.overlays.find_by_stem(group.overlay_stem)
            if ov_idx is not None:
                self.overlays.ensure_open(ov_idx)
                ov = self.overlays.read_at(ov_idx)
                if ov is not None:
                    if ov.shape[:2] != (fh, fw):
                        ov = cv2.resize(ov, (fw, fh),
                                        interpolation=cv2.INTER_AREA)
                    frame = screen_blend(frame, ov)

        return frame

    def _gen_render_size(self):
        """Internal generative resolution = cfg.width/height × scale,
        floored at 64×36 so we never burn cycles below the cv2 minimum
        practical block size."""
        scale = max(0.1, min(1.0, getattr(self.cfg, "gen_render_scale", 1.0)))
        gw = max(64, int(self.w * scale))
        gh = max(36, int(self.h * scale))
        return gw, gh

    def _place_group_into_canvas(self, canvas, source, group):
        """Dispatch into the right placement strategy based on the group's
        fit_mode. "stretch" is per-space (each quad warps its own copy of
        the video — legacy billboard look). The other three modes are
        per-GROUP: the video plays once across the whole canvas at the
        chosen zoom / pan, and the union of the group's space polygons
        is the mask through which it shows. Multiple spaces in one
        group → multiple windows onto one playing video."""
        w, h = self.w, self.h
        if group.fit_mode == "stretch":
            for space in group.spaces:
                self._warp_into_canvas(canvas, source,
                                       space.corners_px(w, h))
            return
        self._window_group_into_canvas(canvas, source, group)

    def _warp_into_canvas(self, canvas, source, dst_corners):
        """Warp `source` into the quad `dst_corners` on `canvas` — the
        old "stretch to fit the quad" behaviour, kept as an opt-in
        fit_mode so the operator can deliberately get the perspective-
        billboard look (good for projecting onto an angled flat surface).

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

    def _window_group_into_canvas(self, canvas, source, group):
        """Place `source` ONCE across the canvas at the group's chosen
        zoom / pan, then reveal it only through the union of the group's
        space polygons. The video keeps its natural aspect (no warp);
        each space is a hole onto a single underlying video plane, so
        two spaces side-by-side in one group show the video continuously
        across both — different parts of the same video, in sync.

        fit_mode = "fit"   : uniform scale so the source fits the
                             canvas (letterboxed). Zoom / pan ignored.
        fit_mode = "fill"  : uniform scale so the source covers the
                             canvas (cropped). Zoom / pan ignored.
        fit_mode = "window": "fit" base scale times group.zoom, plus
                             pan offset (-1..+1 of half-canvas).
        """
        h, w = canvas.shape[:2]
        sh, sw = source.shape[:2]
        if sw < 2 or sh < 2:
            return

        mode = group.fit_mode
        if mode == "fill":
            scale = max(w / sw, h / sh)
            zoom, pan_x, pan_y = 1.0, 0.0, 0.0
        else:  # "window" or "fit"
            scale = min(w / sw, h / sh)
            if mode == "window":
                zoom, pan_x, pan_y = group.zoom, group.pan_x, group.pan_y
            else:
                zoom, pan_x, pan_y = 1.0, 0.0, 0.0
        scale *= max(0.05, zoom)

        dw = max(2, int(round(sw * scale)))
        dh = max(2, int(round(sh * scale)))
        cx = w * 0.5 + pan_x * w * 0.5
        cy = h * 0.5 + pan_y * h * 0.5
        dx = int(round(cx - dw * 0.5))
        dy = int(round(cy - dh * 0.5))

        # Clip the video's destination rect to the canvas.
        vx0, vy0 = max(0, dx), max(0, dy)
        vx1, vy1 = min(w, dx + dw), min(h, dy + dh)
        if vx1 <= vx0 or vy1 <= vy0:
            return  # Video positioned entirely off-canvas.

        # Resolve the group mask + its bounding rect (cached per-group;
        # only rebuilt when corners change, so a static layout pays the
        # cv2.fillConvexPoly cost ONCE, not every frame).
        mask, mbox = self._group_mask(group)
        mx0, my0, mx1, my1 = mbox

        # Intersection of "where the video lands" and "where the mask
        # has any pixels" — that's the only region we need to touch.
        x0, y0 = max(vx0, mx0), max(vy0, my0)
        x1, y1 = min(vx1, mx1), min(vy1, my1)
        if x1 <= x0 or y1 <= y0:
            return  # Mask and video don't overlap.

        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized = cv2.resize(source, (dw, dh), interpolation=interp)

        sx0, sy0 = x0 - dx, y0 - dy
        sx1, sy1 = sx0 + (x1 - x0), sy0 + (y1 - y0)
        # cv2.copyTo writes `src` into `dst` where `mask` != 0 in pure
        # C, GIL released — significantly faster than the previous
        # `dst[mask>0] = src[mask>0]` boolean-fancy-index path which
        # builds a full-canvas-sized intermediate every call.
        cv2.copyTo(resized[sy0:sy1, sx0:sx1],
                   mask[y0:y1, x0:x1],
                   canvas[y0:y1, x0:x1])

    def _group_mask(self, group):
        """Return (mask, (x0, y0, x1, y1)) for `group`. Cached by space
        corners — only rebuilt when the operator edits the layout, so a
        static set pays the cv2.fillConvexPoly cost ONCE."""
        signature = tuple(
            (round(c[0], 6), round(c[1], 6))
            for space in group.spaces for c in space.corners
        )
        key = id(group)
        cached = self._group_mask_cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2]
        h, w = self.h, self.w
        mask = np.zeros((h, w), dtype=np.uint8)
        mx0, my0 = w, h
        mx1, my1 = 0, 0
        for space in group.spaces:
            poly = space.corners_px(w, h).astype(np.int32)
            cv2.fillConvexPoly(mask, poly, 255)
            mx0 = min(mx0, int(poly[:, 0].min()))
            my0 = min(my0, int(poly[:, 1].min()))
            mx1 = max(mx1, int(poly[:, 0].max()) + 1)
            my1 = max(my1, int(poly[:, 1].max()) + 1)
        mx0 = max(0, mx0); my0 = max(0, my0)
        mx1 = min(w, mx1); my1 = min(h, my1)
        bbox = (mx0, my0, mx1, my1)
        self._group_mask_cache[key] = (signature, mask, bbox)
        return mask, bbox

    def _invalidate_mask_cache(self):
        """Drop cached masks for groups that no longer exist (so the
        cache doesn't grow unboundedly through a session of editing).
        Called sporadically — leaks of a few hundred bytes per deleted
        group are not worth chasing every frame."""
        live = {id(g) for g in self.mapping.groups}
        for k in list(self._group_mask_cache):
            if k not in live:
                del self._group_mask_cache[k]

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
        on every group (dim) + the picked space (bright) + corner handles
        + hover toolbars + the in-flight create-drag rectangle. Lets the
        operator align spaces to real-world features and operate the
        editor without looking at the HUD."""
        w, h = self.w, self.h
        m = self.mapping
        for gi, group in enumerate(m.groups):
            for si, space in enumerate(group.spaces):
                pts = space.corners_px(w, h).astype(np.int32).reshape(-1, 1, 2)
                is_picked = (m.selected_space == (gi, si))
                color = (255, 240, 120) if is_picked else (90, 110, 140)
                thickness = 3 if is_picked else 1
                cv2.polylines(canvas, [pts], True, color, thickness, cv2.LINE_AA)
                # Draw corner handles on the picked space so the operator
                # can see where to grab.
                if is_picked:
                    for cx, cy in space.corners_px(w, h).astype(np.int32):
                        cv2.circle(canvas, (int(cx), int(cy)), 6, (20, 20, 30), -1)
                        cv2.circle(canvas, (int(cx), int(cy)), 5, (255, 240, 120), -1)

        # Hover toolbars — selected always; hovered too if different.
        for cand in {m.selected_space, m.hovered_space} - {None}:
            self._draw_hover_toolbar(canvas, *cand)

        # Rubber-band rectangle while dragging-to-create a new space.
        drag = m.drag
        if drag is not None and drag.get("kind") == "create":
            sx, sy = drag["start"]
            cx, cy = drag["current"]
            x0, x1 = int(min(sx, cx) * w), int(max(sx, cx) * w)
            y0, y1 = int(min(sy, cy) * h), int(max(sy, cy) * h)
            if x1 - x0 >= 2 and y1 - y0 >= 2:
                cv2.rectangle(canvas, (x0, y0), (x1, y1), (200, 255, 200), 1)

    def _draw_hover_toolbar(self, canvas, gi, si):
        """Render the per-space hover toolbar on the projector output."""
        w, h = self.w, self.h
        for kind, (nx, ny, nw, nh) in self.mapping.hover_toolbar_buttons(gi, si):
            x0, y0 = int(nx * w), int(ny * h)
            x1, y1 = int((nx + nw) * w), int((ny + nh) * h)
            cv2.rectangle(canvas, (x0, y0), (x1, y1), (28, 30, 40), -1)
            cv2.rectangle(canvas, (x0, y0), (x1, y1), (200, 210, 230), 1)
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            r = max(2, min(x1 - x0, y1 - y0) // 4)
            if kind == "delete":
                col = (90, 90, 255)
                cv2.line(canvas, (cx - r, cy - r), (cx + r, cy + r), col, 2, cv2.LINE_AA)
                cv2.line(canvas, (cx + r, cy - r), (cx - r, cy + r), col, 2, cv2.LINE_AA)
            elif kind == "bind":
                col = (140, 230, 140)
                cv2.line(canvas, (cx - r, cy), (cx + r, cy), col, 2, cv2.LINE_AA)
                cv2.line(canvas, (cx, cy - r), (cx, cy + r), col, 2, cv2.LINE_AA)
            elif kind == "unbind":
                col = (255, 180, 80)
                cv2.line(canvas, (cx - r, cy + r), (cx + r, cy - r), col, 2, cv2.LINE_AA)
                cv2.circle(canvas, (cx, cy), 2, (28, 30, 40), -1)
            elif kind == "group":
                label = f"G{gi + 1}"
                # Size text relative to button height so it stays legible
                # whether the projector is 720p or the HUD preview is small.
                scale = max(0.3, (y1 - y0) / 40.0)
                font = cv2.FONT_HERSHEY_SIMPLEX
                (tw, th), _ = cv2.getTextSize(label, font, scale, 1)
                tx = cx - tw // 2
                ty = cy + th // 2
                cv2.putText(canvas, label, (tx, ty), font, scale,
                            (220, 230, 250), 1, cv2.LINE_AA)

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
                    # In MAPPING mode E is the EDIT-mode toggle — route it
                    # straight to dispatch so any favourite handler doesn't
                    # eat the keystroke before the mapping dispatcher sees it.
                    is_mapping_e = (self.mode == "mapping"
                                    and event.key == pygame.K_e
                                    and not (event.mod & pygame.KMOD_CTRL))
                    if (event.key in FAV_KEYS and not editing
                            and not (event.mod & pygame.KMOD_CTRL)
                            and not is_mapping_e):
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
                    # SDL2 puts the source window on each event. If it's
                    # the HUD's window, let the control panel handle it
                    # (preview drags, display picker buttons). Otherwise
                    # it's a click on the projector itself — in mapping/
                    # edit mode that's how the operator paints spaces
                    # directly onto the projection surface.
                    win = getattr(event, "window", None)
                    hud_id = (control._window_id if control is not None
                              else None)
                    ev_win_id = (getattr(win, "id", None)
                                 if win is not None else None)
                    is_hud_event = (hud_id is not None
                                    and ev_win_id == hud_id)
                    if is_hud_event and control is not None:
                        control.handle_event(event)
                    else:
                        self._handle_output_mouse_event(event)

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
        self.gpu_generators.shutdown()
