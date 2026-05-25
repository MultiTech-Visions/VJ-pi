"""Lights mode: virtual front-of-house lighting rig.

A **Fixture** is a virtual light unit at a position on screen (0..1
normalized coords) with a fixture-type (spot / par / strobe), a color, an
intensity, and type-specific params. It renders as a stylised "mechanism"
icon (visible in EDIT mode only — the live show stays clean) plus its
volumetric light output additively-blended onto the canvas.

A **LightGroup** owns 1..N fixtures that share one chase pattern, a master
dimmer, and a chase speed. Tying fixtures together lets a single key
(`Q` for chase, `A`-`F` for color presets) drive the whole group
symmetrically — the same mental model as projection-mapping groups.

The **LightingRig** owns the groups list, the selected-group index, the
fixture-palette state for edit-mode placement, the cue stack (10
snapshots of per-fixture live params), haze density, and tap-tempo BPM.

Pitfalls this module tries to avoid:
  * Normalized 0..1 positions — fixtures survive resolution / display
    switches and projector swaps mid-show.
  * Chase phase is shared per-group; per-fixture variation comes from the
    fixture's index inside the group, so a 6-spot fan sweeps as a fan, not
    in unison.
  * Tap tempo evicts taps older than `TAP_WINDOW_S` so the BPM tracks
    tempo changes instead of stale averages.
  * Cue snapshots store ONLY dynamic params (color, intensity, on, pan,
    tilt, chase). Layout (which fixtures exist + where they're aimed) is
    NOT in the snapshot — recalling a cue stamps live params onto the
    existing layout. That means re-aiming spots in EDIT mode doesn't
    invalidate every saved cue.
  * Render helpers are kept in `engine.py` for consistency with
    `mapping.py` — this module is pure state.
"""
import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# Fixture kinds available for placement in edit mode. Index matches the
# keyboard palette in EDIT mode (1=spot, 2=par, 3=strobe).
FIXTURE_KINDS = ["spot", "par", "strobe"]

# Chase patterns Q cycles through. "off" leaves fixtures static; the other
# kinds apply per-fixture-type modulation (sweep → spot pan; blink → par
# intensity; all_strobe → every fixture flashes together).
CHASE_KINDS = ["off", "sweep", "blink", "all_strobe"]

# Quick colour presets bound to A / S / D / F in PERFORM mode. "rainbow"
# is special — it spreads hues across the group's fixtures rather than
# painting one solid colour.
COLOR_PRESETS = {
    "warm":    (255, 200, 130),
    "cyan":    (130, 220, 255),
    "magenta": (255, 110, 220),
    "rainbow": None,
}
# Stable iteration order so the keymap can index by position.
COLOR_PRESET_ORDER = ["warm", "cyan", "magenta", "rainbow"]


