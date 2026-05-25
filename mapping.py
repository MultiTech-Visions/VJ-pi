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

# Pre-baked grid layouts available on Ctrl+G. (cols, rows).
GRID_PRESETS = [
    (1, 1), (2, 1), (1, 2), (2, 2), (3, 2), (3, 3), (4, 2), (4, 3),
]


def _clamp01(v):
    return max(0.0, min(1.0, float(v)))


def _default_corners():
    return [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


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
    content_kind: str = "blackout"        # "blackout" | "clip" | "generative"
    clip_stem: Optional[str] = None
    gen_name: Optional[str] = None
    overlay_stem: Optional[str] = None

    # Live controls (per-group so symmetric edits stay symmetric)
    param_x: float = 0.5
    param_y: float = 0.5
    fx_state: dict = field(default_factory=dict)

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
        }

    def content_label(self):
        if self.content_kind == "blackout":
            return "blackout"
        if self.content_kind == "clip":
            return f"clip: {self.clip_stem or '—'}"
        if self.content_kind == "generative":
            return f"gen:  {self.gen_name or '—'}"
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

        groups_data = persisted.get("groups") or []
        self.groups: List[Group] = [Group.from_dict(g) for g in groups_data
                                    if isinstance(g, dict)]
        if not self.groups:
            self.groups = [Group(name="Group 1")]
        self.selected = max(0, min(self.selected, len(self.groups) - 1))

        # Edit-mode transient state
        self.drag: Optional[dict] = None   # {"space": i, "corner": j}

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "selected": self.selected,
            "border_color": list(self.border_color),
            "border_thickness": self.border_thickness,
            "border_intensity": self.border_intensity,
            "show_borders": self.show_borders,
            "groups": [g.to_dict() for g in self.groups],
        }

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

    # ── Edit-mode drag handling ──────────────────────────────────────

    def hit_test_corner(self, norm_xy, radius_norm):
        """Hit-test the SELECTED group's corner handles. Returns (space_i,
        corner_i) or None. Coords are normalized 0..1; `radius_norm` is the
        handle radius in normalized units."""
        g = self.selected_group()
        if g is None:
            return None
        nx, ny = norm_xy
        r2 = radius_norm * radius_norm
        for si, space in enumerate(g.spaces):
            for ci, (cx, cy) in enumerate(space.corners):
                if (cx - nx) ** 2 + (cy - ny) ** 2 <= r2:
                    return (si, ci)
        return None

    def start_drag(self, space_i, corner_i):
        self.drag = {"space": space_i, "corner": corner_i}

    def update_drag(self, norm_xy):
        if self.drag is None:
            return
        g = self.selected_group()
        if g is None:
            self.drag = None
            return
        si, ci = self.drag["space"], self.drag["corner"]
        if 0 <= si < len(g.spaces):
            g.spaces[si].set_corner(ci, norm_xy[0], norm_xy[1])

    def end_drag(self):
        self.drag = None
