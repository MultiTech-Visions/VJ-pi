import argparse
import os
import sys

import pygame

from config import Config
from engine import Engine
from state import load_state, save_state


def parse_args():
    p = argparse.ArgumentParser(description="pi-paint VJ — manual mode")
    p.add_argument("--width", type=int, default=854,
                   help="Output frame width (rendered, may be scaled to fullscreen)")
    p.add_argument("--height", type=int, default=480,
                   help="Output frame height")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--fullscreen", action="store_true",
                   help="Run the OUTPUT window fullscreen on the chosen display")
    p.add_argument("--output-display", type=int, default=None,
                   help="Display index for the projector output (overrides saved state)")
    p.add_argument("--control", action="store_true",
                   help="Also open a Control HUD window with live preview + keys")
    p.add_argument("--control-display", type=int, default=0,
                   help="Display index for the Control HUD window")
    p.add_argument("--control-size", default="680x720",
                   help="Control HUD size as WxH (default 680x720)")
    return p.parse_args()


def _set_sdl_hints(display_idx):
    """Tell SDL where the output window goes — must run BEFORE pygame.init().

    `display=N` on pygame.display.set_mode() is unreliable for fullscreen on
    several pygame/SDL combos; the env-var route works consistently.
    """
    os.environ["SDL_VIDEO_FULLSCREEN_DISPLAY"] = str(display_idx)
    # SDL_WINDOWPOS_CENTERED_DISPLAY(N) — also use this in windowed mode
    # so the non-fullscreen output window opens on the chosen display.
    centered = 0x2FFF0000 | (display_idx & 0xFFFF)
    os.environ.setdefault("SDL_VIDEO_WINDOW_POS", f"{centered},{centered}")
    # Don't let the fullscreen window steal keyboard focus from other windows
    # on the same X server — this is what makes clicks on the control HUD
    # actually land on the control HUD instead of yanking focus back.
    os.environ.setdefault("SDL_HINT_GRAB_KEYBOARD", "0")
    os.environ.setdefault("SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR", "0")


def _open_output_window(cfg):
    flags = pygame.FULLSCREEN | pygame.SCALED if cfg.fullscreen else 0
    try:
        return pygame.display.set_mode(
            (cfg.width, cfg.height), flags, display=cfg.display
        )
    except TypeError:
        return pygame.display.set_mode((cfg.width, cfg.height), flags)


def _open_control_window(size, display_idx):
    """Create a second SDL2 window + renderer for the control HUD."""
    from pygame._sdl2.video import Window, Renderer
    centered_on_display = 0x2FFF0000 | (display_idx & 0xFFFF)
    win = Window(
        "VJ Control",
        size=size,
        position=(centered_on_display, centered_on_display),
        resizable=True,
    )
    win.show()
    renderer = Renderer(win)
    return win, renderer


def _resolve_output_display(args):
    """Persisted display choice wins once set; CLI seeds it on first run.

    The HUD picker writes vj_state.json on every apply, so once the operator
    picks display N, that choice sticks across launches and across the
    different launcher scripts. To override a saved choice from the command
    line, delete vj_state.json (or edit its `output_display` key by hand).
    """
    state = load_state()
    saved = state.get("output_display")
    if isinstance(saved, int) and saved >= 0:
        return saved
    fallback = args.output_display if args.output_display is not None else 0
    save_state({**state, "output_display": fallback})
    return fallback


def main():
    args = parse_args()
    display = _resolve_output_display(args)
    _set_sdl_hints(display)

    cfg = Config(
        width=args.width, height=args.height, fps=args.fps,
        fullscreen=args.fullscreen, display=display,
    )

    pygame.init()
    pygame.font.init()

    output_screen = _open_output_window(cfg)
    pygame.display.set_caption("pi-paint VJ — Output")
    pygame.mouse.set_visible(not cfg.fullscreen)

    # Belt and braces: SDL_VIDEO_FULLSCREEN_DISPLAY env var is set above but
    # not every pygame/SDL combo honours it, so explicitly re-park the
    # window on the requested display via SDL2 if we ended up elsewhere.
    if cfg.display != 0:
        try:
            from display_helpers import move_main_window_to_display
            move_main_window_to_display(
                cfg.display, (cfg.width, cfg.height), fullscreen=cfg.fullscreen,
            )
            output_screen = pygame.display.get_surface() or output_screen
        except Exception as exc:
            print(f"[vj] could not park output on display {cfg.display}: {exc!r}")

    engine = Engine(cfg, output_screen)

    control = None
    if args.control:
        from control import ControlWindow
        try:
            ctrl_w, ctrl_h = (int(x) for x in args.control_size.lower().split("x"))
        except ValueError:
            print(f"[vj] bad --control-size {args.control_size!r}, using 680x720")
            ctrl_w, ctrl_h = 680, 720
        ctrl_win, ctrl_renderer = _open_control_window((ctrl_w, ctrl_h), args.control_display)
        preview_w = ctrl_w - 24
        preview_h = int(preview_w * cfg.height / cfg.width)
        control = ControlWindow(
            engine, ctrl_win, ctrl_renderer,
            size=(ctrl_w, ctrl_h),
            preview_size=(preview_w, preview_h),
        )

    print(f"[vj] output:      {cfg.width}x{cfg.height} fullscreen={cfg.fullscreen} display={cfg.display}")
    if control is not None:
        print(f"[vj] control HUD: display {args.control_display}, size {args.control_size}")
    print(f"[vj] clips dir:    {cfg.clips_dir}")
    print(f"[vj] overlays dir: {cfg.overlays_dir}")
    print(f"[vj] {len(engine.clips)} clip(s), {len(engine.overlays)} overlay(s) loaded")
    print("[vj] keys: -/= cycle clips · [/] cycle overlays · 1-0 clip favs (tap=play, hold=assign) · Q-P overlay favs · ASDFGHJKL generative · ZXCVB hits · F1-F7 FX · ←→↑↓ params · F11/F12 display · Space blackout · Esc panic (keeps clip) · Shift+Esc quit")

    try:
        engine.run(control=control)
    finally:
        pygame.quit()
        sys.exit(0)


if __name__ == "__main__":
    main()
