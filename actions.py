"""Central action registry — the one place that maps an action name to an
engine method. Both the keyboard (`keymap.py`) and the web control panel
(`web.py`) dispatch through here so they can never drift apart.

An "action" is a small JSON-able dict:

    {"name": "fire_hit", "args": {"kind": "strobe"}}

The web layer accepts these as POST bodies; the keyboard layer builds
them in `keymap.dispatch()` and posts them as pygame USEREVENTs (which
the engine main loop then runs via `run(engine, action)`).

Why a registry instead of `getattr(engine, name)`? Three reasons:
  1. Whitelisting — only the listed actions are reachable from the web,
     so a malformed/hostile POST can't reach internals.
  2. Argument coercion — every action declares its arg shape, so we can
     reject bad input from the network in one place.
  3. Smooth params — PARAM X/Y are arrow-keys-while-held on the keyboard
     side, but they're sliders on the phone side. The registry exposes
     `set_param_x` / `set_param_y` for direct values, and the keyboard
     path still uses `engine.update_params_from_keys()` unchanged.
"""
from __future__ import annotations

from typing import Any, Callable

from engine import GENERATIVES, FX_TOGGLES


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── Action implementations ───────────────────────────────────────────
#
# Each takes (engine, args_dict) and runs synchronously on the main
# thread. Keep them tiny — they're called from the pygame event loop.

def _fire_hit(e, a):
    kind = a.get("kind")
    if kind in ("strobe", "black_flash", "invert_flash", "zoom_punch", "rgb_smash"):
        e.fire_hit(kind, frames=_int(a.get("frames", 5), 5))


def _toggle_fx(e, a):
    name = a.get("name")
    if name in FX_TOGGLES:
        e.toggle_fx(name)


def _select_generative(e, a):
    name = a.get("name")
    if name in GENERATIVES:
        e.select_generative(GENERATIVES.index(name))
    else:
        idx = a.get("idx")
        if isinstance(idx, int):
            e.select_generative(idx)


def _browse(pool_attr: str):
    def impl(e, a):
        action = a.get("action", "step")
        arg = a.get("arg")
        if pool_attr == "clips":
            e.browse_clips(action, arg)
        else:
            e.browse_overlays(action, arg)
    return impl


def _play_clip_favorite(e, a):
    e.play_clip_favorite(_int(a.get("slot")))


def _save_clip_favorite(e, a):
    e.save_clip_favorite(_int(a.get("slot")))


def _play_overlay_favorite(e, a):
    e.play_overlay_favorite(_int(a.get("slot")))


def _save_overlay_favorite(e, a):
    e.save_overlay_favorite(_int(a.get("slot")))


def _set_param_x(e, a):
    e.param_x = _clamp01(a.get("value"))


def _set_param_y(e, a):
    e.param_y = _clamp01(a.get("value"))


def _nudge_param_x(e, a):
    e.param_x = _clamp01(e.param_x + float(a.get("delta", 0.0)))


def _nudge_param_y(e, a):
    e.param_y = _clamp01(e.param_y + float(a.get("delta", 0.0)))


def _toggle_blackout(e, _a):
    e.toggle_blackout()


def _toggle_freeze(e, _a):
    e.toggle_freeze()


def _kill_all(e, _a):
    e.kill_all()


def _cycle_pending_display(e, _a):
    e.cycle_pending_display()


def _apply_pending_display(e, _a):
    e.apply_pending_display()


ACTIONS: dict[str, Callable] = {
    "fire_hit":                 _fire_hit,
    "toggle_fx":                _toggle_fx,
    "select_generative":        _select_generative,
    "browse_clips":             _browse("clips"),
    "browse_overlays":          _browse("overlays"),
    "play_clip_favorite":       _play_clip_favorite,
    "save_clip_favorite":       _save_clip_favorite,
    "play_overlay_favorite":    _play_overlay_favorite,
    "save_overlay_favorite":    _save_overlay_favorite,
    "set_param_x":              _set_param_x,
    "set_param_y":              _set_param_y,
    "nudge_param_x":            _nudge_param_x,
    "nudge_param_y":            _nudge_param_y,
    "toggle_blackout":          _toggle_blackout,
    "toggle_freeze":            _toggle_freeze,
    "kill_all":                 _kill_all,
    "cycle_pending_display":    _cycle_pending_display,
    "apply_pending_display":    _apply_pending_display,
}


def run(engine, action: dict) -> None:
    """Execute one action on the engine. Silently drops unknown names —
    the web layer rejects unknown actions earlier with a 400, so anything
    reaching here is either trusted (keymap) or already validated."""
    if not isinstance(action, dict):
        return
    fn = ACTIONS.get(action.get("name"))
    if fn is None:
        return
    args = action.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    try:
        fn(engine, args)
    except Exception as exc:  # noqa: BLE001 — never let a phone tap crash the engine
        print(f"[vj] action {action.get('name')!r} failed: {exc!r}")
