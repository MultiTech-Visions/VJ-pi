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
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


AUTOPILOT_KINDS = [
    "cycle_clips", "random_clips",
    "cycle_generatives", "random_generatives",
]

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

    # Autopilot
    autopilot_enabled: bool = False
    autopilot_kind: str = "cycle_clips"
    autopilot_interval_s: float = 8.0

    # Index into GRID_PRESETS used the last time Ctrl+G was hit on this
    # group — needed because preset (2,1) and (1,2) share a space count,
    # so we can't detect "current preset" from the spaces alone.
    grid_idx: int = 0

    # Transient (not persisted)
    _last_change_at: float = 0.0
    _time_offset: float = 0.0

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
        # (group_idx, space_idx) of the space currently picked up for
        # editing; clicking another space changes it.
        self.selected_space: Optional[tuple] = None
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
        # Active drag, one of {None, {"kind": "corner", "space": (gi,si), "corner": ci},
        # {"kind": "move", "space": (gi,si), "last": (nx,ny)},
        # {"kind": "create", "start": (nx,ny), "current": (nx,ny)}}
        self.drag: Optional[dict] = None

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
        self.selected = (self.selected + step) % len(self.groups)
        self.drag = None  # cancel any in-flight edit

    def add_group(self, seed_from: Optional[Group] = None):
        n = len(self.groups) + 1
        if seed_from is None:
            g = Group(name=f"Group {n}")
        else:
            g = Group.from_dict(seed_from.to_dict())
            g.name = f"Group {n}"
        self.groups.append(g)
        self.selected = len(self.groups) - 1

    def remove_selected_group(self):
        if len(self.groups) <= 1:
            return
        del self.groups[self.selected]
        self.selected = min(self.selected, len(self.groups) - 1)
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
        if g is None or len(g.spaces) <= 1:
            return
        g.spaces.pop()

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

    def cycle_autopilot_kind(self):
        g = self.selected_group()
        if g is None:
            return
        try:
            i = AUTOPILOT_KINDS.index(g.autopilot_kind)
            g.autopilot_kind = AUTOPILOT_KINDS[(i + 1) % len(AUTOPILOT_KINDS)]
        except ValueError:
            g.autopilot_kind = AUTOPILOT_KINDS[0]

    def toggle_autopilot_selected(self):
        g = self.selected_group()
        if g is None:
            return
        g.autopilot_enabled = not g.autopilot_enabled
        if g.autopilot_enabled:
            g._last_change_at = time.time()

    def tick_autopilot(self, engine, now):
        """Advance each group's content if its autopilot interval elapsed."""
        from engine import GENERATIVES
        for g in self.groups:
            if not g.autopilot_enabled:
                continue
            if g._last_change_at <= 0.0:
                g._last_change_at = now
                continue
            if now - g._last_change_at < g.autopilot_interval_s:
                continue
            g._last_change_at = now
            self._autopilot_step(g, engine, GENERATIVES)

    @staticmethod
    def _autopilot_step(g, engine, generatives):
        kind = g.autopilot_kind
        if kind in ("cycle_clips", "random_clips"):
            clips = engine.clips
            if len(clips) == 0:
                return
            g.content_kind = "clip"
            if kind == "cycle_clips":
                cur = clips.find_by_stem(g.clip_stem)
                new_idx = 0 if cur is None else (cur + 1) % len(clips)
            else:
                new_idx = random.randrange(len(clips))
            g.clip_stem = clips.name(new_idx)
        elif kind in ("cycle_generatives", "random_generatives"):
            g.content_kind = "generative"
            if not generatives:
                return
            if kind == "cycle_generatives":
                if g.gen_name in generatives:
                    i = (generatives.index(g.gen_name) + 1) % len(generatives)
                else:
                    i = 0
                g.gen_name = generatives[i]
            else:
                g.gen_name = random.choice(generatives)

    # ── Edit mode ────────────────────────────────────────────────────

    def toggle_edit_mode(self):
        self.edit_mode = not self.edit_mode
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
            self.selected_space = (gi, si)
            self.selected = gi

    def deselect_space(self):
        self.selected_space = None
        self.bind_armed = False

    def delete_selected_space(self):
        if self.selected_space is None:
            return
        gi, si = self.selected_space
        if not (0 <= gi < len(self.groups)) or not (0 <= si < len(self.groups[gi].spaces)):
            self.selected_space = None
            return
        del self.groups[gi].spaces[si]
        if not self.groups[gi].spaces:
            # Group went empty — delete it too. Keep at least one group
            # alive so the manager always has something to draw into.
            if len(self.groups) > 1:
                del self.groups[gi]
                if self.selected >= len(self.groups):
                    self.selected = len(self.groups) - 1
            else:
                # Last group; reseed a fullscreen blackout space.
                self.groups[0].spaces.append(Space.fullscreen())
        self.selected_space = None

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

    def start_move(self, gi, si, start_norm):
        self.drag = {"kind": "move", "space": (gi, si), "last": tuple(start_norm)}

    def start_create(self, start_norm):
        self.drag = {"kind": "create",
                     "start": tuple(start_norm), "current": tuple(start_norm)}

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

    # ── Hover toolbars (mouse-first editing UI) ──────────────────────

    # Per-button width in normalized canvas units. ~4 % means the icons
    # render readably on a 1280×720 projector (≈ 51 px) and stay clickable
    # on a 640-wide HUD preview (≈ 26 px).
    TOOLBAR_BTN = 0.04
    TOOLBAR_GAP = 0.008

    def update_hover(self, norm_xy):
        """Track which space the cursor is over so the renderer knows where
        to put the hover toolbar.

        Hover stays "sticky" while the cursor is anywhere inside the union
        of body + toolbar bounding rect, so traversing the gap between
        body and toolbar doesn't drop hover and make the buttons vanish.
        Without this the toolbar of a non-selected space (the one with the
        `+` bind button) is impossible to click — the cursor passes
        through the dead zone and the toolbar disappears before the click
        lands."""
        self.hover_norm = norm_xy
        if norm_xy is None:
            self.hovered_space = None
            return
        # Direct body hit always wins — clicking on a new space changes
        # hover to that space immediately.
        hit = self.hit_test_space(norm_xy)
        if hit is not None:
            self.hovered_space = hit
            return
        # Otherwise: prefer the currently-hovered space (sticky), then
        # the selected space. Both have toolbars rendered, so both have
        # hover regions that should stay alive.
        nx, ny = norm_xy
        candidates = []
        if self.hovered_space is not None:
            candidates.append(self.hovered_space)
        if (self.selected_space is not None
                and self.selected_space not in candidates):
            candidates.append(self.selected_space)
        for gi, si in candidates:
            x0, y0, x1, y1 = self._hover_region(gi, si)
            if x0 <= nx <= x1 and y0 <= ny <= y1:
                self.hovered_space = (gi, si)
                return
        self.hovered_space = None

    def _hover_region(self, gi, si):
        """Bounding rect (nx0, ny0, nx1, ny1) covering body + toolbar +
        the gap between them. Used as the "sticky" hover area so the
        toolbar stays visible while the cursor walks from body to button."""
        space = self.groups[gi].spaces[si]
        xs = [c[0] for c in space.corners]
        ys = [c[1] for c in space.corners]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        for _kind, (bx, by, bw, bh) in self.hover_toolbar_buttons(gi, si):
            x0 = min(x0, bx)
            y0 = min(y0, by)
            x1 = max(x1, bx + bw)
            y1 = max(y1, by + bh)
        return (x0, y0, x1, y1)

    def _toolbar_kinds(self, gi, si):
        """Decide which buttons appear on the toolbar for space (gi, si)."""
        is_sel = (self.selected_space == (gi, si))
        kinds = []
        if is_sel:
            kinds.extend([
                "fit_mode", "zoom_out", "zoom_in",
                "pan_left", "pan_right", "pan_up", "pan_down",
                "reset_frame",
            ])
            kinds.append("delete")
            if len(self.groups[gi].spaces) > 1:
                kinds.append("unbind")
        else:
            kinds.append("delete")
            # Show "bind into selected" only when a different group's
            # space is selected — same-group spaces are already bound.
            if (self.selected_space is not None
                    and self.selected_space[0] != gi):
                kinds.append("bind")
        kinds.append("group")  # always show the group tag last
        return kinds

    def hover_toolbar_buttons(self, gi, si):
        """Returns [(kind, (nx, ny, nw, nh)), ...] for the hover toolbar of
        space (gi, si). All coords normalized 0..1. Same layout used for
        rendering and hit-testing — the caller just scales by surface
        size. Returns [] if the space doesn't exist."""
        if not (0 <= gi < len(self.groups)):
            return []
        if not (0 <= si < len(self.groups[gi].spaces)):
            return []
        space = self.groups[gi].spaces[si]
        xs = [c[0] for c in space.corners]
        ys = [c[1] for c in space.corners]
        bx0, by0 = min(xs), min(ys)
        bx1, by1 = max(xs), max(ys)

        kinds = self._toolbar_kinds(gi, si)
        btn = self.TOOLBAR_BTN
        gap = self.TOOLBAR_GAP
        # Group label chip is a bit wider so a 2-digit "G12" fits.
        widths = []
        for k in kinds:
            if k == "group":
                widths.append(btn * 1.4)
            elif k == "fit_mode":
                widths.append(btn * 2.0)
            else:
                widths.append(btn)
        total = sum(widths) + gap * (len(widths) - 1)

        # Prefer above the bbox; fall back to below if no room.
        above_y = by0 - btn - gap
        toolbar_y = above_y if above_y >= 0 else min(1.0 - btn, by1 + gap)
        # Anchor at bbox left, clamp so the strip stays on canvas.
        toolbar_x = bx0
        if toolbar_x + total > 1.0:
            toolbar_x = max(0.0, 1.0 - total)

        result = []
        x = toolbar_x
        for kind, w in zip(kinds, widths):
            result.append((kind, (x, toolbar_y, w, btn)))
            x += w + gap
        return result

    def hit_test_hover_button(self, norm_xy):
        """If the click landed on a hover-toolbar button, return
        (kind, gi, si). Toolbar candidates are the hovered space AND the
        selected space (so the selected space's toolbar stays clickable
        even when the cursor briefly leaves the body)."""
        candidates = []
        if self.hovered_space is not None:
            candidates.append(self.hovered_space)
        if (self.selected_space is not None
                and self.selected_space not in candidates):
            candidates.append(self.selected_space)
        nx, ny = norm_xy
        for gi, si in candidates:
            for kind, (bx, by, bw, bh) in self.hover_toolbar_buttons(gi, si):
                if bx <= nx <= bx + bw and by <= ny <= by + bh:
                    return (kind, gi, si)
        return None
