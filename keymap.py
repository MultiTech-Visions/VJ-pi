import pygame


# ── Library cycling ──────────────────────────────────────────────────
#
# `-` / `=` cycle through the CLIP library (prev / next), auto-repeat
# while held. `[` / `]` cycle through the GPU generator catalogue.

CYCLE_CLIPS_PREV   = pygame.K_MINUS
CYCLE_CLIPS_NEXT   = pygame.K_EQUALS
CYCLE_GENS_PREV    = pygame.K_LEFTBRACKET
CYCLE_GENS_NEXT    = pygame.K_RIGHTBRACKET

# Keys that should auto-repeat when held (so a held `-` scrubs back).
NAV_KEYS = {CYCLE_CLIPS_PREV, CYCLE_CLIPS_NEXT,
            CYCLE_GENS_PREV,  CYCLE_GENS_NEXT}

# The two cycle keys in each NAV pair (so one suppresses the other while a
# chord is forming). key → its partner in the same pair.
NAV_PARTNER = {
    CYCLE_CLIPS_PREV: CYCLE_CLIPS_NEXT, CYCLE_CLIPS_NEXT: CYCLE_CLIPS_PREV,
    CYCLE_GENS_PREV:  CYCLE_GENS_NEXT,  CYCLE_GENS_NEXT:  CYCLE_GENS_PREV,
}

# Hold BOTH keys of a pair to open the number-jump picker for that library:
# both `-`/`=` → clips, both `[`/`]` → generators. target → (keyA, keyB).
CHORD_PAIRS = {
    "clip": (CYCLE_CLIPS_PREV, CYCLE_CLIPS_NEXT),
    "gen":  (CYCLE_GENS_PREV,  CYCLE_GENS_NEXT),
}

# Number-row + keypad digits → their character, for typing a jump target.
DIGIT_KEYS = {}
for _d in range(10):
    DIGIT_KEYS[getattr(pygame, f"K_{_d}")] = str(_d)
    DIGIT_KEYS[getattr(pygame, f"K_KP{_d}", getattr(pygame, f"K_KP_{_d}", -1))] = str(_d)
DIGIT_KEYS.pop(-1, None)


# ── Favourite slots ──────────────────────────────────────────────────
#
# Number row → 10 CLIP favourite slots (key "1" = slot 0 .. key "0" = slot 9).
# Home row → 10 GENERATOR favourite slots.
#
# Tap a slot to recall its assigned clip / generator; long-press (≥ 500 ms)
# to assign whatever is currently playing into that slot. Long-press while
# nothing is playing clears the slot. Assignments persist in vj_state.json
# between sessions.

CLIP_FAV_KEYS = [
    pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5,
    pygame.K_6, pygame.K_7, pygame.K_8, pygame.K_9, pygame.K_0,
]
GEN_FAV_KEYS = [
    pygame.K_a, pygame.K_s, pygame.K_d, pygame.K_f, pygame.K_g,
    pygame.K_h, pygame.K_j, pygame.K_k, pygame.K_l, pygame.K_SEMICOLON,
]
FAV_KEYS = set(CLIP_FAV_KEYS) | set(GEN_FAV_KEYS)


def fav_tap(engine, key):
    if key in CLIP_FAV_KEYS:
        engine.play_clip_favorite(CLIP_FAV_KEYS.index(key))
    elif key in GEN_FAV_KEYS:
        engine.play_generator_favorite(GEN_FAV_KEYS.index(key))


def fav_long(engine, key):
    if key in CLIP_FAV_KEYS:
        engine.save_clip_favorite(CLIP_FAV_KEYS.index(key))
    elif key in GEN_FAV_KEYS:
        engine.save_generator_favorite(GEN_FAV_KEYS.index(key))


# ── Generative bases / hits / FX ─────────────────────────────────────

# Bottom row → punch-in hits. Tap = a short one-shot; hold = sustained
# for as long as the key is down (handled by per-frame polling in
# Engine.run, not via pygame's KEYDOWN auto-repeat — the auto-repeat
# initial delay is longer than a single hit's duration so it'd flicker).
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
    pygame.K_F8: "melt",
}


