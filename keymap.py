import pygame


# ── Browser navigation ───────────────────────────────────────────────
#
# Number row → CLIP library (potentially hundreds of files).
# Top letter row → OVERLAY library, same layout.
#
#   key 1/Q  prev (-1)            key 6/Y  +25
#   key 2/W  next (+1)            key 7/U  first
#   key 3/E  -5                   key 8/I  last
#   key 4/R  +5                   key 9/O  random
#   key 5/T  -25                  key 0/P  off (deselect)
#
# Holding a nav key auto-repeats so you can scrub through the library.

CLIP_BROWSE = {
    pygame.K_1: ("step", -1),
    pygame.K_2: ("step", 1),
    pygame.K_3: ("step", -5),
    pygame.K_4: ("step", 5),
    pygame.K_5: ("step", -25),
    pygame.K_6: ("step", 25),
    pygame.K_7: ("first", None),
    pygame.K_8: ("last", None),
    pygame.K_9: ("random", None),
    pygame.K_0: ("off", None),
}

OVERLAY_BROWSE = {
    pygame.K_q: ("step", -1),
    pygame.K_w: ("step", 1),
    pygame.K_e: ("step", -5),
    pygame.K_r: ("step", 5),
    pygame.K_t: ("step", -25),
    pygame.K_y: ("step", 25),
    pygame.K_u: ("first", None),
    pygame.K_i: ("last", None),
    pygame.K_o: ("random", None),
    pygame.K_p: ("off", None),
}

# Keys that should auto-repeat when held (scrub through libraries).
NAV_KEYS = set(CLIP_BROWSE) | set(OVERLAY_BROWSE)

# Home row → generative base layers.
# Indices match engine.GENERATIVES:
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
    # Quit with Shift+Esc, plain Esc = kill all FX
    if key == pygame.K_ESCAPE:
        if mod & pygame.KMOD_SHIFT:
            engine.quit()
        else:
            engine.kill_all()
        return

    if key == pygame.K_SPACE:
        engine.toggle_blackout()
        return

    if key == pygame.K_BACKSPACE:
        engine.toggle_freeze()
        return

    # Output-display picker (works regardless of which window has focus,
    # since these fire wherever keyboard focus happens to land — important
    # for fullscreen mode where the control HUD is hard to click into).
    if key == pygame.K_F11:
        engine.cycle_pending_display()
        return
    if key == pygame.K_F12:
        engine.apply_pending_display()
        return

    # Arrow keys are sampled continuously each frame in Engine.run() — no
    # KEYDOWN handling here so a held key produces smooth motion instead of
    # one-shot steps.
    if key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
        return

    if key in CLIP_BROWSE:
        action, arg = CLIP_BROWSE[key]
        engine.browse_clips(action, arg)
        return

    if key in OVERLAY_BROWSE:
        action, arg = OVERLAY_BROWSE[key]
        engine.browse_overlays(action, arg)
        return

    if key in GEN_KEYS:
        engine.select_generative(GEN_KEYS.index(key))
        return

    if key in HIT_KEYS:
        engine.fire_hit(HIT_KEYS[key], frames=5)
        return

    if key in FX_KEYS:
        engine.toggle_fx(FX_KEYS[key])
        return
