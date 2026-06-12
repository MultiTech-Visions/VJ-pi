"""Catalogue of MilkDrop presets served by the projectM worker.

Presets are `.milk` files under assets/projectm_presets/ (gitignored
operator data, installed by "Setup ProjectM.sh" from a pack pre-filtered
to hold >=24fps on a Pi 5). Each preset becomes a generator named
"pm:<filename stem>" that slots into the normal [/] cycle, favourites,
autopilot and mapping groups exactly like the GLSL generators.

The full pack is large, so the cycle takes an evenly-spaced sample of at
most VJ_PM_MAX presets (default 40; 0 = all). To hand-curate instead,
create projectm_playlist.txt next to this file with one preset filename
(or stem, or unique substring) per line; lines starting with # are
comments. The playlist wins over the sample when it matches anything.

Importable without libprojectM — the main process only needs the names;
only projectm_worker.py touches GL or the library.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRESET_DIR = HERE / "assets" / "projectm_presets"
TEXTURE_DIR = HERE / "assets" / "projectm_textures"
PLAYLIST = HERE / "projectm_playlist.txt"
PM_PREFIX = "pm:"


def _max_presets():
    try:
        return max(0, int(os.environ.get("VJ_PM_MAX", "40")))
    except ValueError:
        return 40


def _playlist_pick(files):
    try:
        lines = PLAYLIST.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    wanted = [ln.strip() for ln in lines
              if ln.strip() and not ln.strip().startswith("#")]
    picked = []
    for w in wanted:
        wl = w.lower()
        hit = next((f for f in files
                    if f.name.lower() == wl or f.stem.lower() == wl), None)
        if hit is None:
            hit = next((f for f in files if wl in f.name.lower()), None)
        if hit is not None and hit not in picked:
            picked.append(hit)
    return picked


def _scan():
    if not PRESET_DIR.exists():
        return {}
    files = sorted(PRESET_DIR.rglob("*.milk"))
    if not files:
        return {}
    picked = _playlist_pick(files) if PLAYLIST.exists() else []
    if not picked:
        limit = _max_presets()
        if limit and len(files) > limit:
            step = len(files) / limit
            picked = [files[int(i * step)] for i in range(limit)]
        else:
            picked = files
    out = {}
    for f in picked:
        name = PM_PREFIX + f.stem
        n, i = name, 2
        while n in out:
            n = f"{name}#{i}"
            i += 1
        out[n] = str(f)
    return out


PROJECTM_GENERATORS = _scan()
PROJECTM_GENERATOR_ORDER = list(PROJECTM_GENERATORS.keys())
