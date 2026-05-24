import argparse
import os
import sys

import pygame

from config import Config
from engine import Engine


def parse_args():
    p = argparse.ArgumentParser(description="pi-paint VJ — manual mode")
    p.add_argument("--width", type=int, default=854,
                   help="Output frame width (rendered, may be scaled to fullscreen)")
    p.add_argument("--height", type=int, default=480,
                   help="Output frame height")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--fullscreen", action="store_true",
                   help="Run the OUTPUT window fullscreen on --output-display")
    p.add_argument("--output-display", type=int, default=0,
                   help="Display index for the projector output (0 = primary)")
    p.add_argument("--control", action="store_true",
                   help="Also open a Control HUD window with live preview + keys")
    p.add_argument("--control-display", type=int, default=0,
                   help="Display index for the Control HUD window")
    p.add_argument("--control-size", default="680x720",
                   help="Control HUD size as WxH (default 680x720)")
    return p.parse_args()


def _open_output_window(cfg):
    flags = pygame.FULLSCREEN | pygame.SCALED if cfg.fullscreen else 0
    try:
        return pygame.display.set_mode(
            (cfg.width, cfg.height), flags, display=cfg.display
        )
    except TypeError:
        os.environ.setdefault("SDL_VIDEO_FULLSCREEN_DISPLAY", str(cfg.display))
        return pygame.display.set_mode((cfg.width, cfg.height), flags)


def _open_control_window(size, display_idx):
    """Create a second SDL2 window + renderer for the control HUD."""
    from pygame._sdl2.video import Window, Renderer
    # SDL's SDL_WINDOWPOS_CENTERED_DISPLAY(N) macro
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


def main():
    args = parse_args()
    cfg = Config(
        width=args.width, height=args.height, fps=args.fps,
        fullscreen=args.fullscreen, display=args.output_display,
    )

    pygame.init()
    pygame.font.init()

    output_screen = _open_output_window(cfg)
    pygame.display.set_caption("pi-paint VJ — Output")
    pygame.mouse.set_visible(not cfg.fullscreen)

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
    print("[vj] keys: 1-0 clips · QWERTY overlays · ASDFGHJKL generative · ZXCVB hits · F1-F7 FX · ←→↑↓ params · Space blackout · Esc kill · Shift+Esc quit")

    try:
        engine.run(control=control)
    finally:
        pygame.quit()
        sys.exit(0)


if __name__ == "__main__":
    main()
