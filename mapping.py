"""Projection-mapping mode: spaces, groups, and the manager.

A **Space** is a quadrilateral (4 corners stored in 0..1 normalized output
coordinates so it survives resolution / display changes) that gets filled
with content warped from its owning Group's source frame.

A **Group** owns 1..N spaces that share one content source (clip /
generative / blackout), one FX chain, one set of params, and one autopilot
timer. Tying multiple spaces to one group is how you build symmetric
mappings — change the control once and every space updates together.

The **MappingManager** owns the groups list, the selected-group index, the
border style settings, and the per-group autopilot timers. It only does
state + bookkeeping; the actual frame compositing lives in Engine because
it needs the clip pool, generative functions, and FX chain.

Pitfalls this module tries to avoid:
  * Normalized 0..1 corners — the mapping survives resolution changes.
  * Per-group `_time_offset` — two groups running the same generative
    don't visually sync.
  * Autopilot timer is wall-clock based, not frame-count based, so it
    doesn't drift when frame rate dips.
  * Border drawing is skippable (`show_borders=False`) so during a live
    set there are no light artefacts on the wall.
"""
import os
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


AUTOPILOT_KINDS = [
    "cycle_clips", "random_clips",
    "cycle_generatives", "random_generatives",
]

# At or below this achieved frame rate, autopilot won't pile FX onto a
# group running a heavy projectM preset (and clears any already on) —
# FX are per-pixel work a crawling pm gen can't afford.
PM_HEAVY_FPS = 4.0

# How a group's content (clip frame or generative) is placed inside its
# spaces' quads. "window" is the default — the quad is a viewport into
# the video, free zoom + pan. "stretch" is the legacy warp-to-quad
# behaviour, kept as an opt-in for when the operator actually wants the
# perspective distortion (mapping onto an angled surface as a billboard).
FIT_MODES = ["window", "fit", "fill", "stretch"]

# Pre-baked grid layouts available on Ctrl+G. (cols, rows).
GRID_PRESETS = [
    (1, 1), (2, 1), (1, 2), (2, 2), (3, 2), (3, 3), (4, 2), (4, 3),
]


def _clamp01(v):
    return max(0.0, min(1.0, float(v)))


def _default_corners():
    return [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


def point_in_quad(p, corners):
    """Convex-quad hit test. Works for any quad whose vertices are listed
    in either consistent CW or CCW order (so 1-px slop on the boundary is
    fine since it returns True for both interior and edge points)."""
    px, py = p
    signs = []
    for i in range(4):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 4]
        signs.append((bx - ax) * (py - ay) - (by - ay) * (px - ax))
    eps = 1e-9
    return all(s >= -eps for s in signs) or all(s <= eps for s in signs)


@dataclass
class Space:
    """A quadrilateral on the output, corners in TL→TR→BR→BL order, 0..1."""
    corners: List[List[float]] = field(default_factory=_default_corners)

    @classmethod
    def fullscreen(cls):
        return cls(corners=_default_corners())

    @classmethod
    def from_dict(cls, d):
        corners = d.get("corners") if isinstance(d, dict) else None
        if not isinstance(corners, list) or len(corners) != 4:
            corners = _default_corners()
        clean = []
        for c in corners:
            if not isinstance(c, (list, tuple)) or len(c) != 2:
                clean.append([0.0, 0.0])
            else:
                clean.append([_clamp01(c[0]), _clamp01(c[1])])
        return cls(corners=clean)

    def to_dict(self):
        return {"corners": [[float(c[0]), float(c[1])] for c in self.corners]}

    def corners_px(self, w, h):
        return np.array([[c[0] * w, c[1] * h] for c in self.corners],
                        dtype=np.float32)

    def set_corner(self, idx, nx, ny):
        if 0 <= idx < 4:
            self.corners[idx] = [_clamp01(nx), _clamp01(ny)]


