import os
import random
import time

import pygame
import numpy as np
import cv2

from clips import ClipPool
from mapping import MappingManager
from state import load_state, save_state
from effects import EffectContext
from gpu import Renderer


GENERATIVES = [
    "plasma", "tunnel", "starfield",
    "warp", "waves", "cells",
    "lissajous", "moire", "metaballs",
]

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
        # Set of HIT_KEYS we've seen go down without a matching KEYUP.
        # Sourced from real events (not get_pressed) so a lost KEYUP
        # never leaves a hit stuck in the on position.
        self._held_hit_keys = set()
        self.blackout = False
        self.freeze = False
        self.frozen_frame = None

        # GPU pipeline. Created here so its constructor runs AFTER pygame
        # has made the GL context current (main.py opens the OPENGL
        # window before constructing Engine).
        self.gpu = Renderer(self.w, self.h)
        self.gpu.set_screen_size(self.screen.get_size())

        # Per-group source FBO + previous-source FBO for feedback trails.
        # Keyed by `id(group)` — wiped if a group is removed in mapping
        # mode. The trail FBO lets a per-group feedback FX sample its own
        # previous content (rather than the whole canvas), which is the
        # correct behaviour for independent group loops.
        self._group_src_fbos = {}
        self._group_prev_fbos = {}

        # Tracks whether feedback was active in the LIVE-mode chain this
        # frame; if so we copy the composited result into the global
        # trail buffer at end-of-frame so the next frame's feedback shader
        # has something to sample.
        self._live_had_feedback = False

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
            # Snapshot whatever the GPU pipeline last rendered. readback()
            # returns a buffer the renderer reuses, so we copy() to own it
            # for the duration of the freeze.
            try:
                snap = self.gpu.readback()
                self.frozen_frame = snap.copy() if snap is not None else None
            except Exception as exc:  # noqa: BLE001 - any GL error → no freeze frame
                print(f"[vj] freeze snapshot failed: {exc!r}")
                self.frozen_frame = None

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
            flags = pygame.NOFRAME | pygame.OPENGL | pygame.DOUBLEBUF
            size = (dw, dh)
        else:
            flags = pygame.OPENGL | pygame.DOUBLEBUF
            size = (self.w, self.h)

        try:
            self.screen = pygame.display.set_mode(size, flags, display=new_idx)
            pygame.display.set_caption("pi-paint VJ — Output")
            pygame.mouse.set_visible(not self.cfg.fullscreen)
            # SDL2 typically destroys the GL context on set_mode under
            # OPENGL — every texture / program / FBO our Renderer holds
            # would be invalid. Safest path is a fresh Renderer keyed to
            # the same render resolution; we lose the feedback-trail
            # buffer but that re-fills in one frame.
            self.gpu = Renderer(self.w, self.h)
            self.gpu.set_screen_size(size)
            self._group_src_fbos.clear()
            self._group_prev_fbos.clear()
            print(f"[vj] output → display {new_idx} ({size[0]}x{size[1]}, "
                  f"borderless={self.cfg.fullscreen})")
        except (TypeError, pygame.error, ValueError) as exc:
            print(f"[vj] switch_output_display({new_idx}) failed: {exc!r}")

    def update_held_hits(self, hit_keys_map):
        """Sustain a punch-in hit while its key is held down.

        compose_frame() decrements hit_frames_left each frame; we top it up
        as long as the corresponding key is pressed. Two frames of headroom
        means an in-flight hit gracefully finishes the frame after release.

        We track held state from KEYDOWN / KEYUP / WINDOWFOCUSLOST events
        rather than polling pygame.key.get_pressed(). Under Wayland (and
        sometimes X11 after focus changes) get_pressed() has been seen to
        report a key as held forever when its KEYUP got dropped, which
        previously caused a permanent strobe / white-screen lockup.
        """
        for k in self._held_hit_keys:
            hit_name = hit_keys_map.get(k)
            if hit_name is None:
                continue
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
            # Arrows in autopilot tune the INTERVALS directly (longer
            # interval = slower changes). Pressing up adds time, down
            # subtracts; right adds, left subtracts — so the direction
            # matches the value on the bar, not "how fast it feels".
            if keys[pygame.K_UP]:
                self.auto_clip_interval = min(60.0, self.auto_clip_interval + dt * 4.0)
            if keys[pygame.K_DOWN]:
                self.auto_clip_interval = max(1.0, self.auto_clip_interval - dt * 4.0)
            if keys[pygame.K_RIGHT]:
                self.auto_fx_interval   = min(30.0, self.auto_fx_interval  + dt * 3.0)
            if keys[pygame.K_LEFT]:
                self.auto_fx_interval   = max(0.5, self.auto_fx_interval   - dt * 3.0)
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

    # ── Render pipeline (GPU) ─────────────────────────────────────────
    #
    # The whole compose pipeline is driven by `self.gpu` (moderngl).
    # Generatives are fragment shaders, FX are shader passes through a
    # ping-pong FBO pair, clips/overlays are textures, and mapping
    # warps are projective-textured quads. `compose_frame()` returns a
    # numpy RGB frame ONLY when the CPU side still needs to look at
    # pixels (HUD open, mapping/edit-mode overlays, PERFORM borders);
    # otherwise the pipeline stays fully GPU-resident and we return
    # None for blit_to_output() to present the GPU FBO directly.

    def _needs_cpu_frame(self):
        if self._has_hud:
            return True
        if self.mode == "mapping" and self.mapping.edit_mode:
            return True
        if (self.mode == "mapping"
                and self.mapping.show_borders
                and self.mapping.selected_group() is not None):
            return True
        return False

    def _build_base_gpu(self, ctx):
        """Push the base layer (clip / generative / black) into the
        current ping-pong FBO."""
        clip_frame = self.clips.read()
        if clip_frame is not None:
            self.gpu.upload_clip(clip_frame)
            self.gpu.draw_clip_base()
            return
        if self.active_generative:
            self.gpu.draw_generative(self.active_generative, ctx.t,
                                     (ctx.px, ctx.py))
            return
        # Nothing selected — current FBO stays at begin_frame() black.

    def _apply_fx_gpu(self, fx_state, ctx):
        """Run each enabled FX as one shader pass through the ping-pong.
        Returns True if feedback was active (so the caller knows to
        update the trail buffer at end-of-frame)."""
        s = fx_state
        had_feedback = False
        if s.get("kaleido"):
            self.gpu.fx_kaleidoscope(int(3 + ctx.px * 9))
        if s.get("mirror"):
            self.gpu.fx_mirror_h()
        if s.get("rgb_split"):
            self.gpu.fx_rgb_split(int(4 + ctx.px * 20))
        if s.get("posterize"):
            self.gpu.fx_posterize(int(2 + ctx.py * 6))
        if s.get("edges"):
            self.gpu.fx_edges()
        if s.get("invert"):
            self.gpu.fx_invert()
        if s.get("feedback"):
            zoom = 1.0 + ctx.px * 0.08
            rot = (ctx.py - 0.5) * 4.0
            self.gpu.fx_feedback(zoom, rot)
            had_feedback = True
        return had_feedback

    def _apply_overlay_gpu(self, pool, stem=None):
        """Screen-blend the active overlay onto the current FBO."""
        if stem is None:
            ov = pool.read()
        else:
            idx = pool.find_by_stem(stem)
            if idx is None:
                return
            pool.ensure_open(idx)
            ov = pool.read_at(idx)
        if ov is None:
            return
        self.gpu.upload_overlay(ov)
        self.gpu.apply_overlay_screen_blend()

    def _apply_hits_gpu(self):
        if self.hit_frames_left <= 0:
            return
        n = self.hit_frames_left
        self.hit_frames_left -= 1
        if self.hit_type == "strobe":
            self.gpu.hit_strobe()
        elif self.hit_type == "black_flash":
            self.gpu.hit_black()
        elif self.hit_type == "invert_flash":
            self.gpu.hit_invert()
        elif self.hit_type == "zoom_punch":
            scale = 1.0 + 0.25 * (n / 5.0)
            self.gpu.hit_zoom_punch(scale)
        elif self.hit_type == "rgb_smash":
            self.gpu.hit_rgb_smash()

    def compose_frame(self):
        """Build the next output frame on the GPU. Returns a CPU numpy
        RGB frame iff `_needs_cpu_frame()` is True, else None."""
        had_feedback = False
        self.gpu.begin_frame()

        if self.blackout:
            # begin_frame() already cleared to black; nothing else needed.
            pass
        elif self.freeze and self.frozen_frame is not None:
            self.gpu.upload_frozen(self.frozen_frame)
            self.gpu.draw_frozen_base()
        elif self.mode == "mapping":
            self._compose_mapping_frame_gpu()
        else:
            ctx = EffectContext(
                self.w, self.h, time.time() - self.start_time,
                (self.param_x, self.param_y),
            )
            self._build_base_gpu(ctx)
            had_feedback = self._apply_fx_gpu(self.fx_state, ctx)
            self._apply_overlay_gpu(self.overlays)
            self._apply_hits_gpu()

        cpu_frame = None
        if self._needs_cpu_frame():
            cpu_frame = self.gpu.readback().copy()
            if self.mode == "mapping" and self.mapping.edit_mode:
                # Borders + edit overlays still use cv2 (they include
                # small icons + text that aren't worth a glyph atlas
                # right now). Drawing on the readback means they appear
                # on both the HUD preview AND, after present_cpu_frame,
                # on the projector.
                if self.mapping.show_borders:
                    self._draw_selection_border(cpu_frame)
                self._draw_edit_overlay(cpu_frame)
            elif self.mapping.show_borders and self.mode == "mapping":
                self._draw_selection_border(cpu_frame)

        # Feedback trail: copy this frame's composited GPU output into
        # the trail FBO so next frame's feedback shader has something to
        # sample. Skipped when feedback wasn't active to save the copy
        # pass — first feedback frame samples whatever was last in the
        # buffer, which fades naturally over a few frames.
        if had_feedback:
            self.gpu.update_feedback_trail()

        return cpu_frame

    @property
    def _has_hud(self):
        # control window plumbing happens via Engine.run(control=...);
        # we lazily mark availability when a HUD is attached.
        return getattr(self, "_control_attached", False)

    # ── Mapping render pipeline (GPU) ─────────────────────────────────

    def _compose_mapping_frame_gpu(self):
        """For each group: compose its source into a persistent FBO,
        then either project that source per-space (fit_mode=stretch) or
        treat each space as a window onto a single canvas-wide video
        plane (fit_mode in {window, fit, fill}). Hits applied at the
        end; borders + edit overlays are drawn on the CPU readback
        further down in compose_frame()."""
        now = time.time()
        self.mapping.tick_autopilot(self, now)

        # Garbage-collect FBOs for groups that no longer exist.
        live_ids = {id(g) for g in self.mapping.groups}
        for stale in [k for k in self._group_src_fbos if k not in live_ids]:
            del self._group_src_fbos[stale]
            self._group_prev_fbos.pop(stale, None)

        for group in self.mapping.groups:
            # Skip groups with no spaces (no mask) AND skip groups
            # whose content is blackout (nothing to compose). Saves a
            # full-resolution generative call per blackout group.
            if not group.spaces:
                continue
            # Skip groups with no real content to compose. Matches the
            # CPU pipeline's optimisation — saves a generative-shader
            # pass per blackout group.
            if (group.content_kind == "blackout"
                    or (group.content_kind == "clip" and not group.clip_stem)
                    or (group.content_kind == "generative" and not group.gen_name)):
                continue
            gid = id(group)
            src_fbo = self._group_src_fbos.get(gid)
            if src_fbo is None:
                src_fbo = self.gpu.make_group_fbo()
                self._group_src_fbos[gid] = src_fbo
            prev_fbo = self._group_prev_fbos.get(gid)
            if prev_fbo is None:
                prev_fbo = self.gpu.make_group_fbo()
                self._group_prev_fbos[gid] = prev_fbo

            self._compose_group_source_gpu(group, now, src_fbo, prev_fbo)
            source_tex = src_fbo.color_attachments[0]

            # fit_mode dispatch:
            #   * stretch → each space gets its own perspective-warped
            #     copy of the source (legacy billboard).
            #   * window / fit / fill → the source is laid down ONCE
            #     across the canvas (with zoom + pan in window mode),
            #     and each space is a window onto that single plane,
            #     so multi-space groups stay visually continuous.
            if group.fit_mode == "stretch":
                for space in group.spaces:
                    corners = np.array(space.corners, dtype=np.float64)
                    self.gpu.warp_source_into_quad(source_tex, corners)
            else:
                dst_xy, dst_size = self._mapping_window_rect(group)
                for space in group.spaces:
                    corners = np.array(space.corners, dtype=np.float64)
                    self.gpu.warp_source_into_window(
                        source_tex, corners, dst_xy, dst_size,
                    )

        self._apply_hits_gpu()

    def _mapping_window_rect(self, group):
        """Pixel-space (top-left xy, width-height) for the video plane
        a group's content gets stamped onto when fit_mode is window /
        fit / fill. Mirrors the CPU `_window_group_into_canvas` math;
        on the GPU side the source FBO is always canvas-sized so the
        base-scale math collapses to 1.0, and zoom/pan only kick in for
        window mode (fit and fill are equivalent and pass through 1:1)."""
        w, h = self.w, self.h
        mode = group.fit_mode
        if mode == "window":
            zoom = max(0.05, float(group.zoom))
            pan_x, pan_y = float(group.pan_x), float(group.pan_y)
        else:  # "fit" / "fill"
            zoom, pan_x, pan_y = 1.0, 0.0, 0.0
        dw = max(2, int(round(w * zoom)))
        dh = max(2, int(round(h * zoom)))
        cx = w * 0.5 + pan_x * w * 0.5
        cy = h * 0.5 + pan_y * h * 0.5
        dx = int(round(cx - dw * 0.5))
        dy = int(round(cy - dh * 0.5))
        return (dx, dy), (dw, dh)

    def _compose_group_source_gpu(self, group, now, src_fbo, prev_fbo):
        """Render one group's source content into `src_fbo`. `prev_fbo`
        is the previous-frame source for this group (used by feedback)."""
        ctx = EffectContext(
            self.w, self.h,
            now - self.start_time + group._time_offset,
            (group.param_x, group.param_y),
        )
        # Redirect the renderer's ping-pong to this group's FBO pair.
        self.gpu.begin_into_aux(prev_source_fbo=prev_fbo)
        try:
            if group.content_kind == "clip" and group.clip_stem:
                idx = self.clips.find_by_stem(group.clip_stem)
                if idx is not None:
                    self.clips.ensure_open(idx)
                    frame = self.clips.read_at(idx)
                    if frame is not None:
                        self.gpu.upload_clip(frame)
                        self.gpu.draw_clip_base()
            elif group.content_kind == "generative" and group.gen_name:
                self.gpu.draw_generative(group.gen_name, ctx.t,
                                         (ctx.px, ctx.py))
            # else: blackout — aux FBO already cleared.

            had_feedback = self._apply_fx_gpu(group.fx_state, ctx)
            if group.overlay_stem:
                self._apply_overlay_gpu(self.overlays, stem=group.overlay_stem)

            # Persist the composited result into src_fbo (the texture the
            # caller will warp from), and into prev_fbo for next frame's
            # feedback sampling (only when feedback is active, to save a
            # pass when it's not needed).
            self.gpu.copy_current_to(src_fbo)
            if had_feedback:
                self.gpu.copy_current_to(prev_fbo)
        finally:
            self.gpu.finish_aux()

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
        """Present the latest composited frame to the output window.

        `frame` is None when the GPU pipeline finished cleanly with no
        CPU side-trip (the live performance path with the HUD closed).
        It's a numpy RGB ndarray when the HUD is open or mapping/edit
        mode added cv2-drawn overlays — in that case we re-upload the
        modified frame and let the GPU sample it to the display res.
        Either way the final scale to the display window happens on the
        GPU now (bilinear sampling, no `pygame.transform.smoothscale`).
        """
        self.gpu.set_screen_size(self.screen.get_size())
        if frame is None:
            self.gpu.present()
        else:
            self.gpu.present_cpu_frame(frame)
        pygame.display.flip()

    def render(self):
        """Convenience: compose + blit (used when there is no control window)."""
        frame = self.compose_frame()
        self.blit_to_output(frame)
        return frame

    def run(self, control=None):
        from keymap import dispatch, NAV_KEYS, FAV_KEYS, HIT_KEYS, fav_tap, fav_long
        # The HUD's existence is what tells compose_frame to do a GPU
        # readback every frame (so the preview surface has pixels to
        # show); without a HUD we keep the pipeline GPU-resident.
        self._control_attached = control is not None
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
                    # K_e collides with the slot-2 overlay favourite, but in
                    # MAPPING mode E is the EDIT-mode toggle — route it
                    # straight to dispatch so the favourite handler doesn't
                    # eat the keystroke before the mapping dispatcher sees it.
                    is_mapping_e = (self.mode == "mapping"
                                    and event.key == pygame.K_e
                                    and not (event.mod & pygame.KMOD_CTRL))
                    if (event.key in FAV_KEYS and not editing
                            and not is_mapping_e):
                        if is_initial:
                            fav_pressed_at[event.key] = time.time()
                            long_press_fired.discard(event.key)
                        # Ignore auto-repeats for favourite keys.
                    elif is_initial or event.key in NAV_KEYS:
                        dispatch(self, event.key, event.mod)

                    # Mark hit keys as held off REAL events so a missed
                    # KEYUP can't leave us stuck on (which previously
                    # made the strobe shader run forever → white-screen).
                    if event.key in HIT_KEYS:
                        self._held_hit_keys.add(event.key)

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
                    self._held_hit_keys.discard(event.key)

                elif event.type == pygame.WINDOWFOCUSLOST:
                    held_keys.clear()
                    fav_pressed_at.clear()
                    self._held_hit_keys.clear()
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
