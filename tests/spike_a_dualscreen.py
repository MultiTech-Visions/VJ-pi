"""Spike A — dual-screen V3D survival test (production stack, no moderngl).

THE QUESTION
------------
The whole GPU history of this project died on one thing: two GL contexts
coexisting in ONE process corrupt each other on V3D (one surface goes
solid black). The hybrid sidesteps it by keeping all GL in a separate
worker process. The `--gpu-scale` output path sidesteps it a different
way: put the ONE GL context on the projector (an _sdl2 Renderer that
hardware-scales the canvas) and keep the HUD as a *software*
pygame.display window (no GL). "Exactly one GL context, the V3D rule
holds."

This spike proves that arrangement in isolation — none of the engine,
clips, FX, or worker machinery — so if it ever misbehaves on real
hardware we can bisect cleanly. It opens BOTH windows the way the real
app does under `--gpu-scale`:

  * OUTPUT  : _sdl2 Window + Renderer + streaming Texture on the
              projector display (the single GL context). Animated.
  * CONTROL : a plain pygame.display software window on the operator
              display. Also animated.

Both draw a fast-moving pattern so a freeze or a blackout is obvious to
the eye, and both print a per-second heartbeat FPS so it's obvious in
the log too. If after 60 s neither window has gone black and both FPS
counters are still ticking, the gpu-scale window layout is sound on this
machine and Spike A passes.

RUN (on the Pi, two displays)
-----------------------------
  ./venv/bin/python tests/spike_a_dualscreen.py \
      --output-display 1 --control-display 0 --seconds 60 --fullscreen

Single screen (both windows on display 0, for a quick smoke test):
  ./venv/bin/python tests/spike_a_dualscreen.py

WHAT TO REPORT BACK
-------------------
  * Did EITHER window ever go solid black / freeze? (the failure mode)
  * The final heartbeat lines for both OUTPUT and CONTROL.
  * Any '[spike-a]' error lines.
"""
import argparse
import sys
import time

import numpy as np
import pygame


def _window_pos_for(display_idx):
    """SDL "centered on display N" magic position, same as engine.py."""
    return (0x2FFF0000 | (display_idx & 0xFFFF),
            0x2FFF0000 | (display_idx & 0xFFFF))


def _moving_canvas(w, h, t):
    """A bright moving gradient + sweeping bar. Cheap, but any freeze or
    black-out is instantly visible. Returns an (h, w, 3) uint8 RGB array."""
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    r = (0.5 + 0.5 * np.sin(6.28 * (xs + t * 0.15)))
    g = (0.5 + 0.5 * np.sin(6.28 * (ys + t * 0.11)))
    b = (0.5 + 0.5 * np.sin(6.28 * (xs + ys + t * 0.07)))
    img = np.stack([r, g, b], axis=-1)
    # A hard white sweeping bar so a stalled frame is unmistakable.
    bar = int((t * 0.5 % 1.0) * w)
    img[:, max(0, bar - 3):bar + 3, :] = 1.0
    return (img * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description="Spike A: dual-screen V3D survival")
    ap.add_argument("--output-display", type=int, default=0)
    ap.add_argument("--control-display", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--canvas", default="1280x720",
                    help="Render/upload canvas size (default 1280x720). The "
                         "GPU renderer scales this to the output window.")
    ap.add_argument("--fullscreen", action="store_true",
                    help="Size the OUTPUT window to the whole projector display.")
    args = ap.parse_args()

    cw, ch = (int(x) for x in args.canvas.lower().split("x"))

    # Don't let SDL grab the keyboard exclusively (matches the app).
    import os
    os.environ.setdefault("SDL_HINT_GRAB_KEYBOARD", "0")
    pygame.init()

    # --- CONTROL (software) window FIRST, exactly like main.py under
    # --- --gpu-scale, so the OUTPUT renderer is the ONLY GL context. -----
    try:
        ctrl = pygame.display.set_mode((680, 720), 0, args.control_display)
        pygame.display.set_caption("Spike A — CONTROL (software)")
        print(f"[spike-a] control: software window on display "
              f"{args.control_display}", flush=True)
    except pygame.error as exc:
        print(f"[spike-a] FAILED to open control window: {exc!r}", flush=True)
        return 1

    # --- OUTPUT: _sdl2 GPU window + renderer (the single GL context) -----
    try:
        from pygame._sdl2.video import Window, Renderer, Texture
        if args.fullscreen:
            try:
                dw, dh = pygame.display.get_desktop_sizes()[args.output_display]
            except (pygame.error, IndexError, AttributeError):
                dw, dh = cw, ch
            size = (dw, dh)
        else:
            size = (cw, ch)
        win = None
        for kwargs in (
            {"size": size, "position": _window_pos_for(args.output_display),
             "borderless": bool(args.fullscreen)},
            {"size": size, "position": _window_pos_for(args.output_display)},
            {"size": size},
        ):
            try:
                win = Window("Spike A — OUTPUT (GPU)", **kwargs)
                break
            except TypeError:
                continue
        if win is None:
            raise RuntimeError("could not construct _sdl2 Window")
        win.show()
        renderer = Renderer(win)
        try:
            renderer.logical_size = (cw, ch)
        except Exception:
            pass
        tex = Texture(renderer, (cw, ch), streaming=True)
        print(f"[spike-a] output: _sdl2 GPU renderer on display "
              f"{args.output_display}, window {size}, canvas {cw}x{ch}",
              flush=True)
    except Exception as exc:
        import traceback
        print(f"[spike-a] FAILED to init GPU output: {exc!r}", flush=True)
        traceback.print_exc()
        return 1

    print("[spike-a] both windows up — watch for either going BLACK/frozen. "
          f"Running {args.seconds:.0f}s. Ctrl-C to stop early.", flush=True)

    t0 = time.perf_counter()
    last_beat = t0
    out_frames = 0
    ctrl_frames = 0
    clock = pygame.time.Clock()
    try:
        while True:
            now = time.perf_counter()
            t = now - t0
            if t >= args.seconds:
                break
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    raise KeyboardInterrupt
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    raise KeyboardInterrupt

            frame = _moving_canvas(cw, ch, t)

            # OUTPUT present (GPU path, mirrors engine._blit_gpu).
            surf = pygame.image.frombuffer(np.ascontiguousarray(frame),
                                           (cw, ch), "RGB")
            tex.update(surf)
            renderer.clear()
            tex.draw()
            renderer.present()
            out_frames += 1

            # CONTROL present (software path): a smaller, differently-phased
            # version so the two windows are visibly independent.
            small = _moving_canvas(680, 720, t * 1.3 + 2.0)
            ctrl_surf = pygame.image.frombuffer(small, (680, 720), "RGB")
            ctrl.blit(ctrl_surf, (0, 0))
            pygame.display.flip()
            ctrl_frames += 1

            if now - last_beat >= 1.0:
                dt = now - last_beat
                print(f"[spike-a] t={t:5.1f}s  OUTPUT={out_frames / dt:5.1f}fps  "
                      f"CONTROL={ctrl_frames / dt:5.1f}fps", flush=True)
                last_beat = now
                out_frames = 0
                ctrl_frames = 0

            clock.tick(60)
    except KeyboardInterrupt:
        print("[spike-a] stopped by user", flush=True)
    finally:
        pygame.quit()

    print("[spike-a] DONE. If neither window went black and both FPS kept "
          "ticking, the gpu-scale dual-screen layout survives on this Pi.",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