@dataclass
class Group:
    """A content source + 1..N spaces it paints into."""
    name: str = "Group"
    spaces: List[Space] = field(default_factory=lambda: [Space.fullscreen()])

    # Content selection
    content_kind: str = "blackout"        # "blackout"|"clip"|"generative"|"camera"
    clip_stem: Optional[str] = None
    gen_name: Optional[str] = None
    overlay_stem: Optional[str] = None

    # Live controls (per-group so symmetric edits stay symmetric)
    param_x: float = 0.5
    param_y: float = 0.5
    fx_state: dict = field(default_factory=dict)

    # How the content frame is placed inside the spaces' quads. Default
    # is "window" — the quad is a viewport, NOT a stretch target — so
    # the video keeps its natural aspect / framing and the operator
    # adjusts zoom + pan to compose what shows through each space.
    fit_mode: str = "window"
    zoom: float = 1.0              # 1.0 = video fits the bbox
    pan_x: float = 0.0             # -1..+1 of bbox half-width
    pan_y: float = 0.0             # -1..+1 of bbox half-height

    # Autopilot. Content auto-cycles WITHIN the type currently shown
    # (clip→random clip, generative→random generative); it also toggles
    # this group's FX and drifts its PARAM X/Y, mirroring the live
    # autopilot. autopilot_kind is legacy/unused (kept for state compat).
    autopilot_enabled: bool = False
    autopilot_kind: str = "cycle_clips"
    autopilot_interval_s: float = 8.0      # content switch delay
    autopilot_fx_interval_s: float = 4.0   # FX toggle rate

    # Index into GRID_PRESETS used the last time Ctrl+G was hit on this
    # group — needed because preset (2,1) and (1,2) share a space count,
    # so we can't detect "current preset" from the spaces alone.
    grid_idx: int = 0

    # Transient (not persisted)
    _last_change_at: float = 0.0
    _time_offset: float = 0.0
    # Autopilot scheduling / drift state (all transient).
    _next_fx_at: float = 0.0
    _next_param_at: float = 0.0
    _target_x: float = 0.5
    _target_y: float = 0.5
    _fx_expiry: dict = field(default_factory=dict)

    def __post_init__(self):
        # Stagger generative phase so identical generatives across groups
        # don't lock-step on the same time `t`.
        if self._time_offset == 0.0:
            self._time_offset = random.uniform(0.0, 1000.0)

    @classmethod
    def from_dict(cls, d):
        spaces = [Space.from_dict(s) for s in d.get("spaces", [])]
        if not spaces:
            spaces = [Space.fullscreen()]
        kind = d.get("autopilot_kind", "cycle_clips")
        if kind not in AUTOPILOT_KINDS:
            kind = "cycle_clips"
        fit_mode = d.get("fit_mode", "window")
        if fit_mode not in FIT_MODES:
            fit_mode = "window"
        g = cls(
            name=d.get("name", "Group"),
            spaces=spaces,
            content_kind=d.get("content_kind", "blackout"),
            clip_stem=d.get("clip_stem"),
            gen_name=d.get("gen_name"),
            overlay_stem=d.get("overlay_stem"),
            param_x=_clamp01(d.get("param_x", 0.5)),
            param_y=_clamp01(d.get("param_y", 0.5)),
            fx_state={k: bool(v) for k, v in (d.get("fx_state") or {}).items()},
            autopilot_enabled=bool(d.get("autopilot_enabled", False)),
            autopilot_kind=kind,
            autopilot_interval_s=max(1.0, float(d.get("autopilot_interval_s", 8.0))),
            autopilot_fx_interval_s=max(0.5, float(d.get("autopilot_fx_interval_s", 4.0))),
            grid_idx=int(d.get("grid_idx", 0)) % len(GRID_PRESETS),
            fit_mode=fit_mode,
            zoom=max(0.1, min(10.0, float(d.get("zoom", 1.0)))),
            pan_x=max(-3.0, min(3.0, float(d.get("pan_x", 0.0)))),
            pan_y=max(-3.0, min(3.0, float(d.get("pan_y", 0.0)))),
        )
        return g

    def to_dict(self):
        return {
            "name": self.name,
            "spaces": [s.to_dict() for s in self.spaces],
            "content_kind": self.content_kind,
            "clip_stem": self.clip_stem,
            "gen_name": self.gen_name,
            "overlay_stem": self.overlay_stem,
            "param_x": self.param_x,
            "param_y": self.param_y,
            "fx_state": dict(self.fx_state),
            "autopilot_enabled": self.autopilot_enabled,
            "autopilot_kind": self.autopilot_kind,
            "autopilot_interval_s": self.autopilot_interval_s,
            "autopilot_fx_interval_s": self.autopilot_fx_interval_s,
            "grid_idx": self.grid_idx,
            "fit_mode": self.fit_mode,
            "zoom": self.zoom,
            "pan_x": self.pan_x,
            "pan_y": self.pan_y,
        }

    def content_label(self):
        if self.content_kind == "blackout":
            return "blackout"
        if self.content_kind == "clip":
            return f"clip: {self.clip_stem or '—'}"
        if self.content_kind == "generative":
            return f"gen:  {self.gen_name or '—'}"
        if self.content_kind == "camera":
            return "live cam"
        return "—"


