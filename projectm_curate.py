"""Score MilkDrop presets and write a curated projectm_playlist.txt.

There are ~8000 presets installed; the cycle otherwise samples 40 at random,
so most are the near-black / static duds the operator sees. This renders a
pool of candidates offscreen, measures how much each one actually *does*
(brightness, lit-area, and frame-to-frame motion with a synthetic beat), and
writes the liveliest ones to projectm_playlist.txt — which projectm_presets.py
prefers over the random sample. Re-run any time to re-curate.

Progress (integer percent + "# label" lines) goes to STDOUT so a zenity
--progress bar can read it; the human-readable report goes to STDERR (the
launcher tees that to vj_last_projectm_curate.log).

Tunables (env):
  VJ_PM_CURATE_POOL  candidates to score, evenly spaced over all presets
                     (default 1000; 0 = every preset — slow)
  VJ_PM_CURATE_KEEP  how many to write to the playlist (default 70)
  VJ_PM_SOFTWARE     1 = force llvmpipe (safe, cannot freeze V3D; slower)
  VJ_PM_CURATE_SIZE  render size WxH (default 480x270)
"""
import ctypes
import os
import sys
from ctypes import POINTER, c_int16, c_void_p

if os.environ.get("VJ_PM_SOFTWARE") == "1":
    os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
    os.environ.setdefault("GALLIUM_DRIVER", "llvmpipe")

import numpy as np

import projectm_worker as W
from projectm_presets import PRESET_DIR, PM_PREFIX, removed_names


def out(msg):       # progress channel (zenity reads stdout)
    print(msg, flush=True)


def rep(msg):       # human report (teed to the log)
    print(msg, file=sys.stderr, flush=True)


def _intenv(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _synth_pcm(handle, pm, t0):
    fr = 1470
    t = t0 + np.arange(fr) / 44100.0
    beat = (t * 2.0) % 1.0
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-beat * 9.0)
    mono = np.clip(kick * 0.8 + np.random.randn(fr) * 0.1, -1, 1)
    st = np.empty(fr * 2, np.int16)
    st[0::2] = st[1::2] = (mono * 32767).astype(np.int16)
    pm.projectm_pcm_add_int16(handle, st.ctypes.data_as(POINTER(c_int16)), fr, 2)
    return t[-1] + 1.0 / 44100.0


def main():
    files = sorted(PRESET_DIR.rglob("*.milk"))
    if not files:
        rep("[curate] no presets found — run 'Setup ProjectM.sh' first.")
        return 2
    # Honour the operator's HUD-banished presets: never score or re-list a
    # preset they killed with Delete in the show. This is what makes the kill
    # permanent across re-curates.
    banished = removed_names()
    if banished:
        before = len(files)
        files = [f for f in files if f.name.lower() not in banished]
        rep(f"[curate] skipping {before - len(files)} operator-removed presets "
            f"(projectm_removed.txt)")
    pool_n = _intenv("VJ_PM_CURATE_POOL", 1500)
    keep = _intenv("VJ_PM_CURATE_KEEP", 120)
    size = os.environ.get("VJ_PM_CURATE_SIZE", "480x270")
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        w, h = 480, 270

    if pool_n and len(files) > pool_n:
        step = len(files) / pool_n
        pool = [files[int(i * step)] for i in range(pool_n)]
    else:
        pool = files
    rep(f"[curate] {len(files)} presets installed; scoring {len(pool)} "
        f"at {w}x{h}; keeping top {keep}")

    egl = W.EglContext(w, h)               # pbuffer (FBO 0 exists)
    gl = W.Gl().g
    pm = W.ProjectM()
    pm.pm.projectm_set_window_size(pm.handle, w, h)
    gl.glGetString.restype = ctypes.c_char_p
    rep(f"[curate] renderer: {gl.glGetString(0x1F01).decode()}")

    FRAMES, WARMUP = 16, 5
    buf = np.empty((h, w, 4), np.uint8)
    scored, failed = [], 0
    t0 = 0.0
    last_pct = -1
    for i, f in enumerate(pool):
        pm.last_fail = None
        pm.pm.projectm_load_preset_file(pm.handle, str(f).encode(), False)
        prev = None
        bright = lit = motion = 0.0
        nf = 0
        for fi in range(FRAMES):
            t0 = _synth_pcm(pm.handle, pm.pm, t0)
            gl.glBindFramebuffer(0x8D40, 0)
            gl.glViewport(0, 0, w, h)
            pm.pm.projectm_opengl_render_frame(pm.handle)
            gl.glBindFramebuffer(0x8D40, 0)
            gl.glReadPixels(0, 0, w, h, 0x1908, 0x1401, buf.ctypes.data_as(c_void_p))
            if fi < WARMUP:
                continue
            rgb = buf[:, :, :3].astype(np.int16)
            luma = rgb.mean(axis=2)
            bright += float(luma.mean())
            lit += float((luma > 16).mean())
            if prev is not None:
                motion += float(np.abs(rgb - prev).mean())
            prev = rgb
            nf += 1
        if pm.last_fail is not None:
            failed += 1
        elif nf:
            bright /= nf
            lit /= nf
            motion /= max(1, nf - 1)
            # Visual-activity score: motion matters most, then how much of the
            # frame is lit, with a little credit for overall brightness.
            score = motion * 3.0 + lit * 60.0 + bright * 0.15
            dead = motion < 0.6 and lit < 0.04      # ~static & ~black
            if not dead:
                scored.append((score, f, bright, lit, motion))
        pct = int((i + 1) * 100 / len(pool))
        if pct != last_pct:
            last_pct = pct
            out(pct)
            out(f"# scored {i + 1}/{len(pool)} · kept {len(scored)} · "
                f"dropped {i + 1 - len(scored) - failed} · failed {failed}")

    scored.sort(key=lambda r: r[0], reverse=True)
    winners = scored[:keep]
    playlist = W.HERE / "projectm_playlist.txt"
    lines = ["# Auto-curated by projectm_curate.py — liveliest presets first.",
             "# Delete a line to drop that preset; re-run the curator to rebuild.",
             f"# scored {len(pool)}, kept {len(winners)} (of {len(scored)} "
             f"non-dead, {failed} load-failed).", ""]
    lines += [f.name for (_s, f, *_m) in winners]
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rep("")
    rep(f"[curate] wrote {len(winners)} presets to {playlist.name}")
    rep("[curate] top 15 by visual activity:")
    for s, f, b, lit, mo in winners[:15]:
        rep(f"  {s:7.1f}  mot={mo:5.1f} lit={lit:4.2f} brt={b:5.1f}  "
            f"{PM_PREFIX}{f.stem[:46]}")
    rep(f"\n[curate] summary: {len(winners)} kept, "
        f"{len(scored) - len(winners)} good-but-trimmed, "
        f"{len(pool) - len(scored) - failed} dropped as dead/black, "
        f"{failed} load-failed.")
    out(100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
