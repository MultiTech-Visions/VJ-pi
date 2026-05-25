"""Keyboard → action dispatch.

Each pygame key maps to an `action` dict (see `actions.py` for the
registry). The engine's main loop runs the action via `actions.run()`,
the same path the web control panel uses — so keyboard and phone can
never drift apart.

Arrow keys are special: they sample continuously each frame in
`Engine.update_params_from_keys()` so a held key produces smooth motion.
They don't go through this dispatch.
"""
import pygame

import actions


# ── Library cycling ──────────────────────────────────────────────────
#
# `-` / `=` cycle through the CLIP library (prev / next), auto-repeat
# while held. `[` / `]` do the same for OVERLAYS.

CYCLE_CLIPS_PREV   = pygame.K_MINUS
CYCLE_CLIPS_NEXT   = pygame.K_EQUALS
CYCLE_OVRS_PREV    = pygame.K_LEFTBRACKET
CYCLE_OVRS_NEXT    = pygame.K_RIGHTBRACKET

# Keys that should auto-repeat when held (so a held `-` scrubs back).
NAV_KEYS = {CYCLE_CLIPS_PREV, CYCLE_CLIPS_NEXT,
            CYCLE_OVRS_PREV,  CYCLE_OVRS_NEXT}


# ── Favourite slots ──────────────────────────────────────────────────
#
# Number row → 10 CLIP favourite slots (key "1" = slot 0 .. key "0" = slot 9).
# Top letter row → 10 OVERLAY favourite slots.
#
# Tap a slot to recall its assigned clip / overlay; long-press (≥ 500 ms)
# to assign whatever is currently playing into that slot. Long-press while
# nothing is playing clears the slot. Assignments persist in vj_state.json
# between sessions.

CLIP_FAV_KEYS = [
    pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5,
    pygame.K_6, pygame.K_7, pygame.K_8, pygame.K_9, pygame.K_0,
]
OVERLAY_FAV_KEYS = [
    pygame.K_q, pygame.K_w, pygame.K_e, pygame.K_r, pygame.K_t,
    pygame.K_y, pygame.K_u, pygame.K_i, pygame.K_o, pygame.K_p,
]
FAV_KEYS = set(CLIP_FAV_KEYS) | set(OVERLAY_FAV_KEYS)


def fav_tap(engine, key):
    if key in CLIP_FAV_KEYS:
        actions.run(engine, {"name": "play_clip_favorite",
                             "args": {"slot": CLIP_FAV_KEYS.index(key)}})
    elif key in OVERLAY_FAV_KEYS:
        actions.run(engine, {"name": "play_overlay_favorite",
                             "args": {"slot": OVERLAY_FAV_KEYS.index(key)}})


def fav_long(engine, key):
    if key in CLIP_FAV_KEYS:
        actions.run(engine, {"name": "save_clip_favorite",
                             "args": {"slot": CLIP_FAV_KEYS.index(key)}})
    elif key in OVERLAY_FAV_KEYS:
        actions.run(engine, {"name": "save_overlay_favorite",
                             "args": {"slot": OVERLAY_FAV_KEYS.index(key)}})


# ── Generative bases / hits / FX ─────────────────────────────────────

# Home row → generative base layers. Indices match engine.GENERATIVES:
#   A=plasma  S=tunnel  D=starfield  F=warp  G=waves  H=cells
#   J=lissajous  K=moiré  L=metaballs
GEN_KEYS = [
    pygame.K_a, pygame.K_s, pygame.K_d, pygame.K_f, pygame.K_g,
    pygame.K_h, pygame.K_j, pygame.K_k, pygame.K_l,
]

# Bottom row → one-shot hits
HIT_KEYS = {
    pygame.K_z: "strobe",
    pygame.K_x: "black_flash",
    pygame.K_c: "invert_flash",
    pygame.K_v: "zoom_punch",
    pygame.K_b: "rgb_smash",
}

# Function keys → persistent FX toggles
FX_KEYS = {
    pygame.K_F1: "kaleido",
    pygame.K_F2: "mirror",
    pygame.K_F3: "feedback",
    pygame.K_F4: "invert",
    pygame.K_F5: "posterize",
    pygame.K_F6: "edges",
    pygame.K_F7: "rgb_split",
}


def dispatch(engine, key, mod):
    # Quit with Shift+Esc, plain Esc = full reset
    if key == pygame.K_ESCAPE:
        if mod & pygame.KMOD_SHIFT:
            # Quit is intentionally NOT exposed as a registered action —
            # we don't want a phone in the audience killing the show.
            engine.quit()
        else:
            actions.run(engine, {"name": "kill_all"})
        return

    if key == pygame.K_SPACE:
        actions.run(engine, {"name": "toggle_blackout"})
        return

    if key == pygame.K_BACKSPACE:
        actions.run(engine, {"name": "toggle_freeze"})
        return

    # Output-display picker (works regardless of which window has focus,
    # since these fire wherever keyboard focus happens to land — important
    # for fullscreen mode where the control HUD is hard to click into).
    if key == pygame.K_F11:
        actions.run(engine, {"name": "cycle_pending_display"})
        return
    if key == pygame.K_F12:
        actions.run(engine, {"name": "apply_pending_display"})
        return

    # Arrow keys are sampled continuously each frame in Engine.run() — no
    # KEYDOWN handling here so a held key produces smooth motion.
    if key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
        return

    if key == CYCLE_CLIPS_PREV:
        actions.run(engine, {"name": "browse_clips", "args": {"action": "step", "arg": -1}})
        return
    if key == CYCLE_CLIPS_NEXT:
        actions.run(engine, {"name": "browse_clips", "args": {"action": "step", "arg": 1}})
        return
    if key == CYCLE_OVRS_PREV:
        actions.run(engine, {"name": "browse_overlays", "args": {"action": "step", "arg": -1}})
        return
    if key == CYCLE_OVRS_NEXT:
        actions.run(engine, {"name": "browse_overlays", "args": {"action": "step", "arg": 1}})
        return

    # Favourite keys (1-0, Q-P) are handled in Engine.run()'s long-press
    # logic — they don't go through dispatch at all.

    if key in GEN_KEYS:
        from engine import GENERATIVES
        actions.run(engine, {"name": "select_generative",
                             "args": {"name": GENERATIVES[GEN_KEYS.index(key)]}})
        return

    if key in HIT_KEYS:
        actions.run(engine, {"name": "fire_hit", "args": {"kind": HIT_KEYS[key]}})
        return

    if key in FX_KEYS:
        actions.run(engine, {"name": "toggle_fx", "args": {"name": FX_KEYS[key]}})
        return