class MappingManager:
    """Holds the projection-mapping configuration + per-frame state."""

    DEFAULT_BORDER_COLOR = (180, 180, 180)   # light gray (not white)
    DEFAULT_BORDER_THICKNESS = 2

    def __init__(self, persisted=None):
        persisted = persisted or {}
        self.enabled = bool(persisted.get("enabled", False))
        self.selected = int(persisted.get("selected", 0))

        bc = persisted.get("border_color")
        if (isinstance(bc, list) and len(bc) == 3
                and all(isinstance(v, (int, float)) for v in bc)):
            self.border_color = (int(bc[0]) & 0xFF,
                                 int(bc[1]) & 0xFF,
                                 int(bc[2]) & 0xFF)
        else:
            self.border_color = self.DEFAULT_BORDER_COLOR
        self.border_thickness = max(1, int(persisted.get(
            "border_thickness", self.DEFAULT_BORDER_THICKNESS)))
        self.border_intensity = _clamp01(persisted.get("border_intensity", 1.0))
        self.show_borders = bool(persisted.get("show_borders", True))

        # Internal compositing resolution for mapping mode, as a fraction of
        # the output canvas. Mapping pushes every group's FX + warp at canvas
        # resolution, so the per-pixel cost dominates; rendering the composite
        # smaller and letting --gpu-scale stretch it to the projector (free,
        # on the GPU) is the biggest framerate lever. 1.0 = full canvas res.
        # Live-adjustable with F9/F10. Clamped to a sane floor so it can't be
        # dialled into mush. Live mode is unaffected — this only scales the
        # mapping composite.
        self.render_scale = max(0.4, min(1.0, float(
            persisted.get("render_scale", 0.6))))

        groups_data = persisted.get("groups") or []
        self.groups: List[Group] = [Group.from_dict(g) for g in groups_data
                                    if isinstance(g, dict)]
        if not self.groups:
            self.groups = [Group(name="Group 1")]
        self.selected = max(0, min(self.selected, len(self.groups) - 1))

        # Edit-mode transient state. None of this persists — the operator
        # always starts in perform mode and decides when to edit.
        self.edit_mode: bool = False
        # When True the on-wall selection banner (outline) is suppressed so
        # the projection is clean. Set by a Tab LONG-PRESS in perform mode
        # (see clear_selection); any explicit selection — Tab tap, click,
        # entering edit mode — clears it.
        self.banner_blank: bool = False
        # (group_idx, space_idx) of the space currently picked up for
        # editing; clicking another space changes it.
        self.selected_space: Optional[tuple] = None
        # Corner index (0..3) of the selected space that the arrow keys
        # nudge. Set when a corner is clicked/dragged; cleared when the
        # selection changes. Lets the operator drag a corner close with the
        # mouse, then fine-tune it pixel-by-pixel with the arrows.
        self.selected_corner: Optional[int] = None
        # (group_idx, space_idx) the cursor is currently over — updated on
        # MOUSEMOTION in either the HUD preview or the projector output,
        # used to decide which space's hover toolbar to render.
        self.hovered_space: Optional[tuple] = None
        # Last hover position (normalised 0..1), for drawing a big crosshair
        # cursor on the projector in edit mode (the OS pointer is tiny at 4K).
        self.hover_norm: Optional[tuple] = None
        # When True, the NEXT space click binds into the selected space's
        # group (instead of becoming the new selection). Reset after use.
        # Still used by the keyboard fallback path; the hover toolbar's +
        # button is the primary way to bind now.
        self.bind_armed: bool = False
        # Two-step destructive group delete. Backspace arms the currently
        # selected group; Backspace again confirms; Esc or selection changes
        # cancel it.
        self.delete_group_armed: Optional[int] = None
        # Four-click quad creation. Empty-space clicks add TL/TR/BR/BL-ish
        # points in the order the operator clicks them; the fourth point
        # creates the new mapped box.
        self.create_points: List[List[float]] = []
        # Active drag, one of {None,
        # {"kind": "corner", "space": (gi,si), "corner": ci},
        # {"kind": "move", "space": (gi,si), "last": (nx,ny)},
        # {"kind": "toolbar_move", "off": (dx,dy)},
        # {"kind": "toolbar_resize"}}.
        # New boxes use create_points instead of drag state.
        self.drag: Optional[dict] = None

        # Floating editor toolbar placement (normalised top-left + size).
        # Persisted so the operator parks it once on a flat, visible patch.
        tp = persisted.get("toolbar_pos")
        if isinstance(tp, (list, tuple)) and len(tp) == 2:
            self.toolbar_pos = [_clamp01(tp[0]), _clamp01(tp[1])]
        else:
            self.toolbar_pos = [0.03, 0.05]
        ts = persisted.get("toolbar_size")
        if isinstance(ts, (list, tuple)) and len(ts) == 2:
            self.toolbar_size = [max(0.07, min(1.0, float(ts[0]))),
                                 max(0.05, min(1.0, float(ts[1])))]
        else:
            self.toolbar_size = [0.50, 0.085]

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "selected": self.selected,
            "border_color": list(self.border_color),
            "border_thickness": self.border_thickness,
            "border_intensity": self.border_intensity,
            "show_borders": self.show_borders,
            "render_scale": self.render_scale,
            "toolbar_pos": list(self.toolbar_pos),
            "toolbar_size": list(self.toolbar_size),
            "groups": [g.to_dict() for g in self.groups],
        }

    def adjust_render_scale(self, delta):
        """Nudge the mapping compositing resolution (F9/F10). Returns the new
        value so the caller can log / invalidate size-dependent caches."""
        self.render_scale = max(0.4, min(1.0, round(self.render_scale + delta, 3)))
        return self.render_scale

    # ── Group selection / mutation ───────────────────────────────────

    def selected_group(self) -> Optional[Group]:
        if not self.groups:
            return None
        return self.groups[self.selected]

    def cycle_selected(self, step=1):
        if not self.groups:
            return
        n = len(self.groups)
        # Tab cycles over real groups only — in both perform and edit mode.
        # Clearing the selection banner (the clean-projection state) is now a
        # long-press on Tab, handled separately (see clear_selection); a tap
        # always lands on a real group and re-shows the banner.
        self.selected = (self.selected + step) % n
        self.banner_blank = False
        self.delete_group_armed = None
        self.create_points = []
        self.drag = None  # cancel any in-flight edit

    def clear_selection(self):
        """Hide the on-wall selection banner for a clean projection (Tab
        long-press in perform mode). Any explicit selection — a Tab tap, a
        click, entering edit mode — brings it back."""
        self.banner_blank = True
        self.delete_group_armed = None
        self.create_points = []
        self.drag = None

    def add_group(self, seed_from: Optional[Group] = None):
        n = len(self.groups) + 1
        if seed_from is None:
            g = Group(name=f"Group {n}")
        else:
            g = Group.from_dict(seed_from.to_dict())
            g.name = f"Group {n}"
        self.groups.append(g)
        self.selected = len(self.groups) - 1
        self.delete_group_armed = None
        self.create_points = []

    def move_group_in_stack(self, gi, delta):
        """Reorder group `gi` in the paint stack. Groups composite in list
        order, so a later group paints ON TOP. delta>0 raises the group
        (toward the end → on top); delta<0 lowers it (toward the front →
        behind). The selection (group + space) follows the moved group so the
        toolbar stays attached to it. Returns True if it actually moved."""
        if not (0 <= gi < len(self.groups)):
            return False
        nj = gi + (1 if delta > 0 else -1)
        if not (0 <= nj < len(self.groups)):
            return False
        self.groups[gi], self.groups[nj] = self.groups[nj], self.groups[gi]
        # Keep the selected-group pointer and the picked space following the
        # two groups that just swapped positions.
        if self.selected == gi:
            self.selected = nj
        elif self.selected == nj:
            self.selected = gi
        if self.selected_space is not None:
            sgi, ssi = self.selected_space
            if sgi == gi:
                self.selected_space = (nj, ssi)
            elif sgi == nj:
                self.selected_space = (gi, ssi)
        self.delete_group_armed = None
        return True

    def remove_selected_group(self):
        if len(self.groups) <= 1:
            # Keep one empty anchor group so the editor has somewhere to add
            # the next box, but remove the last visible box/group from output.
            if self.groups:
                self.groups[0].spaces = []
                self.groups[0].content_kind = "blackout"
                self.groups[0].clip_stem = None
                self.groups[0].gen_name = None
                self.groups[0].overlay_stem = None
                self.selected = 0
                self.selected_space = None
                self.hovered_space = None
                self.drag = None
                self.create_points = []
            self.delete_group_armed = None
            return
        del self.groups[self.selected]
        self.selected = min(self.selected, len(self.groups) - 1)
        self.selected_space = None
        self.hovered_space = None
        self.delete_group_armed = None
        self.create_points = []
        self.drag = None

    def add_space_to_selected(self):
        g = self.selected_group()
        if g is None:
            return
        # Drop the new space slightly inset from the last one so it's visible.
        n = len(g.spaces)
        off = 0.04 * ((n % 8) + 1)
        corners = [[_clamp01(0.10 + off), _clamp01(0.10 + off)],
                   [_clamp01(0.40 + off), _clamp01(0.10 + off)],
                   [_clamp01(0.40 + off), _clamp01(0.40 + off)],
                   [_clamp01(0.10 + off), _clamp01(0.40 + off)]]
        g.spaces.append(Space(corners=corners))

    def remove_space_from_selected(self):
        g = self.selected_group()
        if g is None or not g.spaces:
            return
        if len(g.spaces) <= 1:
            self.remove_selected_group()
            return
        g.spaces.pop()
        self.selected_space = None
        self.hovered_space = None
        self.delete_group_armed = None

    def cycle_grid_for_selected(self):
        """Cycle through pre-baked grid layouts (1x1 → 2x1 → … → 4x3 → 1x1)."""
        g = self.selected_group()
        if g is None:
            return
        g.grid_idx = (g.grid_idx + 1) % len(GRID_PRESETS)
        cols, rows = GRID_PRESETS[g.grid_idx]
        g.spaces = []
        for r in range(rows):
            for c in range(cols):
                x0, y0 = c / cols, r / rows
                x1, y1 = (c + 1) / cols, (r + 1) / rows
                g.spaces.append(Space(corners=[[x0, y0], [x1, y0],
                                               [x1, y1], [x0, y1]]))

    # ── Border style ─────────────────────────────────────────────────

    def adjust_border_intensity(self, delta):
        self.border_intensity = _clamp01(self.border_intensity + delta)

    def adjust_border_thickness(self, delta):
        self.border_thickness = max(1, min(10, self.border_thickness + delta))

    def cycle_border_color(self):
        """Cycle through a few sensible non-white border colors."""
        palette = [
            (180, 180, 180),   # light gray (default)
            (120, 200, 255),   # cyan-ish
            (255, 200, 90),    # warm amber
            (180, 120, 255),   # violet
            (120, 255, 160),   # mint green
            (255, 110, 110),   # red-pink
        ]
        try:
            i = palette.index(self.border_color)
            self.border_color = palette[(i + 1) % len(palette)]
        except ValueError:
            self.border_color = palette[0]

    def border_color_eff(self):
        """Apply intensity multiplier to the stored color."""
        i = self.border_intensity
        return (int(self.border_color[0] * i),
                int(self.border_color[1] * i),
                int(self.border_color[2] * i))

    def toggle_borders(self):
        self.show_borders = not self.show_borders

    # ── Autopilot ────────────────────────────────────────────────────

    def adjust_autopilot_interval(self, delta):
        g = self.selected_group()
        if g is None:
            return
        g.autopilot_interval_s = max(1.0, min(300.0,
                                              g.autopilot_interval_s + delta))


    def toggle_autopilot_selected(self):
        g = self.selected_group()
        if g is None:
            return
        g.autopilot_enabled = not g.autopilot_enabled
        if g.autopilot_enabled:
            g._last_change_at = time.time()

    def set_autopilot_selected(self, enabled):
        g = self.selected_group()
        if g is None:
            return
        g.autopilot_enabled = bool(enabled)
        if g.autopilot_enabled:
            g._last_change_at = time.time()

    def tick_autopilot(self, engine, now):
        """Drive each enabled group's autopilot: random-cycle its content
        within the type currently shown, toggle its FX, and drift its
        PARAM X/Y — analogous to the live autopilot, but per group."""
        from engine import GENERATIVES, FX_TOGGLES, AUTO_FX_MAX_HOLD
        dbg = os.environ.get("VJ_DEBUG_AUTOPILOT", "1") != "0"
        for g in self.groups:
            if not g.autopilot_enabled:
                continue
            # First tick after enabling: arm all the timers from `now`.
            if g._last_change_at <= 0.0:
                g._last_change_at = now
                g._next_fx_at = now + g.autopilot_fx_interval_s
                g._next_param_at = now
                g._target_x, g._target_y = g.param_x, g.param_y
                if dbg:
                    print(f"[autopilot] '{g.name}' armed (content every "
                          f"{g.autopilot_interval_s:.0f}s, fx {g.autopilot_fx_interval_s:.0f}s)")
                continue

            # Enforce per-FX max-hold caps (e.g. edges) so a dark FX can't
            # sit too long, regardless of the toggle interval.
            for fx in list(g._fx_expiry.keys()):
                if now >= g._fx_expiry[fx]:
                    g.fx_state[fx] = False
                    del g._fx_expiry[fx]

            # The output is over the brightness ceiling (the limiter is
            # already dimming it). FX don't reliably brighten — invert can even
            # DARKEN an all-white scene — so we don't touch FX for this; we
            # just move OFF the bright source sooner by pulling the content
            # switch forward (floored so it can't thrash).
            too_bright = (getattr(engine, "_bright_level", 0.0)
                          >= getattr(engine, "_bright_ceiling", 1.0))

            # Content switch — step to the NEXT source of the current type.
            held = now - g._last_change_at
            switch_due = held >= g.autopilot_interval_s
            if too_bright and held >= 2.0:
                switch_due = True
            if switch_due:
                g._last_change_at = now
                before = (g.content_kind, g.clip_stem, g.gen_name)
                self._autopilot_content(g, engine, GENERATIVES)
                if dbg:
                    after = (g.content_kind, g.clip_stem, g.gen_name)
                    tag = " (too bright)" if too_bright else ""
                    print(f"[autopilot] '{g.name}' content{tag} {before} -> {after}")

            # FX toggle (jittered around the group's fx interval) — UNLESS the
            # group is on a heavy projectM preset that's already crawling
            # (≤ PM_HEAVY_FPS). FX add per-pixel cost that a 4fps pm gen can't
            # afford, so suppress new FX and clear any that are on.
            fps = getattr(engine, "fps_measured", 0.0)
            heavy_pm = ((g.gen_name or "").startswith("pm:")
                        and g.content_kind == "generative"
                        and 0.0 < fps <= PM_HEAVY_FPS)
            if heavy_pm:
                if any(g.fx_state.get(k) for k in FX_TOGGLES):
                    for k in FX_TOGGLES:
                        g.fx_state[k] = False
                    g._fx_expiry.clear()
                    if dbg:
                        print(f"[autopilot] '{g.name}' heavy pm @ {fps:.1f}fps "
                              f"— FX cleared/suppressed")
            elif now >= g._next_fx_at:
                self._autopilot_fx(g, now, FX_TOGGLES, AUTO_FX_MAX_HOLD)
                g._next_fx_at = now + g.autopilot_fx_interval_s * random.uniform(0.5, 1.8)

            # PARAM X/Y drift toward fresh random targets every few seconds.
            if now >= g._next_param_at:
                g._target_x = random.random()
                g._target_y = random.random()
                g._next_param_at = now + random.uniform(2.0, 5.0)
            g.param_x += (g._target_x - g.param_x) * 0.04
            g.param_y += (g._target_y - g.param_y) * 0.04

    @staticmethod
    def _autopilot_content(g, engine, generatives):
        """Advance the group to the NEXT source of the SAME type it's
        currently showing (clip→next clip, generative→next generative), in
        the same order as the manual cycle keys. Sequential (not random) so
        that if the operator likes what lands, they can stop autopilot and
        step BACK to it with `−/=` (clips) or `[/]` (generators). Camera /
        blackout groups keep their content (autopilot still does FX + param
        drift on them)."""
        if g.content_kind == "generative":
            if not generatives:
                return
            if g.gen_name in generatives:
                i = (generatives.index(g.gen_name) + 1) % len(generatives)
            else:
                i = 0
            g.gen_name = generatives[i]
        elif g.content_kind == "clip":
            clips = engine.clips
            n = len(clips)
            if n == 0:
                return
            cur = clips.find_by_stem(g.clip_stem)
            new_idx = 0 if cur is None else (cur + 1) % n
            g.clip_stem = clips.name(new_idx)

    @staticmethod
    def _autopilot_fx(g, now, fx_toggles, max_hold):
        """Toggle one of the group's FX on/off, keeping the active count
        sane — same policy as the live autopilot."""
        active_on = [k for k in fx_toggles if g.fx_state.get(k)]
        active_off = [k for k in fx_toggles if not g.fx_state.get(k)]
        if active_on and (len(active_on) >= 3 or random.random() < 0.45):
            turned_off = random.choice(active_on)
            g.fx_state[turned_off] = False
            g._fx_expiry.pop(turned_off, None)
        elif active_off:
            turned_on = random.choice(active_off)
            g.fx_state[turned_on] = True
            if turned_on in max_hold:
                g._fx_expiry[turned_on] = now + max_hold[turned_on]

    # ── Edit mode ────────────────────────────────────────────────────

    def toggle_edit_mode(self):
        self.edit_mode = not self.edit_mode
        # Entering edit mode always targets a real group, never the blank stop.
        if self.edit_mode:
            self.banner_blank = False
        # Leaving edit mode cancels any half-finished gesture.
        if not self.edit_mode:
            self.drag = None
            self.bind_armed = False

    def arm_bind(self):
        """Set a flag: next clicked space is bound into the currently-
        selected space's group instead of becoming the new selection."""
        if self.selected_space is None:
            return
        self.bind_armed = True

    def select_space(self, gi, si):
        """Pick a space for editing — its corners get handles, key actions
        target its group."""
        if 0 <= gi < len(self.groups) and 0 <= si < len(self.groups[gi].spaces):
            if self.selected != gi:
                self.delete_group_armed = None
            self.create_points = []
            self.selected_space = (gi, si)
            self.selected_corner = None
            self.selected = gi
            self.banner_blank = False

    def deselect_space(self):
        self.selected_space = None
        self.selected_corner = None
        self.bind_armed = False
        self.delete_group_armed = None
        self.create_points = []

    def delete_selected_space(self):
        if self.selected_space is None:
            return
        gi, si = self.selected_space
        if not (0 <= gi < len(self.groups)) or not (0 <= si < len(self.groups[gi].spaces)):
            self.selected_space = None
            return
        del self.groups[gi].spaces[si]
        if not self.groups[gi].spaces:
            # Group went empty — delete it too, unless it is the final group.
            # The final group stays as an empty anchor so the last visible box
            # can truly disappear while the editor remains usable.
            if len(self.groups) > 1:
                del self.groups[gi]
                if self.selected >= len(self.groups):
                    self.selected = len(self.groups) - 1
            else:
                self.selected = 0
                self.groups[0].content_kind = "blackout"
                self.groups[0].clip_stem = None
                self.groups[0].gen_name = None
                self.groups[0].overlay_stem = None
        self.selected_space = None
        self.selected_corner = None
        self.hovered_space = None
        self.delete_group_armed = None
        self.create_points = []

    def unbind_selected_space(self):
        """Pull the selected space out into its own new group (so it can
        run independent content / autopilot)."""
        if self.selected_space is None:
            return
        gi, si = self.selected_space
        if not (0 <= gi < len(self.groups)) or len(self.groups[gi].spaces) <= 1:
            return
        space = self.groups[gi].spaces.pop(si)
        new_group = Group(name=f"Group {len(self.groups) + 1}", spaces=[space])
        self.groups.append(new_group)
        self.selected = len(self.groups) - 1
        self.selected_space = (self.selected, 0)
        self.selected_corner = None
        self.hovered_space = None
        self.bind_armed = False
        self.delete_group_armed = None
        self.create_points = []

    def bind_to_selected(self, gi, si):
        """Move space (gi, si) into the selected space's group. If the
        source group ends up empty it is removed. The selection stays on
        the originator so you can chain multiple binds into one group
        without re-selecting."""
        if self.selected_space is None:
            return
        dst_gi, dst_si = self.selected_space
        if gi == dst_gi or not (0 <= gi < len(self.groups)):
            return
        if not (0 <= dst_gi < len(self.groups)
                and 0 <= dst_si < len(self.groups[dst_gi].spaces)):
            self.selected_space = None
            return
        if not (0 <= si < len(self.groups[gi].spaces)):
            return
        src = self.groups[gi]
        space = src.spaces.pop(si)
        self.groups[dst_gi].spaces.append(space)
        if not src.spaces:
            del self.groups[gi]
            if gi < dst_gi:
                dst_gi -= 1
        self.selected = dst_gi
        self.selected_space = (dst_gi, dst_si)
        self.bind_armed = False
        self.delete_group_armed = None
        self.create_points = []
        # The space the cursor was over is gone or moved — invalidate.
        self.hovered_space = None

    # ── Hit testing ──────────────────────────────────────────────────

    def hit_test_corner_of_selected_space(self, norm_xy, radius_norm):
        """Return the corner index (0..3) of the SELECTED space that the
        point is on, or None. Other spaces' corners are not draggable —
        click the space first to select it."""
        if self.selected_space is None:
            return None
        gi, si = self.selected_space
        if not (0 <= gi < len(self.groups)) or not (0 <= si < len(self.groups[gi].spaces)):
            return None
        space = self.groups[gi].spaces[si]
        nx, ny = norm_xy
        r2 = radius_norm * radius_norm
        for ci, (cx, cy) in enumerate(space.corners):
            if (cx - nx) ** 2 + (cy - ny) ** 2 <= r2:
                return ci
        return None

    def hit_test_space(self, norm_xy):
        """Return (group_idx, space_idx) of the topmost space under `norm_xy`,
        or None. Iterate in reverse so newer (drawn-last) spaces win."""
        for gi in range(len(self.groups) - 1, -1, -1):
            spaces = self.groups[gi].spaces
            for si in range(len(spaces) - 1, -1, -1):
                if point_in_quad(norm_xy, spaces[si].corners):
                    return (gi, si)
        return None

    # ── Drag-to-corner / drag-to-move / drag-to-create ──────────────

    def start_corner_drag(self, corner_i):
        if self.selected_space is None:
            return
        gi, si = self.selected_space
        self.drag = {"kind": "corner", "space": (gi, si), "corner": corner_i}
        # Remember which corner the mouse grabbed so the arrow keys nudge it.
        self.selected_corner = corner_i

    def nudge_selected_corner(self, dx, dy):
        """Move the keyboard-active corner of the selected space by (dx, dy)
        in normalised output units. Returns True if a corner actually moved."""
        if self.selected_space is None or self.selected_corner is None:
            return False
        gi, si = self.selected_space
        if not (0 <= gi < len(self.groups)) or not (0 <= si < len(self.groups[gi].spaces)):
            return False
        space = self.groups[gi].spaces[si]
        ci = self.selected_corner
        if not (0 <= ci < len(space.corners)):
            return False
        cx, cy = space.corners[ci]
        space.set_corner(ci, cx + dx, cy + dy)
        return True

    def start_move(self, gi, si, start_norm):
        self.drag = {"kind": "move", "space": (gi, si), "last": tuple(start_norm)}

    def start_create(self, start_norm):
        self.create_points = [[_clamp01(start_norm[0]), _clamp01(start_norm[1])]]
        self.drag = None
        self.selected_space = None
        self.selected_corner = None
        self.hovered_space = None
        self.delete_group_armed = None

    def nudge_create_point(self, dx, dy):
        """While laying out a new quad (four-click create), move the
        most-recently-dropped point by (dx, dy) in normalised output units.
        Lets the operator drop a point roughly with the mouse, then dial it
        into a fine position with the arrows before dropping the next one —
        the mouse still places the following corner. Returns True if a point
        moved."""
        if not self.create_points:
            return False
        pt = self.create_points[-1]
        pt[0] = _clamp01(pt[0] + dx)
        pt[1] = _clamp01(pt[1] + dy)
        return True

    def add_create_point(self, norm_xy):
        """Add a point to the four-click quad creator. Returns True when a
        new space was created."""
        pt = [_clamp01(norm_xy[0]), _clamp01(norm_xy[1])]
        if not self.create_points:
            self.start_create(pt)
            return False
        self.create_points.append(pt)
        if len(self.create_points) < 4:
            return False
        space = Space(corners=[list(p) for p in self.create_points[:4]])
        empty_gi = next((i for i, g in enumerate(self.groups)
                         if not g.spaces), None)
        if empty_gi is not None:
            self.groups[empty_gi].spaces.append(space)
            gi = empty_gi
        else:
            self.groups.append(Group(
                name=f"Group {len(self.groups) + 1}", spaces=[space]
            ))
            gi = len(self.groups) - 1
        self.selected = gi
        self.selected_space = (gi, len(self.groups[gi].spaces) - 1)
        self.hovered_space = self.selected_space
        # Keep the arrow keys bound to the corner that was just dropped (the
        # 4th / last point) so the operator can fine-place it immediately,
        # without re-grabbing it with the mouse. The blue ring marks it.
        # Clicking elsewhere, Esc, or picking another space clears it.
        self.selected_corner = 3
        self.create_points = []
        self.drag = None
        return True

    def update_drag(self, norm_xy):
        self.hover_norm = norm_xy
        if self.drag is None:
            return
        kind = self.drag.get("kind")
        if kind == "corner":
            gi, si = self.drag["space"]
            if 0 <= gi < len(self.groups) and 0 <= si < len(self.groups[gi].spaces):
                self.groups[gi].spaces[si].set_corner(
                    self.drag["corner"], norm_xy[0], norm_xy[1]
                )
        elif kind == "move":
            gi, si = self.drag["space"]
            if not (0 <= gi < len(self.groups)) or not (0 <= si < len(self.groups[gi].spaces)):
                self.drag = None
                return
            space = self.groups[gi].spaces[si]
            raw_dx = norm_xy[0] - self.drag["last"][0]
            raw_dy = norm_xy[1] - self.drag["last"][1]
            # Clamp the delta so the WHOLE space stays on-screen — moving
            # by per-corner clamp would deform the quad instead of moving it.
            min_x = min(c[0] for c in space.corners)
            max_x = max(c[0] for c in space.corners)
            min_y = min(c[1] for c in space.corners)
            max_y = max(c[1] for c in space.corners)
            dx = max(-min_x, min(1.0 - max_x, raw_dx))
            dy = max(-min_y, min(1.0 - max_y, raw_dy))
            for c in space.corners:
                c[0] += dx
                c[1] += dy
            self.drag["last"] = tuple(norm_xy)
        elif kind == "toolbar_move":
            offx, offy = self.drag["off"]
            nw = max(self.TOOLBAR_MIN_W, min(1.0, float(self.toolbar_size[0])))
            nh = max(self.TOOLBAR_MIN_H, min(1.0, float(self.toolbar_size[1])))
            self.toolbar_pos = [
                max(0.0, min(1.0 - nw, norm_xy[0] - offx)),
                max(0.0, min(1.0 - nh, norm_xy[1] - offy)),
            ]
        elif kind == "toolbar_resize":
            nx, ny = self.toolbar_pos
            self.toolbar_size = [
                max(self.TOOLBAR_MIN_W, min(1.0 - nx, norm_xy[0] - nx)),
                max(self.TOOLBAR_MIN_H, min(1.0 - ny, norm_xy[1] - ny)),
            ]
        elif kind == "create":
            self.drag["current"] = (_clamp01(norm_xy[0]), _clamp01(norm_xy[1]))

    def end_drag(self):
        """Finalize whatever drag was in progress. For 'create', the
        rubber-banded rectangle becomes a new space in a brand-new group
        unless it's degenerate (too small to be useful)."""
        if self.drag is None:
            return
        kind = self.drag.get("kind")
        if kind == "create":
            sx, sy = self.drag["start"]
            cx, cy = self.drag["current"]
            x0, x1 = min(sx, cx), max(sx, cx)
            y0, y1 = min(sy, cy), max(sy, cy)
            if (x1 - x0) >= 0.02 and (y1 - y0) >= 0.02:
                space = Space(corners=[[x0, y0], [x1, y0],
                                       [x1, y1], [x0, y1]])
                # Prefer to fill any existing empty group (so the operator's
                # first drag after entering mapping mode doesn't leave a
                # ghost "Group 1" with no spaces sitting beside the new one).
                empty_gi = next((i for i, g in enumerate(self.groups)
                                 if not g.spaces), None)
                if empty_gi is not None:
                    self.groups[empty_gi].spaces.append(space)
                    gi = empty_gi
                else:
                    self.groups.append(Group(
                        name=f"Group {len(self.groups) + 1}", spaces=[space]
                    ))
                    gi = len(self.groups) - 1
                self.selected = gi
                self.selected_space = (gi, len(self.groups[gi].spaces) - 1)
        self.drag = None

    def cancel_drag(self):
        self.drag = None
        self.create_points = []

    # ── Frame controls (per-group zoom / pan / fit mode) ─────────────

    def cycle_fit_mode(self, step=1):
        g = self.selected_group()
        if g is None:
            return
        try:
            i = FIT_MODES.index(g.fit_mode)
        except ValueError:
            i = 0
        g.fit_mode = FIT_MODES[(i + step) % len(FIT_MODES)]

    def adjust_zoom(self, factor):
        """Multiply zoom by `factor` (e.g. 1.1 for +10 %)."""
        g = self.selected_group()
        if g is None:
            return
        g.zoom = max(0.1, min(10.0, g.zoom * factor))

    def adjust_pan(self, dx, dy):
        g = self.selected_group()
        if g is None:
            return
        g.pan_x = max(-3.0, min(3.0, g.pan_x + dx))
        g.pan_y = max(-3.0, min(3.0, g.pan_y + dy))

    def reset_frame(self):
        """Recenter & unzoom the selected group's content."""
        g = self.selected_group()
        if g is None:
            return
        g.zoom = 1.0
        g.pan_x = 0.0
        g.pan_y = 0.0

    # ── Floating editor toolbar (mouse-first editing UI) ─────────────
    #
    # ONE toolbar for the whole editor instead of a strip glued under each
    # box. The old per-space strip kept landing below the box, off the flat
    # part of the projection surface, where it was invisible/unusable. This
    # one floats: the operator drags it (grip bar) to any flat, visible
    # patch and resizes it (bottom-right handle) so it reflows from a long
    # one-row strip into a compact grid to fit the space available. It acts
    # on whichever space is currently selected. Position + size are
    # normalised 0..1 and persisted.

    TOOLBAR_MIN_W = 0.07
    TOOLBAR_MIN_H = 0.05
    TOOLBAR_GRIP_FRAC = 0.26    # share of toolbar height used by the move grip
    TOOLBAR_GRIP_MAX = 0.035    # …but never a fat grip on a tall square panel
    TOOLBAR_RESIZE = 0.028      # bottom-right resize handle (normalised)

    def update_hover(self, norm_xy):
        """Track the cursor (projector crosshair) and which space body it's
        over. With a single floating toolbar there's no per-space toolbar to
        keep alive, so this is just the direct body hit — no sticky region."""
        self.hover_norm = norm_xy
        self.hovered_space = (None if norm_xy is None
                              else self.hit_test_space(norm_xy))

    def floating_toolbar_rect(self):
        """(nx, ny, nw, nh) of the floating toolbar, clamped on-canvas."""
        nw = max(self.TOOLBAR_MIN_W, min(1.0, float(self.toolbar_size[0])))
        nh = max(self.TOOLBAR_MIN_H, min(1.0, float(self.toolbar_size[1])))
        nx = max(0.0, min(1.0 - nw, float(self.toolbar_pos[0])))
        ny = max(0.0, min(1.0 - nh, float(self.toolbar_pos[1])))
        return (nx, ny, nw, nh)

    def floating_toolbar_kinds(self):
        """Buttons shown on the floating toolbar. They all act on the
        currently selected space; empty when nothing is selected."""
        if self.selected_space is None:
            return []
        gi, si = self.selected_space
        if not (0 <= gi < len(self.groups)) or not (
                0 <= si < len(self.groups[gi].spaces)):
            return []
        kinds = ["fit_mode", "zoom_out", "zoom_in",
                 "pan_left", "pan_right", "pan_up", "pan_down",
                 "reset_frame"]
        if len(self.groups) > 1:
            kinds.extend(["raise", "lower"])
        kinds.append("bind")   # arm a bind: next space click binds into this group
        if len(self.groups[gi].spaces) > 1:
            kinds.append("unbind")
        kinds.append("delete")
        return kinds

    def _layout_buttons_in(self, body, canvas_w, canvas_h):
        """Reflow the buttons into a grid that fills `body` (nx,ny,nw,nh)
        with roughly square ON-SCREEN cells: a wide toolbar → many columns
        (one-row strip), a square toolbar → a ~sqrt(n) grid. Needs the
        canvas pixel size because normalised units aren't square."""
        kinds = self.floating_toolbar_kinds()
        bx, by, bw, bh = body
        if not kinds or bw <= 0 or bh <= 0:
            return []
        n = len(kinds)
        px_w = max(1.0, bw * canvas_w)
        px_h = max(1.0, bh * canvas_h)
        cols = int(round((n * px_w / px_h) ** 0.5))
        cols = max(1, min(n, cols))
        rows = -(-n // cols)   # ceil
        cell_w = bw / cols
        cell_h = bh / rows
        pad_x = cell_w * 0.10
        pad_y = cell_h * 0.10
        result = []
        for i, kind in enumerate(kinds):
            r, c = divmod(i, cols)
            x = bx + c * cell_w + pad_x
            y = by + r * cell_h + pad_y
            result.append((kind, (x, y,
                                  max(0.0, cell_w - 2 * pad_x),
                                  max(0.0, cell_h - 2 * pad_y))))
        return result

    def floating_toolbar_geometry(self, canvas_w, canvas_h):
        """All sub-rects of the floating toolbar (normalised): the panel,
        the move grip bar, the button body, the laid-out buttons, and the
        resize handle. One source of truth for rendering AND hit-testing."""
        nx, ny, nw, nh = self.floating_toolbar_rect()
        grip_h = min(nh * self.TOOLBAR_GRIP_FRAC, self.TOOLBAR_GRIP_MAX)
        grip = (nx, ny, nw, grip_h)
        body = (nx, ny + grip_h, nw, max(0.0, nh - grip_h))
        rh = min(self.TOOLBAR_RESIZE, nw * 0.5, nh * 0.5)
        resize = (nx + nw - rh, ny + nh - rh, rh, rh)
        return {
            "rect": (nx, ny, nw, nh),
            "grip": grip,
            "body": body,
            "resize": resize,
            "buttons": self._layout_buttons_in(body, canvas_w, canvas_h),
        }

    def hit_test_floating_toolbar(self, norm_xy, canvas_w, canvas_h):
        """Click priority on the floating toolbar: resize handle, then a
        button, then the grip/body (move). Returns ('button', kind) |
        ('move', None) | ('resize', None) | None."""
        geo = self.floating_toolbar_geometry(canvas_w, canvas_h)
        nx, ny = norm_xy

        def inside(rect):
            rx, ry, rw, rh = rect
            return rx <= nx <= rx + rw and ry <= ny <= ry + rh

        if inside(geo["resize"]):
            return ("resize", None)
        for kind, brect in geo["buttons"]:
            if inside(brect):
                return ("button", kind)
        if inside(geo["grip"]) or inside(geo["body"]):
            return ("move", None)
        return None

    def start_toolbar_move(self, norm_xy):
        """Begin dragging the toolbar. Remember the grab offset so it doesn't
        snap its corner to the cursor."""
        nx, ny, _nw, _nh = self.floating_toolbar_rect()
        self.drag = {"kind": "toolbar_move",
                     "off": (norm_xy[0] - nx, norm_xy[1] - ny)}

    def start_toolbar_resize(self, _norm_xy):
        self.drag = {"kind": "toolbar_resize"}
