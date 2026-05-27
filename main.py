import argparse
import os
import sys

import pygame

from config import Config
from engine import Engine
from state import load_state, save_state


def parse_args():
    p = argparse.ArgumentParser(description="pi-paint VJ — manual mode")
    p.add_argument("--width", type=int, default=1280,
                   help="Render width (output is scaled to display size, default 1280)")
    p.add_argument("--height", type=int, default=720,
                   help="Render height (default 720). 854x480 is the lighter Pi 4 default; "
                        "1920x1080 is full quality on Pi 5 if you have the headroom.")
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
    p.add_argument("--gen-render-scale", type=float, default=0.5,
                   help="Render generatives at this fraction of canvas "
                        "resolution and upscale on the way out (0.1..1.0, "
                        "default 0.5). 0.33 is great for smooth patterns; "
                        "1.0 disables the optimisation. Clips are unaffected.")
    return p.parse_args()


def _set_sdl_hints():
    """SDL behaviour hints — must be set BEFORE pygame.init().

    Output window position / display is handled by constructing the SDL2
    Window with `position=WINDOWPOS_CENTERED_DISPLAY(N)` (see
    _open_output_window), so no SDL_VIDEO_FULLSCREEN_DISPLAY or
    SDL_VIDEO_WINDOW_POS env vars needed any more.
    """
    # Don't grab the keyboard exclusively — the control HUD window needs
    # to receive its own clicks without focus getting yanked away.
    os.environ.setdefault("SDL_HINT_GRAB_KEYBOARD", "0")
    os.environ.setdefault("SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR", "0")


def _display_size(display_idx, fallback):
    """Pixel size of display `display_idx`, or `fallback` if unavailable."""
    try:
        sizes = pygame.display.get_desktop_sizes()
        return sizes[display_idx]
    except (pygame.error, IndexError, AttributeError):
        return fallback


def _open_output_window(cfg):
    """Open the output window as an SDL2 Window + Renderer pair.

    Returns (window, renderer). The renderer is hardware-accelerated by
    default on V3D, so the per-frame canvas → display upscale happens on
    the GPU instead of `pygame.transform.smoothscale` on the CPU.

    For "fullscreen" we open a *borderless window sized to the target
    display* rather than using SDL_WINDOW_FULLSCREEN. SDL2 has a long-
    standing bug (https://github.com/libsdl-org/SDL/issues/3192) where
    its fullscreen flags don't honour a specific display reliably and
    can't be moved between monitors at runtime. A borderless window
    centered on display N works on every SDL build and can be re-created
    on any monitor at runtime (see Engine.switch_output_display).
    """
    from pygame._sdl2.video import Window, Renderer
    if cfg.fullscreen:
        dw, dh = _display_size(cfg.display, (cfg.width, cfg.height))
        size = (dw, dh)
        borderless = True
    else:
        size = (cfg.width, cfg.height)
        borderless = False
    # SDL_WINDOWPOS_CENTERED_DISPLAY(N) = 0x2FFF0000 | N — places the new
    # window on a specific physical display without needing a separate
    # post-create move (which X11 occasionally drops on the floor).
    pos_val = 0x2FFF0000 | (cfg.display & 0xFFFF)
    try:
        win = Window(
            "pi-paint VJ — Output",
            size=size,
            position=(pos_val, pos_val),
            borderless=borderless,
        )
    except (TypeError, pygame.error, ValueError) as exc:
        print(f"[vj] Window(display={cfg.display}) failed: {exc!r}; "
              f"falling back to default display")
        win = Window(
            "pi-paint VJ — Output",
            size=size,
            borderless=borderless,
        )
    win.show()
    renderer = Renderer(win)
    return win, renderer


def _open_control_window(size, display_idx):
    """Create a second SDL2 window + renderer for the control HUD.

    The HUD renderer is forced to the SOFTWARE backend (accelerated=0).
    Why: gpu.py's standalone moderngl EGL context shares the V3D
    driver state with any GL-accelerated SDL_Renderer on the Pi, and
    we've observed the HUD turn solid black the moment moderngl
    initialises — V3D leaks driver state between contexts despite
    them being nominally independent. The output window stays on the
    hardware renderer (it's the larger framebuffer that benefits most
    from GPU scaling); the HUD goes through CPU rasterisation and
    presents through X with zero GL state to corrupt. ~680×720 of HUD
    pixels at 30 fps is trivial CPU work on Pi 5 — not a bottleneck.

    Falls back to the default (accelerated) renderer if SDL refuses
    accelerated=0 — older builds may not honour the flag, and on those
    setups moderngl probably isn't loading either so the conflict
    doesn't arise.
    """
    from pygame._sdl2.video import Window, Renderer
    centered_on_display = 0x2FFF0000 | (display_idx & 0xFFFF)
    win = Window(
        "VJ Control",
        size=size,
        position=(centered_on_display, centered_on_display),
        resizable=True,
    )
    win.show()
    try:
        renderer = Renderer(win, accelerated=0)
        print("[vj] control HUD: software renderer "
              "(avoids V3D GL state conflict with gpu.py)")
    except (TypeError, pygame.error):
        renderer = Renderer(win)
        print("[vj] control HUD: accelerated renderer "
              "(software fallback rejected by SDL)")
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
    _set_sdl_hints()

    pygame.init()
    pygame.font.init()

    # Validate the resolved display against what actually exists right now
    # (e.g. saved=1 but the projector isn't plugged in this time). Fall
    # back to 0 so the launcher never crashes on a stale state file.
    try:
        num = pygame.display.get_num_displays()
    except (pygame.error, AttributeError):
        num = 1
    if display >= num or display < 0:
        print(f"[vj] saved output display {display} not available "
              f"(only {num} display(s) attached); using 0")
        display = 0

    cfg = Config(
        width=args.width, height=args.height, fps=args.fps,
        fullscreen=args.fullscreen, display=display,
        gen_render_scale=max(0.1, min(1.0, args.gen_render_scale)),
    )

    output_window, output_renderer = _open_output_window(cfg)

    # NOTE: we *don't* try to move the window to the target display at
    # launch any more — the SDL pump loop took long enough that X11
    # marked the window as unresponsive and the WM force-closed it on
    # first click. If the window opened on the wrong monitor, the
    # operator can press F12 to move it after launch.

    engine = Engine(cfg, output_window, output_renderer)
    # Cursor visibility is mode-aware (hidden only in clean live fullscreen);
    # let the engine decide so a session that boots straight into mapping
    # mode from persisted state still shows the pointer over the projector.
    engine._apply_cursor_visibility()

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
    print("[vj] keys: -/= cycle clips · [/] cycle overlays · 1-0 clip favs (tap=play, hold=assign) · Q-P overlay favs · ASDFGHJKL generative · ZXCVB hits · F1-F7 FX · ←→↑↓ params · Enter Enter autopilot · F11/F12 display · Space blackout · Esc panic · Shift+Esc quit")

    try:
        engine.run(control=control)
    finally:
        pygame.quit()
        sys.exit(0)


if __name__ == "__main__":
    main()
