import math
import os
import random
import time

import pygame
import numpy as np
import cv2

from clips import ClipPool
from lights import LightingRig
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
        # Lights mode — virtual front-of-house lighting rig. Mutually
        # exclusive with mapping (a session is either projecting clips
        # into spaces OR running a fake light rig, not both at once).
        self.lights = LightingRig(persisted.get("lights"))
        if self.mapping.enabled:
            self.mode = "mapping"
            # If a stale state file had both flags set, lights loses.
            self.lights.enabled = False
        elif self.lights.enabled:
            self.mode = "lights"
        else:
            self.mode = "live"
        # Hide the cursor only in clean live fullscreen — in mapping mode
        # the operator needs to drag corners around in the HUD preview.
        self._mapping_persist_dirty = False
        self._lights_persist_dirty = False
        # Wall-clock of the last chase tick — bootstrapped on mode entry
        # so the first frame's `dt` doesn't catapult chase_phase forward.
        self._lights_last_t = 0.0

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

    def _persist_lights(self):
        """Same deal as _persist_mapping — coalesce disk writes to one per
        frame so a held arrow key doesn't pound the SD card."""
        self._lights_persist_dirty = True

    def _flush_lights_persist(self):
        if not self._lights_persist_dirty:
            return
        self._lights_persist_dirty = False
        state = load_state()
        state["lights"] = self.lights.to_dict()
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

        In lights mode we stop the SELECTED group's chase but keep the
        rig layout intact — cues stay saved, fixtures stay placed.
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
        if self.mode == "lights":
            g = self.lights.selected_group()
            if g is not None and g.chase_kind != "off":
                g.chase_kind = "off"
                g.chase_phase = 0.0
                self._persist_lights()
            for k in self.fx_state:
                self.fx_state[k] = False
            self.overlays.deselect()
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
        """Arrows: tune PARAM X/Y in manual mode, tune auto rates in autopilot,
        haze + group-master in lights/perform.

        In mapping/edit and lights/edit, arrows are ignored entirely — the
        operator's laying out spaces / fixtures, not tweaking visuals.
        """
        if self.mode == "mapping" and self.mapping.edit_mode:
            return
        if self.mode == "lights" and self.lights.edit_mode:
            return
        keys = pygame.key.get_pressed()
        if self.mode == "lights":
            # Lights/perform: ←→ adjusts haze, ↑↓ adjusts the selected
            # group's master dimmer. Both run continuously while held.
            changed = False
            if keys[pygame.K_LEFT]:
                self.lights.adjust_haze(-dt * 0.6); changed = True
            if keys[pygame.K_RIGHT]:
                self.lights.adjust_haze(dt * 0.6); changed = True
            if keys[pygame.K_UP]:
                self.lights.adjust_group_master(dt * 0.6); changed = True
            if keys[pygame.K_DOWN]:
                self.lights.adjust_group_master(-dt * 0.6); changed = True
            if changed:
                self._persist_lights()
            return
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
            # Mapping and lights are mutually exclusive — pressing M from
            # lights mode drops the rig and enters mapping cleanly.
            if self.mode == "lights":
                self.lights.enabled = False
                self.lights.edit_mode = False
                self.lights.drag = None
                self.lights.palette_kind = None
                self.lights.selected_fixture = None
                self._persist_lights()
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
        elif self.mode == "lights":
            frame = self._compose_lights_frame()
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

    # ── Lights render pipeline ────────────────────────────────────────

    def _compose_lights_frame(self):
        """Render the virtual front-of-house lighting rig.

        Pipeline per frame:
          1. Tick every group's chase phase by wall-clock dt (BPM-synced).
          2. For each fixture, accumulate its volumetric light into a
             float32 canvas (additive blend — light is light, it sums).
          3. Apply the global FX chain (kaleidoscope on a beam fan is wild).
          4. Composite the global overlay (sparks on top of cones — yes).
          5. In EDIT mode, draw fixture mechanism icons + the picked-fixture
             handle on top so the operator can see what they're aiming.
          6. Apply punch-in hits (strobe/flash global).
        """
        w, h = self.w, self.h
        canvas = np.zeros((h, w, 3), dtype=np.float32)
        now = time.time()
        t = now - self.start_time

        # Bootstrap chase dt on first tick or after a mode switch (avoids
        # a giant phase jump when entering lights mode).
        if self._lights_last_t <= 0.0:
            dt = 0.0
        else:
            dt = max(0.0, min(0.5, now - self._lights_last_t))
        self._lights_last_t = now
        self.lights.tick_chases(dt)

        haze = self.lights.haze
        for group in self.lights.groups:
            n = max(1, len(group.fixtures))
            for fi, fx in enumerate(group.fixtures):
                intensity, pan, on, color = self.lights.effective_fixture(
                    group, fx, fi, n, t
                )
                if not on or intensity <= 0.001:
                    continue
                if fx.kind == "spot":
                    self._draw_spot_cone(canvas, fx, intensity, pan, color, haze)
                elif fx.kind == "par":
                    self._draw_par_splash(canvas, fx, intensity, color, haze)
                elif fx.kind == "strobe":
                    self._draw_strobe_flash(canvas, fx, intensity, color)

        frame = np.clip(canvas, 0, 255).astype(np.uint8)

        # Reuse the live-mode FX chain on top of the rig output — feedback,
        # kaleido, mirror, etc. all stack nicely on top of beams. Uses the
        # global self.fx_state since lights mode doesn't have per-group FX.
        if any(self.fx_state.values()):
            ctx = EffectContext(self.w, self.h, t,
                                (self.param_x, self.param_y))
            frame = self._apply_fx(frame, ctx)

        # Global overlay (e.g. sparks pre-keyed to black) — same screen-blend
        # path as live mode.
        frame = self._apply_overlay(frame)

        # Edit-mode chrome lives ABOVE the FX/overlay so the operator can
        # always see fixture positions even when feedback is in play.
        if self.lights.edit_mode:
            self._draw_fixture_chrome(frame)
            self._draw_lights_edit_overlay(frame)

        frame = self._apply_hits(frame)
        return frame

    def _draw_spot_cone(self, canvas, fx, intensity, pan, color, haze):
        """Additive-blend a triangular cone (apex at fixture, base at the
        beam tip) onto `canvas`. We render into a bounding-box sub-array
        instead of the full frame — cheap enough that 8-12 spots stay
        under our per-frame budget on Pi 5.

        `haze` (0..1) modulates beam visibility: at 0 the beam is faint
        (you only see where it would hit a surface, in real life); at 1 it
        is fully volumetric.
        """
        h, w = canvas.shape[:2]
        ox, oy = fx.x * w, fx.y * h

        # Front-view aim: pan=0 → straight-down; pan=±1 → ±70° deflection.
        angle = pan * math.radians(70.0)
        dx, dy = math.sin(angle), math.cos(angle)

        length_px = fx.beam_length * h * 1.2
        half_tip = max(8.0, fx.beam_width * w * 0.55)

        perp_x, perp_y = -dy, dx  # unit perpendicular to direction
        tip_cx = ox + dx * length_px
        tip_cy = oy + dy * length_px
        tip_l = (tip_cx + perp_x * half_tip, tip_cy + perp_y * half_tip)
        tip_r = (tip_cx - perp_x * half_tip, tip_cy - perp_y * half_tip)

        pts = np.array([(ox, oy), tip_l, tip_r], dtype=np.float32)

        pad = 16
        x_min = max(0, int(pts[:, 0].min()) - pad)
        x_max = min(w, int(pts[:, 0].max()) + pad)
        y_min = max(0, int(pts[:, 1].min()) - pad)
        y_max = min(h, int(pts[:, 1].max()) + pad)
        if x_max - x_min < 6 or y_max - y_min < 6:
            return

        sub_h = y_max - y_min
        sub_w = x_max - x_min
        mask = np.zeros((sub_h, sub_w), dtype=np.uint8)
        local_pts = (pts - np.array([[x_min, y_min]], dtype=np.float32)
                     ).astype(np.int32)
        cv2.fillConvexPoly(mask, local_pts, 255)

        # Soften the edges → reads as a glowy volumetric beam. Kernel
        # capped at 31 because GaussianBlur cost scales with kernel size
        # — past 31px the visual gain is small and the per-frame budget
        # is real on Pi 5. ~24ms → ~10ms per dozen cones at 1280x720.
        kshort = min(sub_w, sub_h)
        ksize = max(11, min(31, (kshort // 12) | 1))
        mask = cv2.GaussianBlur(mask, (ksize, ksize), 0)

        # Haze multiplier: 0.18 at haze=0 (faint hint of beam), 1.0 at
        # haze=1 (full volumetric).
        haze_mult = 0.18 + 0.82 * haze
        scale = intensity * haze_mult / 255.0
        mask_f = mask.astype(np.float32) * scale
        r, g, b = color

        sub = canvas[y_min:y_max, x_min:x_max]
        sub[..., 0] += mask_f * r
        sub[..., 1] += mask_f * g
        sub[..., 2] += mask_f * b

    def _draw_par_splash(self, canvas, fx, intensity, color, haze):
        """A par can = a soft circular blob centred on the fixture body.

        Pars don't care about haze nearly as much as spots — you can see a
        par light up regardless of atmosphere — so the haze multiplier
        here only modulates a small portion of the brightness.
        """
        h, w = canvas.shape[:2]
        ox, oy = int(fx.x * w), int(fx.y * h)
        radius = max(20, int((fx.beam_width + 0.06) * min(w, h) * 0.6))

        pad = 12
        x_min = max(0, ox - radius - pad)
        x_max = min(w, ox + radius + pad)
        y_min = max(0, oy - radius - pad)
        y_max = min(h, oy + radius + pad)
        if x_max - x_min < 4 or y_max - y_min < 4:
            return

        mask = np.zeros((y_max - y_min, x_max - x_min), dtype=np.uint8)
        cv2.circle(mask, (ox - x_min, oy - y_min), radius, 255, -1)
        ksize = max(15, min(31, (radius // 2) | 1))
        mask = cv2.GaussianBlur(mask, (ksize, ksize), 0)

        haze_mult = 0.55 + 0.45 * haze
        scale = intensity * haze_mult / 255.0
        mask_f = mask.astype(np.float32) * scale
        r, g, b = color

        sub = canvas[y_min:y_max, x_min:x_max]
        sub[..., 0] += mask_f * r
        sub[..., 1] += mask_f * g
        sub[..., 2] += mask_f * b

    def _draw_strobe_flash(self, canvas, fx, intensity, color):
        """Strobe = a bright disc with bloom when on. Intensity is already
        gated to 0 between flashes by `effective_fixture`."""
        h, w = canvas.shape[:2]
        ox, oy = int(fx.x * w), int(fx.y * h)
        radius = max(30, int(fx.strobe_radius * min(w, h)))

        pad = 24
        x_min = max(0, ox - radius - pad)
        x_max = min(w, ox + radius + pad)
        y_min = max(0, oy - radius - pad)
        y_max = min(h, oy + radius + pad)
        if x_max - x_min < 4 or y_max - y_min < 4:
            return

        mask = np.zeros((y_max - y_min, x_max - x_min), dtype=np.uint8)
        cv2.circle(mask, (ox - x_min, oy - y_min), radius, 255, -1)
        ksize = max(25, min(41, (radius // 2) | 1))
        mask = cv2.GaussianBlur(mask, (ksize, ksize), 0)

        scale = intensity * 1.6 / 255.0  # strobes punch — let them clip
        mask_f = mask.astype(np.float32) * scale
        r, g, b = color

        sub = canvas[y_min:y_max, x_min:x_max]
        sub[..., 0] += mask_f * r
        sub[..., 1] += mask_f * g
        sub[..., 2] += mask_f * b

    def _draw_fixture_chrome(self, frame):
        """Draw the mechanism icon for every fixture so the operator can
        see what they're placing. Drawn IN EDIT MODE ONLY — the live show
        stays beam-only (no metal artefacts on the wall)."""
        w, h = self.w, self.h
        for group in self.lights.groups:
            for fx in group.fixtures:
                ox, oy = int(fx.x * w), int(fx.y * h)
                if fx.kind == "spot":
                    # Yoke = small box; head = circle inside.
                    cv2.rectangle(frame, (ox - 10, oy - 7),
                                  (ox + 10, oy + 7),
                                  (160, 170, 200), -1)
                    cv2.rectangle(frame, (ox - 10, oy - 7),
                                  (ox + 10, oy + 7),
                                  (40, 40, 55), 1)
                    cv2.circle(frame, (ox, oy), 5, (40, 40, 55), -1)
                elif fx.kind == "par":
                    cv2.circle(frame, (ox, oy), 9, (150, 160, 180), -1)
                    cv2.circle(frame, (ox, oy), 9, (40, 40, 55), 1)
                    cv2.circle(frame, (ox, oy), 4, (40, 40, 55), -1)
                elif fx.kind == "strobe":
                    cv2.rectangle(frame, (ox - 8, oy - 8),
                                  (ox + 8, oy + 8),
                                  (200, 200, 210), -1)
                    cv2.rectangle(frame, (ox - 8, oy - 8),
                                  (ox + 8, oy + 8),
                                  (40, 40, 55), 1)

    def _draw_lights_edit_overlay(self, frame):
        """Edit-mode overlay drawn on the projector itself: outline the
        selected fixture, draw a palette-armed cursor hint, and tag every
        fixture with its group index so it's clear what owns what."""
        w, h = self.w, self.h
        sel = self.lights.selected_fixture
        for gi, group in enumerate(self.lights.groups):
            for fi, fx in enumerate(group.fixtures):
                ox, oy = int(fx.x * w), int(fx.y * h)
                if sel == (gi, fi):
                    cv2.circle(frame, (ox, oy), 18, (255, 240, 120), 2,
                               cv2.LINE_AA)
                # Tiny group-index tag below the fixture body.
                if self.lights.selected == gi:
                    color = (200, 220, 255)
                else:
                    color = (110, 120, 150)
                cv2.putText(frame, f"G{gi + 1}", (ox + 12, oy + 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1,
                            cv2.LINE_AA)
        # Palette-armed hint, painted near the top-left so the operator
        # knows the next click places a fixture.
        if self.lights.palette_kind is not None:
            label = f"PLACE: {self.lights.palette_kind.upper()}  (Esc to disarm)"
            cv2.putText(frame, label, (16, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 240, 140), 2,
                        cv2.LINE_AA)

    # ── Lights public actions (called from keymap) ────────────────────

    def toggle_lights_mode(self):
        """Enter / leave lights mode. Mutually exclusive with mapping."""
        if self.mode == "lights":
            self.mode = "live"
            self.lights.enabled = False
            self.lights.edit_mode = False
            self.lights.drag = None
            self.lights.palette_kind = None
            self.lights.selected_fixture = None
        else:
            # If we were in mapping mode, drop it first.
            if self.mode == "mapping":
                self.mapping.enabled = False
                self.mapping.edit_mode = False
                self.mapping.drag = None
                self.mapping.bind_armed = False
                self._persist_mapping()
            self.mode = "lights"
            self.lights.enabled = True
            # First-time UX: empty rig → start in EDIT so the operator can
            # immediately drop fixtures with the palette.
            pristine = (len(self.lights.groups) == 1
                        and len(self.lights.groups[0].fixtures) == 0)
            if pristine:
                self.lights.edit_mode = True
            # Reseed the chase clock so the first frame's dt is zero.
            self._lights_last_t = 0.0
        self._persist_lights()
        pygame.mouse.set_visible(True)

    def lights_toggle_edit_mode(self):
        if self.mode != "lights":
            return
        self.lights.toggle_edit_mode()
        self._persist_lights()

    def lights_cycle_group(self, step=1):
        self.lights.cycle_selected(step)
        self._persist_lights()

    def lights_add_group(self):
        self.lights.add_group()
        self._persist_lights()

    def lights_remove_group(self):
        self.lights.remove_selected_group()
        self._persist_lights()

    def lights_arm_palette(self, kind):
        self.lights.arm_palette(kind)
        # Don't persist — palette arming is transient edit-mode state.

    def lights_cancel_edit_gesture(self):
        """Esc inside lights/edit: cancel drag, disarm palette, clear pick."""
        self.lights.cancel_drag()
        self.lights.disarm_palette()
        self.lights.deselect_fixture()

    def lights_delete_selected_fixture(self):
        self.lights.delete_selected_fixture()
        self._persist_lights()

    def lights_set_color(self, name):
        self.lights.set_group_color(name)
        self._persist_lights()

    def lights_cycle_chase(self):
        self.lights.cycle_group_chase()
        self._persist_lights()

    def lights_adjust_haze(self, delta):
        self.lights.adjust_haze(delta)
        self._persist_lights()

    def lights_adjust_master(self, delta):
        self.lights.adjust_group_master(delta)
        self._persist_lights()

    def lights_tap_tempo(self):
        self.lights.tap_tempo()
        self._persist_lights()

    def lights_recall_cue(self, slot):
        if self.lights.recall_cue(slot):
            self._persist_lights()

    def lights_save_cue(self, slot):
        self.lights.save_cue(slot)
        self._persist_lights()

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
        from keymap import (dispatch, NAV_KEYS, FAV_KEYS, CLIP_FAV_KEYS,
                            HIT_KEYS, fav_tap, fav_long)
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

                    # In mapping/edit and lights/edit, the operator is laying
                    # out spaces / fixtures — swallow favourite long-press
                    # timing so 1-0 / Q-P don't fire content actions.
                    editing = (
                        (self.mode == "mapping" and self.mapping.edit_mode)
                        or (self.mode == "lights" and self.lights.edit_mode)
                    )
                    # Lights mode uses only the 1-0 row as fav-keys (cue
                    # stack); Q-P are plain keys in lights/perform.
                    fav_keys_active = (set(CLIP_FAV_KEYS)
                                       if self.mode == "lights"
                                       else FAV_KEYS)
                    if event.key in fav_keys_active and not editing:
                        if is_initial:
                            fav_pressed_at[event.key] = time.time()
                            long_press_fired.discard(event.key)
                        # Ignore auto-repeats for favourite keys.
                    elif is_initial or event.key in NAV_KEYS:
                        dispatch(self, event.key, event.mod)

                elif event.type == pygame.KEYUP:
                    if event.key in fav_pressed_at:
                        elapsed = time.time() - fav_pressed_at.pop(event.key)
                        in_edit = (
                            (self.mode == "mapping" and self.mapping.edit_mode)
                            or (self.mode == "lights" and self.lights.edit_mode)
                        )
                        if (event.key not in long_press_fired
                                and elapsed < LONG_PRESS_S
                                and not in_edit):
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
            self._flush_lights_persist()
            self.clock.tick(self.cfg.fps)
        self._flush_mapping_persist()
        self._flush_lights_persist()
        self.clips.release_all()
        self.overlays.release_all()
