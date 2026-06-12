"""Offscreen projectM diagnostic.

Drives the real worker Renderer (surfaceless EGL — no window, no projector,
no HUD) over a sample of installed presets, ONE at a time and slowly, so it
never triggers the rapid-cycle shader-recompile storm that hangs V3D. For
each preset it reports whether projectM actually rendered non-black pixels,
whether the load failed (via projectM's switch-failed callback), and any GL
error. This tells us if MilkDrop renders at all on this Pi's GLES 3.1 / V3D,
and exactly which presets break — without risking the live display.

Run it through "Diagnose ProjectM.sh". Tunables:
  VJ_PM_DIAG_N     how many presets to sample (default 6, 0 = all)
  VJ_PM_SOFTWARE   1 = force the llvmpipe software renderer (cannot touch
                   V3D, so it can never freeze the display — but slow)
  VJ_PM_DIAG_SIZE  render size WxH (default 640x360 — lighter on the GPU)
"""
import os
import sys
import time

# Load presets immediately (no rate-limit) and stay on the synthetic beat
# unless a mic exists — deterministic for a one-shot diagnostic.
os.environ.setdefault("VJ_PM_SWITCH_MS", "0")
# Optional belt-and-braces: force software GL so the probe physically
# cannot lock up the V3D GPU / freeze the display.
if os.environ.get("VJ_PM_SOFTWARE") == "1":
    os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
    os.environ.setdefault("GALLIUM_DRIVER", "llvmpipe")

import numpy as np  # noqa: E402

import projectm_worker as W  # noqa: E402
from projectm_presets import PROJECTM_GENERATOR_ORDER, PRESET_DIR  # noqa: E402


def _sample(names, n):
    if n <= 0 or len(names) <= n:
        return list(names)
    step = len(names) / n
    return [names[int(i * step)] for i in range(n)]


def main():
    print(f"[diag] preset dir: {PRESET_DIR}")
    print(f"[diag] presets in cycle: {len(PROJECTM_GENERATOR_ORDER)}")
    if not PROJECTM_GENERATOR_ORDER:
        print("[diag] no presets installed — run 'Setup ProjectM.sh' first.")
        return 2

    try:
        n = int(os.environ.get("VJ_PM_DIAG_N", "6"))
    except ValueError:
        n = 6
    size = os.environ.get("VJ_PM_DIAG_SIZE", "640x360")
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        w, h = 640, 360

    names = _sample(PROJECTM_GENERATOR_ORDER, n)
    print(f"[diag] testing {len(names)} preset(s) at {w}x{h}, "
          f"{W.AUDIO_RATE}Hz synthetic/mic audio\n")

    renderer = W.Renderer()      # GL + projectM build lazily on first render
    pm = None

    ok = black = failed = errors = 0
    for i, name in enumerate(names, 1):
        if pm is not None:
            pm.last_fail = None
        last = None
        try:
            # ~15 frames so the smooth crossfade fades the preset fully in
            # before we judge brightness; paced to stay gentle on the GPU.
            for _ in range(15):
                last = renderer.render(name, w, h)
                if pm is None:
                    pm = renderer.projectm   # available after the first render
                time.sleep(0.02)
        except Exception as exc:
            print(f"[{i:2}/{len(names)}] {name:38} EXC {exc!r}")
            errors += 1
            continue

        glerr = renderer.gl.glGetError()
        arr = np.frombuffer(last, dtype=np.uint8)
        mean = float(arr.mean()) if arr.size else 0.0
        peak = int(arr.max()) if arr.size else 0

        if pm.last_fail is not None:
            fn, msg = pm.last_fail
            print(f"[{i:2}/{len(names)}] {name:38} FAILED — {msg}")
            failed += 1
        elif mean < 1.0:
            print(f"[{i:2}/{len(names)}] {name:38} BLACK (peak={peak})"
                  + (f" glErr=0x{glerr:04x}" if glerr else ""))
            black += 1
        else:
            print(f"[{i:2}/{len(names)}] {name:38} ok  mean={mean:5.1f} "
                  f"peak={peak}" + (f" glErr=0x{glerr:04x}" if glerr else ""))
            ok += 1
        if glerr:
            errors += 1
        time.sleep(0.1)   # gap between presets — no recompile storm

    print(f"\n[diag] summary: {ok} rendered, {black} black, "
          f"{failed} load-failed, {errors} with GL errors "
          f"(of {len(names)} tested)")
    if ok == 0:
        print("[diag] VERDICT: projectM renders nothing non-black on this "
              "GPU — MilkDrop on V3D/GLES 3.1 is the blocker, not the wiring.")
    elif black or failed:
        print("[diag] VERDICT: some presets work — the cycle needs filtering "
              "to the ones that render (and the rapid-cycle freeze fixed).")
    else:
        print("[diag] VERDICT: all sampled presets render — the black screen "
              "is in the live wiring/pacing, not projectM itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