def _clamp01(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _clamp_byte(v):
    try:
        return max(0, min(255, int(v)))
    except (TypeError, ValueError):
        return 0


def _hsv_to_rgb(h, s, v):
    """h in degrees 0..360, s/v in 0..1. Returns (R, G, B) bytes.

    Small hand-rolled HSV→RGB so we don't drag cv2 into this module just
    for the rainbow palette. Matches what cv2.cvtColor would give us.
    """
    h = float(h) % 360.0
    c = v * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = v - c
    if   h < 60.0:  r, g, b = c, x, 0.0
    elif h < 120.0: r, g, b = x, c, 0.0
    elif h < 180.0: r, g, b = 0.0, c, x
    elif h < 240.0: r, g, b = 0.0, x, c
    elif h < 300.0: r, g, b = x, 0.0, c
    else:           r, g, b = c, 0.0, x
    return (_clamp_byte((r + m) * 255),
            _clamp_byte((g + m) * 255),
            _clamp_byte((b + m) * 255))


@dataclass
class Fixture:
    """A single virtual light unit, positioned in normalized (0..1) coords.

    `kind` controls how it renders + which params apply:
      * "spot" — moving-head with a throwable cone (pan drives screen-x
        deflection from straight-down; tilt scales beam length).
      * "par"  — fixed wash that paints a soft splash directly below the
        fixture body. Cheaper to render than a cone.
      * "strobe" — flashes a bright disc at `strobe_rate` Hz when on.
    """
    kind: str = "spot"
    x: float = 0.5            # 0..1 horizontal position
    y: float = 0.10           # 0..1 vertical position (near top = the truss)
    color: Tuple[int, int, int] = (255, 200, 130)
    intensity: float = 1.0    # 0..1 master per-fixture dimmer
    on: bool = True

    # Spot aim params. `pan` deflects the cone left (-1) or right (+1)
    # from straight-down; `tilt` scales beam length (0.0 = fully retracted,
    # 1.0 = beam reaches across the frame).
    pan: float = 0.0
    tilt: float = 0.85
    beam_width: float = 0.10  # cone half-width at the tip, in 0..1 of frame width
    beam_length: float = 0.95 # 0..1 of frame height

    # Strobe params
    strobe_rate: float = 8.0  # Hz
    strobe_radius: float = 0.08

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        kind = d.get("kind", "spot")
        if kind not in FIXTURE_KINDS:
            kind = "spot"
        col = d.get("color")
        if (isinstance(col, (list, tuple)) and len(col) == 3
                and all(isinstance(v, (int, float)) for v in col)):
            color = (_clamp_byte(col[0]), _clamp_byte(col[1]), _clamp_byte(col[2]))
        else:
            color = (255, 200, 130)
        return cls(
            kind=kind,
            x=_clamp01(d.get("x", 0.5)),
            y=_clamp01(d.get("y", 0.10)),
            color=color,
            intensity=_clamp01(d.get("intensity", 1.0)),
            on=bool(d.get("on", True)),
            pan=max(-1.0, min(1.0, float(d.get("pan", 0.0)))),
            tilt=_clamp01(d.get("tilt", 0.85)),
            beam_width=_clamp01(d.get("beam_width", 0.10)),
            beam_length=_clamp01(d.get("beam_length", 0.95)),
            strobe_rate=max(0.5, min(30.0, float(d.get("strobe_rate", 8.0)))),
            strobe_radius=_clamp01(d.get("strobe_radius", 0.08)),
        )

    def to_dict(self):
        return {
            "kind": self.kind,
            "x": float(self.x),
            "y": float(self.y),
            "color": list(self.color),
            "intensity": float(self.intensity),
            "on": bool(self.on),
            "pan": float(self.pan),
            "tilt": float(self.tilt),
            "beam_width": float(self.beam_width),
            "beam_length": float(self.beam_length),
            "strobe_rate": float(self.strobe_rate),
            "strobe_radius": float(self.strobe_radius),
        }


@dataclass
class LightGroup:
    name: str = "Group 1"
    fixtures: List[Fixture] = field(default_factory=list)

    # Chase pattern + state. `chase_speed` is in cycles-per-beat when
    # bpm_sync is on; otherwise cycles-per-second.
    chase_kind: str = "off"
    chase_speed: float = 1.0
    chase_phase: float = 0.0     # rolling 0..1
    bpm_sync: bool = True

    master: float = 1.0          # per-group dimmer (multiplies every fixture's)

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        kind = d.get("chase_kind", "off")
        if kind not in CHASE_KINDS:
            kind = "off"
        fxs = [Fixture.from_dict(f) for f in d.get("fixtures", [])
               if isinstance(f, dict)]
        return cls(
            name=str(d.get("name", "Group 1")),
            fixtures=fxs,
            chase_kind=kind,
            chase_speed=max(0.05, min(20.0, float(d.get("chase_speed", 1.0)))),
            bpm_sync=bool(d.get("bpm_sync", True)),
            master=_clamp01(d.get("master", 1.0)),
        )

    def to_dict(self):
        return {
            "name": self.name,
            "fixtures": [f.to_dict() for f in self.fixtures],
            "chase_kind": self.chase_kind,
            "chase_speed": float(self.chase_speed),
            "bpm_sync": bool(self.bpm_sync),
            "master": float(self.master),
        }

    def fixture_kind_counts(self):
        """Return {kind: count} for the summary HUD."""
        counts = {k: 0 for k in FIXTURE_KINDS}
        for fx in self.fixtures:
            counts[fx.kind] = counts.get(fx.kind, 0) + 1
        return counts


class LightingRig:
    """Holds the virtual-rig configuration + per-frame edit / perform state."""

    DEFAULT_HAZE = 0.55
    MAX_CUES = 10
    TAP_HISTORY = 8       # how many recent taps to consider when computing BPM
    TAP_WINDOW_S = 4.0    # taps older than this get evicted so BPM tracks changes

    def __init__(self, persisted=None):
        persisted = persisted or {}
        self.enabled = bool(persisted.get("enabled", False))
        self.selected = int(persisted.get("selected", 0))
        self.haze = _clamp01(persisted.get("haze", self.DEFAULT_HAZE))
        try:
            bpm = float(persisted.get("bpm", 120.0))
        except (TypeError, ValueError):
            bpm = 120.0
        self.bpm = max(30.0, min(300.0, bpm))

        groups_data = persisted.get("groups") or []
        self.groups: List[LightGroup] = [LightGroup.from_dict(g)
                                         for g in groups_data
                                         if isinstance(g, dict)]
        if not self.groups:
            self.groups = [LightGroup(name="Group 1", fixtures=[])]
        self.selected = max(0, min(self.selected, len(self.groups) - 1))

        # Cue snapshots — each is a serialisable list of group-dicts (just the
        # dynamic params; layout stays live). None = empty slot.
        cues = persisted.get("cues") or []
        self.cues: List[Optional[list]] = [None] * self.MAX_CUES
        for i in range(min(len(cues), self.MAX_CUES)):
            c = cues[i]
            if isinstance(c, list):
                self.cues[i] = c

        # Transient — never persisted.
        self.edit_mode: bool = False
        # Which fixture-type the next click will place. None = select/move.
        self.palette_kind: Optional[str] = None
        self.selected_fixture: Optional[Tuple[int, int]] = None
        self.drag: Optional[dict] = None
        # Tap-tempo state — wall-clock list of recent taps.
        self.taps: List[float] = []

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "selected": self.selected,
            "haze": self.haze,
            "bpm": self.bpm,
            "groups": [g.to_dict() for g in self.groups],
            "cues": self.cues,
        }

    # ── Group selection / mutation ───────────────────────────────────

    def selected_group(self) -> Optional[LightGroup]:
        if not self.groups:
            return None
        return self.groups[self.selected]

    def cycle_selected(self, step=1):
        if not self.groups:
            return
        self.selected = (self.selected + step) % len(self.groups)
        self.drag = None
        self.selected_fixture = None

    def add_group(self):
        n = len(self.groups) + 1
        self.groups.append(LightGroup(name=f"Group {n}", fixtures=[]))
        self.selected = len(self.groups) - 1
        self.selected_fixture = None

    def remove_selected_group(self):
        if len(self.groups) <= 1:
            return
        del self.groups[self.selected]
        self.selected = min(self.selected, len(self.groups) - 1)
        self.selected_fixture = None
        self.drag = None

    # ── Fixture placement / palette ─────────────────────────────────

    def arm_palette(self, kind):
        """Edit-mode UX: next click on the preview places a fixture of `kind`."""
        if kind in FIXTURE_KINDS:
            self.palette_kind = kind

    def disarm_palette(self):
        self.palette_kind = None

    def add_fixture(self, kind, nx, ny):
        if kind not in FIXTURE_KINDS:
            return None
        g = self.selected_group()
        if g is None:
            self.add_group()
            g = self.selected_group()
        fx = Fixture(kind=kind, x=_clamp01(nx), y=_clamp01(ny))
        # Sensible per-kind defaults — pars are usually low on the truss,
        # strobes sit alone, spots fan from the top.
        if kind == "par":
            fx.color = (255, 200, 130)
        elif kind == "strobe":
            fx.color = (255, 255, 255)
            fx.strobe_rate = 10.0
        g.fixtures.append(fx)
        self.selected_fixture = (self.selected, len(g.fixtures) - 1)
        return self.selected_fixture

    def delete_selected_fixture(self):
        if self.selected_fixture is None:
            return
        gi, fi = self.selected_fixture
        if not (0 <= gi < len(self.groups)):
            self.selected_fixture = None
            return
        g = self.groups[gi]
        if not (0 <= fi < len(g.fixtures)):
            self.selected_fixture = None
            return
        del g.fixtures[fi]
        self.selected_fixture = None

    def select_fixture(self, gi, fi):
        if 0 <= gi < len(self.groups) and 0 <= fi < len(self.groups[gi].fixtures):
            self.selected_fixture = (gi, fi)
            self.selected = gi

    def deselect_fixture(self):
        self.selected_fixture = None

    # ── Group-wide quick ops bound to perform keys ───────────────────

    def set_group_color(self, color_name):
        g = self.selected_group()
        if g is None or not g.fixtures:
            return
        if color_name == "rainbow":
            n = len(g.fixtures)
            for i, fx in enumerate(g.fixtures):
                fx.color = _hsv_to_rgb((i / n) * 360.0, 0.85, 1.0)
        elif color_name in COLOR_PRESETS and COLOR_PRESETS[color_name] is not None:
            c = COLOR_PRESETS[color_name]
            for fx in g.fixtures:
                fx.color = c

    def set_group_chase(self, kind):
        g = self.selected_group()
        if g is None:
            return
        if kind not in CHASE_KINDS:
            return
        g.chase_kind = kind
        g.chase_phase = 0.0

    def cycle_group_chase(self):
        g = self.selected_group()
        if g is None:
            return
        i = CHASE_KINDS.index(g.chase_kind) if g.chase_kind in CHASE_KINDS else 0
        g.chase_kind = CHASE_KINDS[(i + 1) % len(CHASE_KINDS)]
        g.chase_phase = 0.0

    def adjust_group_master(self, delta):
        g = self.selected_group()
        if g is None:
            return
        g.master = _clamp01(g.master + delta)

    def adjust_haze(self, delta):
        self.haze = _clamp01(self.haze + delta)

    # ── Tap tempo ────────────────────────────────────────────────────

    def tap_tempo(self):
        """Record a tap; after 2+ taps inside the window, update self.bpm.

        Uses inter-tap deltas (not avg-since-first) so the BPM tracks tempo
        changes within a few taps once stale taps fall off the window.
        """
        now = time.time()
        self.taps = [t for t in self.taps if now - t <= self.TAP_WINDOW_S]
        self.taps.append(now)
        if len(self.taps) > self.TAP_HISTORY:
            self.taps = self.taps[-self.TAP_HISTORY:]
        if len(self.taps) >= 2:
            deltas = [self.taps[i + 1] - self.taps[i]
                      for i in range(len(self.taps) - 1)]
            avg = sum(deltas) / len(deltas)
            # 30..300 BPM is a sane range for live electronic music; ignore
            # anything outside (probably a misfire / double-tap fat-finger).
            if 0.2 <= avg <= 2.0:
                self.bpm = max(30.0, min(300.0, 60.0 / avg))

    # ── Cue stack ────────────────────────────────────────────────────

    def save_cue(self, slot):
        """Snapshot dynamic per-group + per-fixture state into a cue slot.

        Layout (which fixtures exist, where they're placed, beam shape) is
        deliberately NOT captured — so editing the rig mid-set doesn't
        invalidate older cues. Recall stamps by index onto whatever's live.
        """
        if not (0 <= slot < self.MAX_CUES):
            return
        snap = []
        for g in self.groups:
            snap.append({
                "chase_kind": g.chase_kind,
                "chase_speed": g.chase_speed,
                "bpm_sync": g.bpm_sync,
                "master": g.master,
                "fixtures": [
                    {"color": list(fx.color),
                     "intensity": fx.intensity,
                     "on": fx.on,
                     "pan": fx.pan,
                     "tilt": fx.tilt,
                     "beam_width": fx.beam_width,
                     "beam_length": fx.beam_length,
                     "strobe_rate": fx.strobe_rate}
                    for fx in g.fixtures
                ],
            })
        self.cues[slot] = snap

    def recall_cue(self, slot):
        if not (0 <= slot < self.MAX_CUES):
            return False
        snap = self.cues[slot]
        if not isinstance(snap, list):
            return False
        for gi, g_snap in enumerate(snap):
            if gi >= len(self.groups) or not isinstance(g_snap, dict):
                continue
            g = self.groups[gi]
            kind = g_snap.get("chase_kind", g.chase_kind)
            if kind in CHASE_KINDS:
                g.chase_kind = kind
                g.chase_phase = 0.0
            try:
                g.chase_speed = max(0.05, min(20.0,
                    float(g_snap.get("chase_speed", g.chase_speed))))
            except (TypeError, ValueError):
                pass
            g.bpm_sync = bool(g_snap.get("bpm_sync", g.bpm_sync))
            g.master = _clamp01(g_snap.get("master", g.master))
            for fi, fx_snap in enumerate(g_snap.get("fixtures", [])):
                if fi >= len(g.fixtures) or not isinstance(fx_snap, dict):
                    continue
                fx = g.fixtures[fi]
                col = fx_snap.get("color")
                if isinstance(col, (list, tuple)) and len(col) == 3:
                    fx.color = (_clamp_byte(col[0]),
                                _clamp_byte(col[1]),
                                _clamp_byte(col[2]))
                fx.intensity = _clamp01(fx_snap.get("intensity", fx.intensity))
                fx.on = bool(fx_snap.get("on", fx.on))
                try:
                    fx.pan = max(-1.0, min(1.0,
                        float(fx_snap.get("pan", fx.pan))))
                except (TypeError, ValueError):
                    pass
                fx.tilt = _clamp01(fx_snap.get("tilt", fx.tilt))
                fx.beam_width = _clamp01(fx_snap.get("beam_width", fx.beam_width))
                fx.beam_length = _clamp01(fx_snap.get("beam_length", fx.beam_length))
                try:
                    fx.strobe_rate = max(0.5, min(30.0,
                        float(fx_snap.get("strobe_rate", fx.strobe_rate))))
                except (TypeError, ValueError):
                    pass
        return True

    def clear_cue(self, slot):
        if 0 <= slot < self.MAX_CUES:
            self.cues[slot] = None

    def cue_filled(self, slot):
        return 0 <= slot < self.MAX_CUES and self.cues[slot] is not None

    # ── Edit-mode hit testing / drag ─────────────────────────────────

    def hit_test_fixture(self, nx, ny, radius_norm=0.04):
        """Return (gi, fi) of the topmost fixture under (nx, ny), or None.

        Iterates newest-first so a freshly-placed fixture wins clicks over
        an older one sitting under the cursor.
        """
        r2 = radius_norm * radius_norm
        for gi in range(len(self.groups) - 1, -1, -1):
            fxs = self.groups[gi].fixtures
            for fi in range(len(fxs) - 1, -1, -1):
                fx = fxs[fi]
                if (fx.x - nx) ** 2 + (fx.y - ny) ** 2 <= r2:
                    return (gi, fi)
        return None

    def start_move(self, gi, fi, start_norm):
        self.drag = {"kind": "move", "fixture": (gi, fi),
                     "last": tuple(start_norm)}

    def update_drag(self, nx, ny):
        if self.drag is None or self.drag.get("kind") != "move":
            return
        gi, fi = self.drag["fixture"]
        if not (0 <= gi < len(self.groups)):
            self.drag = None
            return
        if not (0 <= fi < len(self.groups[gi].fixtures)):
            self.drag = None
            return
        fx = self.groups[gi].fixtures[fi]
        fx.x = _clamp01(nx)
        fx.y = _clamp01(ny)

    def end_drag(self):
        self.drag = None

    def cancel_drag(self):
        self.drag = None
        self.palette_kind = None

    # ── Per-frame chase tick ─────────────────────────────────────────

    def tick_chases(self, dt):
        """Advance each group's `chase_phase` by `dt` seconds.

        Wall-clock based, not frame-count based, so dips in framerate
        don't drag the chase out of sync with the BPM.
        """
        if dt <= 0.0:
            return
        for g in self.groups:
            if g.chase_kind == "off":
                continue
            beats_per_sec = (self.bpm / 60.0) if g.bpm_sync else 1.0
            cyc_per_sec = g.chase_speed * beats_per_sec
            g.chase_phase = (g.chase_phase + cyc_per_sec * dt) % 1.0

    def effective_fixture(self, group, fx, fi, n, t):
        """Compute (intensity, pan, color, on) after chase modulation.

        Chase-modulation rules:
          * "sweep" — spots' `pan` oscillates ±1 with per-fixture phase offset
            so 6 spots in a group sweep as a fan, not in unison.
          * "blink" — pars' intensity pulses; spots/strobes unchanged.
          * "all_strobe" — every fixture toggles full/off at 10 Hz together.
          * Strobe-kind fixtures ALWAYS flash at their own `strobe_rate`
            (independent of the group chase) — they're a "pure flash" unit.
        """
        intensity = fx.intensity * group.master
        pan = fx.pan
        on = fx.on
        color = fx.color

        if group.chase_kind == "sweep" and fx.kind == "spot":
            phase = (group.chase_phase + fi / max(1, n)) * 2.0 * math.pi
            pan = max(-1.0, min(1.0, math.sin(phase)))
        elif group.chase_kind == "blink" and fx.kind == "par":
            phase = (group.chase_phase + fi / max(1, n)) * 2.0 * math.pi
            intensity *= max(0.0, math.sin(phase))
        elif group.chase_kind == "all_strobe":
            flash_rate = 10.0
            on_ = math.fmod(t * flash_rate, 1.0) < 0.5
            intensity = intensity if on_ else 0.0

        if fx.kind == "strobe":
            on_ = math.fmod(t * fx.strobe_rate, 1.0) < 0.5
            intensity = intensity if on_ else 0.0

        return intensity, pan, on, color

    # ── Mode lifecycle ───────────────────────────────────────────────

    def toggle_edit_mode(self):
        self.edit_mode = not self.edit_mode
        if not self.edit_mode:
            self.drag = None
            self.palette_kind = None
            self.selected_fixture = None