def dispatch(engine, key, mod):
    # Shift+Esc quits anywhere. Plain Esc has mode-dependent meaning.
    if key == pygame.K_ESCAPE:
        if mod & pygame.KMOD_SHIFT:
            engine.quit()
            return
        if engine.mode == "cinematic":
            engine.stop_cinematic_mode()
            return
        if engine.mode == "mapping" and engine.mapping.edit_mode:
            # Cancel any in-flight drag / deselect the picked space, but
            # stay in edit mode so the operator can keep working.
            engine.mapping_cancel_drag()
            return
        engine.kill_all()
        return

    # 4K cinematic uses the SAME keys as the rest of the app — it's not a
    # separate control scheme. The clip-cycle keys step the 4K playlist (cycle
    # videos = cycle videos), N toggles the mode off (the same key that turned
    # it on), and EVERY other key drops out of cinematic and then does its
    # normal job (so M → mapping, [ ] → generators, a favourite plays a
    # favourite, etc.). Handled before the M/N toggles below so those don't
    # pre-empt it.
    if engine.mode == "cinematic":
        if key == CYCLE_CLIPS_PREV:
            engine.cinematic_step(-1)
            return
        if key == CYCLE_CLIPS_NEXT:
            engine.cinematic_step(1)
            return
        if key == pygame.K_n and not (mod & pygame.KMOD_CTRL):
            engine.toggle_cinematic_mode()
            return
        # Anything else: leave cinematic, then fall through to normal handling.
        engine.stop_cinematic_mode()

    # Mode toggle works in both modes; no Ctrl required.
    if key == pygame.K_m and not (mod & pygame.KMOD_CTRL):
        engine.toggle_mapping_mode()
        return

    if key == pygame.K_n and not (mod & pygame.KMOD_CTRL):
        engine.toggle_cinematic_mode()
        return

    # Mapping-mode-only ops.
    if engine.mode == "mapping":
        if key == pygame.K_TAB:
            step = -1 if (mod & pygame.KMOD_SHIFT) else 1
            engine.cycle_mapping_group(step)
            return
        # Edit-mode sub-shortcuts use bare letter keys (the live keymap is
        # intentionally suppressed in edit mode below).
        if key == pygame.K_e and not (mod & pygame.KMOD_CTRL):
            engine.toggle_edit_mode()
            return
        # F9 / F10 — lower / raise the mapping compositing resolution live
        # (trade sharpness for framerate). Works in perform and edit mode.
        if key == pygame.K_F9:
            engine.mapping_adjust_render_scale(-0.05)
            return
        if key == pygame.K_F10:
            engine.mapping_adjust_render_scale(0.05)
            return
        if engine.mapping.edit_mode:
            if key == pygame.K_b and not (mod & pygame.KMOD_CTRL):
                engine.mapping_arm_bind()
                return
            if key == pygame.K_u and not (mod & pygame.KMOD_CTRL):
                engine.mapping_unbind_selected_space()
                return
            if key == pygame.K_BACKSPACE and not (mod & pygame.KMOD_CTRL):
                engine.mapping_request_remove_group()
                return
            if key in (pygame.K_DELETE,):
                engine.mapping_delete_selected_space()
                return
            # Arrow keys fine-tune the keyboard-active corner of the
            # selected space, pixel-by-pixel — drag a corner close with the
            # mouse, then dial it in with the arrows. Shift = coarse 10px
            # step. (Auto-repeat is off, so one tap = one step.)
            if key in (pygame.K_LEFT, pygame.K_RIGHT,
                       pygame.K_UP, pygame.K_DOWN):
                step = 10 if (mod & pygame.KMOD_SHIFT) else 1
                dx = (-step if key == pygame.K_LEFT
                      else step if key == pygame.K_RIGHT else 0)
                dy = (-step if key == pygame.K_UP
                      else step if key == pygame.K_DOWN else 0)
                engine.mapping_nudge_corner(dx, dy)
                return
            # In edit mode, swallow the content / FX / favourite keys —
            # the operator is laying out spaces, not jamming. Mapping
            # operations under Ctrl below still work, and so do
            # Tab / M / Esc / Backspace above.
            if (key in FAV_KEYS or key in HIT_KEYS
                    or key in FX_KEYS
                    or key == pygame.K_BACKSLASH):
                return
        if mod & pygame.KMOD_CTRL:
            if key == pygame.K_n:
                engine.mapping_add_group()
                return
            if key == pygame.K_BACKSPACE:
                engine.mapping_remove_group()
                return
            if key == pygame.K_g:
                engine.mapping_cycle_grid()
                return
            if key == pygame.K_PLUS or key == pygame.K_EQUALS:
                engine.mapping_add_space()
                return
            if key == pygame.K_MINUS:
                engine.mapping_remove_space()
                return
            if key == pygame.K_a:
                engine.mapping_toggle_autopilot()
                return
            if key == pygame.K_k:
                engine.mapping_cycle_autopilot_kind()
                return
            if key == pygame.K_COMMA:
                engine.mapping_adjust_autopilot_interval(-1.0)
                return
            if key == pygame.K_PERIOD:
                engine.mapping_adjust_autopilot_interval(1.0)
                return
            if key == pygame.K_b:
                engine.mapping_toggle_borders()
                return
            if key == pygame.K_LEFTBRACKET:
                engine.mapping_adjust_border_intensity(-0.1)
                return
            if key == pygame.K_RIGHTBRACKET:
                engine.mapping_adjust_border_intensity(0.1)
                return
            if key == pygame.K_SEMICOLON:
                engine.mapping_adjust_border_thickness(-1)
                return
            if key == pygame.K_QUOTE:
                engine.mapping_adjust_border_thickness(1)
                return
            if key == pygame.K_c:
                engine.mapping_cycle_border_color()
                return

    if key == pygame.K_SPACE:
        engine.toggle_blackout()
        return

    if key == pygame.K_BACKSPACE:
        engine.toggle_freeze()
        return

    # P → toggle control of the physical "mushroom" LED prop. ON tracks the
    # show's average colour onto it; OFF hands it back to its built-in effect.
    if key == pygame.K_p:
        engine.toggle_mushroom()
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
    # KEYDOWN handling here so a held key produces smooth motion.
    if key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
        return

    if key == CYCLE_CLIPS_PREV:
        engine.browse_clips("step", -1)
        return
    if key == CYCLE_CLIPS_NEXT:
        engine.browse_clips("step", 1)
        return
    if key == CYCLE_GENS_PREV:
        engine.browse_generatives(-1)
        return
    if key == CYCLE_GENS_NEXT:
        engine.browse_generatives(1)
        return

    # Live webcam: `\` toggles the camera as the base layer (or, in mapping
    # perform mode, as the selected group's content). Shift+\ flips the
    # selfie mirror. All existing FX/overlays/hits then run on the feed.
    if key == pygame.K_BACKSLASH:
        if mod & pygame.KMOD_SHIFT:
            engine.toggle_camera_mirror()
        else:
            engine.toggle_camera()
        return

    # Favourite keys (1-0, A-L, ;) are handled in Engine.run()'s long-press
    # logic — they don't go through dispatch at all.

    # Face point cloud (live mode only): backtick toggles it as the base
    # layer; Shift+backtick toggles the "two faces facing each other" view;
    # , / . step through the baked faces (and turn it on if it was off).
    # These keys are unused elsewhere in live mode.
    if engine.mode == "live":
        if key == pygame.K_BACKQUOTE:
            if mod & pygame.KMOD_SHIFT:
                engine.toggle_face_duo()
            else:
                engine.toggle_facecloud()
            return
        if key == pygame.K_COMMA:
            engine.cycle_face(-1)
            return
        if key == pygame.K_PERIOD:
            engine.cycle_face(1)
            return

    if key in HIT_KEYS:
        engine.fire_hit(HIT_KEYS[key], frames=5)
        return

    if key in FX_KEYS:
        engine.toggle_fx(FX_KEYS[key])
        return
