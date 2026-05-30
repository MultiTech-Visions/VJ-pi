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
    p.add_argument("--fx-render-scale", type=float, default=0.5,
                   help="Run per-group FX at this fraction of canvas "
                        "(0.1..1.0, default 0.5). Big speedup for heavy FX "
                        "like kaleidoscope in mapping mode (the result is "
                        "warped onto a quad anyway); 1.0 = full-res FX.")
    p.add_argument("--mapping-threads", type=int, default=0,
                   help="Parallelise per-group FX + warp in mapping mode "
                        "across this many threads (default 0 = auto: number "
                        "of cores, capped at 4). Use 1 to force serial.")
    p.add_argument("--display-filter", choices=("linear", "cubic"),
                   default="linear",
                   help="Interpolation for the final upscale to the display "
                        "(default linear = faster; cubic = sharper, slower). "
                        "Ignored under --gpu-scale (the GPU does the scaling).")
    p.add_argument("--gpu-scale", action="store_true",
                   help="Scale output to the projector on the GPU instead of "
                        "the CPU — makes the 'disp' cost ~independent of "
                        "projector resolution (crisp 2K/4K cheaply). The "
                        "control HUD then renders in software. One GL context "
                        "either way.")
    return p.parse_args()


def _set_sdl_hints():
    """SDL behaviour hints — must be set BEFORE pygame.init().

    Output window position / display is handled via set_mode(display=N) and
    NOFRAME (see _open_output_window), so no SDL_VIDEO_FULLSCREEN_DISPLAY
    or SDL_VIDEO_WINDOW_POS needed any more.
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
    """Open the output window.

    For "fullscreen" we open a *borderless window sized to the target
    display* rather than using SDL_WINDOW_FULLSCREEN. SDL2 has a long-
    standing bug (https://github.com/libsdl-org/SDL/issues/3192) where
    its fullscreen flags don't honour a specific display reliably and
    can't be moved between monitors at runtime. A NOFRAME window with
    `display=N` and size = display N's resolution works on every SDL
    build and can be re-opened on any monitor by calling set_mode again.

    Manual frame scaling already happens in Engine.blit_to_output, so
    rendering at 854x480 onto a display-sized surface is fine.
    """
    if cfg.fullscreen:
        dw, dh = _display_size(cfg.display, (cfg.width, cfg.height))
        flags = pygame.NOFRAME
        size = (dw, dh)
    else:
        flags = 0
        size = (cfg.width, cfg.height)
    try:
        return pygame.display.set_mode(size, flags, display=cfg.display)
    except (TypeError, pygame.error, ValueError) as exc:
        print(f"[vj] set_mode(display={cfg.display}) failed: {exc!r}; "
              f"falling back to default display")
        return pygame.display.set_mode(size, flags)


def _open_control_window_bare(size, display_idx):
    """Create the control HUD window WITHOUT a renderer (decided later)."""
    from pygame._sdl2.video import Window
    centered_on_display = 0x2FFF0000 | (display_idx & 0xFFFF)
    win = Window(
        "VJ Control",
        size=size,
        position=(centered_on_display, centered_on_display),
        resizable=True,
    )
    win.show()
    return win


def _window_software_surface_works(win):
    """True if this pygame build can present `win` via a software surface
    (get_surface + flip) — the path the HUD needs when the OUTPUT owns the
    single GL renderer under --gpu-scale.

    The repo historically notes SDL2 Window has no get_surface(); newer
    pygame-ce builds added it. Rather than guess, probe once at startup so
    --gpu-scale only takes the software-HUD path when it genuinely works,
    and otherwise falls back to the proven renderer-on-HUD path.
    """
    try:
        surf = win.get_surface()
        win.flip()
        return surf is not None
    except Exception:
        return False


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
        fx_render_scale=max(0.1, min(1.0, args.fx_render_scale)),
        mapping_threads=(args.mapping_threads if args.mapping_threads >= 1
                         else min(4, os.cpu_count() or 1)),
        display_filter=args.display_filter,
    )

    output_screen = _open_output_window(cfg)
    pygame.display.set_caption("pi-paint VJ — Output")
    pygame.mouse.set_visible(True)  # always show cursor

    # NOTE: we *don't* try to move the window to the target display at
    # launch any more — the SDL pump loop took long enough that X11
    # marked the window as unresponsive and the WM force-closed it on
    # first click. If pygame opened the window on the wrong monitor,
    # the operator can press F12 to move it after launch.

    engine = Engine(cfg, output_screen)

    # --gpu-scale wants the single SDL renderer (one GL context — the V3D
    # rule) on the OUTPUT window. With a HUD too, that only works if this
    # pygame build can present the HUD window via a software surface; some
    # builds have no Window.get_surface(). So when both are requested we
    # PROBE the HUD's software path first and only commit to GPU output if it
    # works — otherwise we fall back to the proven path (renderer on HUD,
    # CPU-scaled output) so the HUD is never left blank.
    from pygame._sdl2.video import Renderer

    control = None
    gpu_out_ok = False
    if args.control:
        from control import ControlWindow
        try:
            ctrl_w, ctrl_h = (int(x) for x in args.control_size.lower().split("x"))
        except ValueError:
            print(f"[vj] bad --control-size {args.control_size!r}, using 680x720")
            ctrl_w, ctrl_h = 680, 720
        ctrl_win = _open_control_window_bare((ctrl_w, ctrl_h), args.control_display)

        ctrl_renderer = None
        if args.gpu_scale and _window_software_surface_works(ctrl_win):
            # HUD can run in software → give the renderer to the output.
            gpu_out_ok = engine.init_gpu_output()
        if args.gpu_scale and not gpu_out_ok:
            print("[vj] --gpu-scale: HUD has no software-surface path on this "
                  "build; keeping renderer on the HUD (CPU-scaled output)")
        if not gpu_out_ok:
            ctrl_renderer = Renderer(ctrl_win)   # proven path: HUD owns the GL

        preview_w = ctrl_w - 24
        preview_h = int(preview_w * cfg.height / cfg.width)
        control = ControlWindow(
            engine, ctrl_win, ctrl_renderer,
            size=(ctrl_w, ctrl_h),
            preview_size=(preview_w, preview_h),
        )
    elif args.gpu_scale:
        # Output only, no HUD → no second window, always safe.
        gpu_out_ok = engine.init_gpu_output()

    print(f"[vj] output:      {cfg.width}x{cfg.height} fullscreen={cfg.fullscreen} display={cfg.display}")
    if control is not None:
        print(f"[vj] control HUD: display {args.control_display}, size {args.control_size}")
    print(f"[vj] clips dir:    {cfg.clips_dir}")
    print(f"[vj] {len(engine.clips)} clip(s) loaded")
    print("[vj] keys: -/= cycle clips · [/] cycle generators · 1-0 clip favs (tap=play, hold=assign) · ASDFGHJKL; generator favs · ZXCVB hits · F1-F7 FX · ←→↑↓ params · Enter Enter autopilot · F11/F12 display · Space blackout · Esc panic · Shift+Esc quit")

    try:
        engine.run(control=control)
    finally:
        pygame.quit()
        sys.exit(0)


if __name__ == "__main__":
    main()
