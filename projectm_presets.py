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
# Operator "banished" presets — one .milk filename per line. Removed live from
# the HUD with the Delete key (see Engine.remove_current_generator). Filtered
# out of the cycle here AND skipped by projectm_curate.py, so a killed preset
# never comes back, not even on a re-curate. Restored with Shift+Delete.
REMOVED = HERE / "projectm_removed.txt"
PM_PREFIX = "pm:"


def removed_names():
    """Set of lowercased .milk filenames the operator has banished."""
    try:
        lines = REMOVED.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    return {ln.strip().lower() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")}


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
    banished = removed_names()
    if banished:
        files = [f for f in files if f.name.lower() not in banished]
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


def filename_for(name):
    """The .milk filename behind a 'pm:<stem>' cycle name, or None."""
    path = PROJECTM_GENERATORS.get(name)
    return Path(path).name if path else None


def _strip_from_playlist(filename):
    """Drop any playlist line that names this preset (so the curated 70 loses
    it too). Matches the curator's own output format (one filename per line)."""
    try:
        lines = PLAYLIST.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    fl = filename.lower()
    kept = [ln for ln in lines
            if ln.strip().startswith("#") or not ln.strip()
            or ln.strip().lower() != fl]
    if len(kept) != len(lines):
        PLAYLIST.write_text("\n".join(kept) + "\n", encoding="utf-8")


def banish(name):
    """Append the preset behind `name` to projectm_removed.txt and strip it
    from the playlist. Returns the .milk filename banished, or None if `name`
    isn't a known pm: preset. Idempotent — a double-tap won't duplicate it."""
    filename = filename_for(name)
    if not filename:
        return None
    if filename.lower() not in removed_names():
        with REMOVED.open("a", encoding="utf-8") as fh:
            fh.write(filename + "\n")
    _strip_from_playlist(filename)
    return filename


def restore(filename):
    """Remove `filename` from projectm_removed.txt (the Shift+Delete undo).
    Returns True if it was present and removed. Does NOT re-add it to the
    playlist — the curator owns that; the preset simply rejoins the sample."""
    if not filename:
        return False
    try:
        lines = REMOVED.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    fl = filename.lower()
    kept = [ln for ln in lines if ln.strip().lower() != fl]
    if len(kept) == len(lines):
        return False
    body = "\n".join(kept)
    REMOVED.write_text((body + "\n") if body.strip() else "", encoding="utf-8")
    return True
