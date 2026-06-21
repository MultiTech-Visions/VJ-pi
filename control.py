"""Control HUD window — runs on the second display (the operator's tiny
wireless tablet screen, ~4x6").

It is a single, fullscreen, landscape DASHBOARD: a big live preview top-left,
the NOW-PLAYING status + params beside it, favourite-slot grids below, and a
compact display picker / footer. The full key reference is an ON-DEMAND
overlay (press `/`), not a permanent wall of text.

Two things make it readable on a physically tiny panel:

  • Every font AND every spacing/offset is derived from one ``self._scale``
    factor, seeded from the operator's SYSTEM font point size (so it honours
    "the size I set for my desktop") and then live-tunable with Ctrl+ / Ctrl-
    (persisted to vj_state.json). No more 12px text baked in.

  • The window goes fullscreen on the control display (see main.py), so the
    whole tablet is used instead of a 680x720 window floating on the desktop.
"""
import os
import time

import pygame

from state import load_state, update_state


# Default HUD text scale when nothing is saved / no env override. Biased big
# because the operator's screen is physically tiny — easy to dial DOWN with
# Ctrl+- if it's too large.
DEFAULT_HUD_SCALE = 1.4
MIN_SCALE = 0.6
MAX_SCALE = 3.5


KEY_CHEAT = [
    ("− / =",        "Prev / next CLIP (hold to scrub)"),
    ("[ / ]",        "Prev / next GENERATOR (hold to scan)"),
    ("hold −+= / [+]", "JUMP picker: type a clip / generator # · Enter = go"),
    ("P",            "Toggle HUD live preview on/off (saves CPU)"),
    ("/",            "Show / hide this key reference"),
    ("Ctrl + / Ctrl −", "Bigger / smaller HUD text (saved)"),
    ("1 - 0",        "Clip favourites — tap=play, hold ≥½s=assign current"),
    ("A-L ;",        "Generator favourites — tap=play, hold ≥½s=assign"),
    ("Z X C V B",    "Hits: strobe / black / inv / zoom / RGB"),
    ("` · , / .",    "Face cloud: toggle · prev/next  (Shift+` = two facing)"),
    ("F1 - F8",      "Persistent FX toggles (F8 = melt)"),
    ("← → ↑ ↓",      "Tune PARAM X/Y  (auto: ←→ FX rate · ↑↓ clip rate)"),
    ("Enter Enter",  "Engage AUTOPILOT (any key hands control back)"),
    ("F11 / F12",    "Cycle output display / APPLY"),
    ("Space",        "Blackout (panic)"),
    ("Backspace",    "Freeze frame"),
    ("M",            "Toggle MAPPING mode"),
    ("N",            "Toggle 4K CINEMATIC mode  (-/= prev/next)"),
    ("Esc",          "Panic: clear FX / hits (keeps the clip playing)"),
    ("Shift+Esc",    "Quit"),
]

MAPPING_KEY_CHEAT = [
    ("M",            "Leave MAPPING mode"),
    ("E",            "Toggle EDIT mode (mouse drives the editor)"),
    ("Tab",          "Next group (Shift+Tab = prev; hold = hide border)"),
    ("/",            "Show / hide this key reference"),
    ("Ctrl + / Ctrl −", "Bigger / smaller HUD text (saved)"),
    ("EDIT — click empty", "place 4 corners → new mapped box"),
    ("EDIT — click body",  "pick that space (handles + toolbar appear)"),
    ("EDIT — drag body",   "move the whole space"),
    ("EDIT — drag corner", "reshape the picked space"),
    ("EDIT — ←→↑↓",     "nudge active corner 1px (Shift=10px)"),
    ("EDIT — toolbar ×",   "delete this space"),
    ("EDIT — toolbar +",   "bind this space into the selected space's group"),
    ("EDIT — toolbar ⊘",   "unbind this space into its own new group"),
    ("EDIT — toolbar frame", "mode / zoom / pan / reset selected group's video"),
    ("EDIT — toolbar G·",  "tag chip = which group this space belongs to"),
    ("EDIT — −/= [/]", "cycle clip / generator for selected box's group"),
    ("hold −+= / [+]", "JUMP picker: type a #, Enter → selected group"),
    ("P",            "Toggle HUD live preview on/off (saves CPU)"),
    ("EDIT — Backspace", "delete selected group: press twice"),
    ("EDIT — Esc",   "cancel drag / pending delete / deselect"),
    ("Ctrl+N",       "New group"),
    ("Ctrl+Back",    "Delete current group immediately"),
    ("Ctrl+= / -",   "Add / remove a space in current group"),
    ("Ctrl+G",       "Cycle grid layout (1·2x1·2x2·3x2·3x3·4x2·4x3)"),
    ("Enter Enter",  "Engage autopilot on selected group (Enter = off)"),
    ("Ctrl+A",       "Toggle autopilot on current group"),
    ("AUTO — ↑↓",    "content switch delay · ←→ FX rate"),
    ("Ctrl+, / .",   "Autopilot content delay ±1s"),
    ("PERFORM — content keys", "1-0/A-L;/[]/F1-F8/←→↑↓ → selected group"),
]


def _parse_font_spec(spec):
    """Parse a fontconfig-style 'Family Name 12' string → (family, points).

    Either part may be missing: 'PibotoLt' → ('PibotoLt', None); '12' →
    (None, 12); '' → (None, None)."""
    spec = (spec or "").strip().strip("'\"")
    if not spec:
        return None, None
    parts = spec.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].replace(".", "", 1).isdigit():
        fam = parts[0].strip() or None
        return fam, int(float(parts[1]))
    if spec.replace(".", "", 1).isdigit():
        return None, int(float(spec))
    return spec, None


def discover_system_ui_font():
    """Best-effort read of the operator's desktop UI font (family, point size).

    Honours, in order: the VJ_HUD_FONT env var, the GTK3 settings.ini
    ``gtk-font-name``, then ``gsettings`` (GNOME schema). Falls back to a
    sane default so the HUD always renders. The point size is what seeds the
    HUD text scale — i.e. "use the size I set for my system."
    """
    fam, pt = _parse_font_spec(os.environ.get("VJ_HUD_FONT", ""))

    if fam is None or pt is None:
        try:
            import configparser
            from pathlib import Path
            ini = Path.home() / ".config" / "gtk-3.0" / "settings.ini"
            if ini.exists():
                cp = configparser.ConfigParser()
                cp.read(ini)
                f2, p2 = _parse_font_spec(
                    cp.get("Settings", "gtk-font-name", fallback=""))
                fam = fam or f2
                pt = pt or p2
        except Exception:
            pass

    if pt is None:
        try:
            import subprocess
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "font-name"],
                capture_output=True, text=True, timeout=1.5)
            if out.returncode == 0:
                f2, p2 = _parse_font_spec(out.stdout.strip())
                fam = fam or f2
                pt = pt or p2
        except Exception:
            pass

    return (fam or "Sans"), (pt or 11)


class ControlWindow:
    """Renders the control HUD into the second display.

    Either the HUD owns the single GL context (renderer set → texture
    upload), or — under --gpu-scale — the HUD is the pygame.display software
    window (blit + flip). One GL context per process either way.
    """

    def __init__(self, engine, window, renderer, size, preview_size,
                 software_surface=None):
        Texture = None
        if renderer is not None:
            from pygame._sdl2.video import Texture
        self.engine = engine
        self.window = window
        self.renderer = renderer
        self.software_surface = software_surface
        self.size = size  # (w, h)
        self.surface = pygame.Surface(size)

        src_w, src_h = preview_size
        self._aspect = (src_w / src_h) if src_h else (16.0 / 9.0)
        self._Texture = Texture

        # Font: family + point size from the operator's desktop settings; the
        # point size seeds the scale so the HUD honours their chosen size.
        self._font_family, self._sys_pt = discover_system_ui_font()

        st = load_state()
        env_scale = os.environ.get("VJ_HUD_SCALE", "").strip()
        try:
            scale = (float(env_scale) if env_scale
                     else float(st.get("hud_scale", DEFAULT_HUD_SCALE)))
        except ValueError:
            scale = DEFAULT_HUD_SCALE
        self._scale = max(MIN_SCALE, min(MAX_SCALE, scale))

        self._build_fonts()
        self._recompute_metrics()

        # Hit-test rects, populated each frame in render().
        self._display_btn_rects = []
        self._apply_rect = None
        self._preview_toggle_rect = pygame.Rect(0, 0, 0, 0)
        try:
            self._window_id = window.id
        except AttributeError:
            self._window_id = None

        self.preview_enabled = st.get("hud_preview", True)
        self.show_keys = False

    # ── Scaling / fonts ──────────────────────────────────────────────

    def _px(self, v):
        """Scale a design pixel value by the current HUD scale (≥1)."""
        return max(1, int(round(v * self._scale)))

    def _build_fonts(self):
        fam = f"{self._font_family},Sans,DejaVuSans,Arial"
        base = self._sys_pt * 1.5 * self._scale

        def mk(px, bold=False):
            return pygame.font.SysFont(fam, max(9, int(round(px))), bold=bold)

        self.font_s = mk(base * 0.80)
        self.font_m = mk(base)
        self.font_h = mk(base * 1.28, bold=True)
        self.font_big = mk(base * 2.0, bold=True)

        self.line_s = self.font_s.get_linesize()
        self.line_m = self.font_m.get_linesize()
        self.line_h = self.font_h.get_linesize()
        # A consistent label column so "CLIP", "AUTOPILOT", etc. all align and
        # values never collide with labels at any scale.
        self._label_col = self.font_m.size("AUTOPILOT ")[0] + self._px(8)

    def _recompute_metrics(self):
        win_w, win_h = self.size
        self.pad = self._px(8)
        self._toggle_h = self._px(20)
        # Preview: a target height (~40% of the screen) keeping source aspect,
        # capped so the status column beside it keeps room.
        ph = int(win_h * 0.40)
        pw = int(ph * self._aspect)
        max_pw = int(win_w * 0.55)
        if pw > max_pw:
            pw = max_pw
            ph = int(pw / self._aspect)
        self.preview_w = max(80, pw)
        self.preview_h = max(60, ph)
        self._preview_rect = pygame.Rect(self.pad, self.pad,
                                         self.preview_w, self.preview_h)

    def adjust_scale(self, delta):
        """Live HUD-text resize (Ctrl+ / Ctrl-). Rebuilds fonts + layout and
        persists the choice so it sticks across launches."""
        new = max(MIN_SCALE, min(MAX_SCALE, round(self._scale + delta, 2)))
        if new == self._scale:
            return
        self._scale = new
        update_state(hud_scale=self._scale)
        self._build_fonts()
        self._recompute_metrics()
        try:
            self.engine._flash(f"HUD text {int(self._scale * 100)}%")
        except Exception:
            pass

    def toggle_keys_overlay(self):
        self.show_keys = not self.show_keys
        return self.show_keys

    # ── Event handling ───────────────────────────────────────────────

    def handle_event(self, event):
        """Forward mouse clicks to the HUD's buttons (preview toggle, display
        picker). Mapping boxes are edited on the projector output, never in
        this preview."""
        if not self._event_is_ours(event):
            return
        if event.type != pygame.MOUSEBUTTONDOWN or event.button != 1:
            return
        pos = getattr(event, "pos", None)
        if pos is None:
            return

        if self._preview_toggle_rect.collidepoint(pos):
            self.toggle_preview()
            return
        for idx, rect in self._display_btn_rects:
            if rect.collidepoint(pos):
                self.engine.pending_display = idx
                return
        if self._apply_rect is not None and self._apply_rect.collidepoint(pos):
            self.engine.apply_pending_display()

    def _event_is_ours(self, event):
        win = getattr(event, "window", None)
        if self._window_id is None:
            return True
        if win is None:
            return False
        ev_id = getattr(win, "id", None)
        if ev_id is None:
            return win is self.window
        return ev_id == self._window_id

    # ── Rendering ────────────────────────────────────────────────────

    def render(self, frame):
        surface = self.surface
        surface.fill((16, 18, 26))
        win_w, win_h = self.size
        e = self.engine
        mapping_mode = e.mode == "mapping"
        p = self.pad
        gap = self._px(6)

        # Top-left: live preview (+ toggle button beneath it).
        self._draw_preview(surface, p, p, frame)
        if mapping_mode:
            self._draw_space_overlay(surface)

        # Top-right: status / params beside the preview.
        sx = p + self.preview_w + self._px(14)
        right_w = max(self._px(120), win_w - sx - p)
        if mapping_mode:
            status_bottom = self._draw_mapping_status(surface, sx, p, right_w)
        else:
            status_bottom = self._draw_status(surface, sx, p, right_w)

        prev_bottom = p + self.preview_h + self._toggle_h + gap
        y = max(prev_bottom, status_bottom) + gap

        # Full-width rows below the top band.
        y = self._draw_fps(surface, p, y, mapping_mode)
        y = self._draw_badges(surface, p, y)

        if mapping_mode:
            y = self._draw_groups_list(surface, p, y, win_w - p * 2)
            y += gap

        clip_header = "CLIP FAVS  (1-0)"
        gen_header = "GEN  FAVS  (A-L ;)"
        active_clip_stem = e.clips.name(e.clips.active_idx)
        active_gen_name = e.active_generative
        if mapping_mode:
            g = e.mapping.selected_group()
            if g is not None:
                clip_header += f"  → {g.name}"
                gen_header += f"  → {g.name}"
                active_clip_stem = g.clip_stem if g.content_kind == "clip" else None
                active_gen_name = g.gen_name if g.content_kind == "generative" else None
        y = self._draw_favorites(surface, p, y, win_w - p * 2,
                                 clip_header, "1234567890",
                                 e.clip_favorites, active_stem=active_clip_stem)
        y += self._px(3)
        y = self._draw_favorites(surface, p, y, win_w - p * 2,
                                 gen_header, "ASDFGHJKL;",
                                 e.generator_favorites, active_stem=active_gen_name)
        y += gap

        # Footer: compact display picker + a one-line key hint.
        y = self._draw_display_selector(surface, p, y, win_w - p * 2)
        hint = self.font_s.render(
            "/ keys   ·   Ctrl + / Ctrl −  text size   ·   P preview",
            True, (120, 124, 150))
        if y + hint.get_height() < win_h:
            surface.blit(hint, (p, y + self._px(2)))

        # Overlays drawn last so they sit on top.
        if e.number_entry is not None:
            self._draw_number_entry(surface)
        if self.show_keys:
            self._draw_keys_overlay(surface, mapping_mode)

        # Present.
        if self.renderer is not None:
            tex = self._Texture.from_surface(self.renderer, surface)
            self.renderer.clear()
            tex.draw()
            self.renderer.present()
        elif self.software_surface is not None:
            self.software_surface.blit(surface, (0, 0))
            pygame.display.flip()

    def toggle_preview(self):
        """Flip the live HUD preview (per-frame thumbnail blit). Persisted."""
        self.preview_enabled = not self.preview_enabled
        update_state(hud_preview=self.preview_enabled)
        return self.preview_enabled

    # ── FPS / perf row ───────────────────────────────────────────────

    def _draw_fps(self, surface, x, y, mapping_mode):
        e = self.engine
        fps = getattr(e, "fps_measured", 0.0)
        fcol = ((110, 230, 130) if fps >= 25.0 else
                (245, 215, 110) if fps >= 15.0 else (240, 90, 90))
        fps_surf = self.font_h.render("%.0f FPS" % fps, True, fcol)
        surface.blit(fps_surf, (x, y))
        pm_fps, pm_n = e.gpu_generators.pm_stream_fps()
        if pm_n:
            pcol = ((110, 230, 130) if pm_fps >= 13.0 else
                    (245, 215, 110) if pm_fps >= 8.0 else (240, 90, 90))
            ps = self.font_m.render("pm %.0ffps ×%d" % (pm_fps, pm_n), True, pcol)
            surface.blit(ps, (x + fps_surf.get_width() + self._px(12),
                              y + fps_surf.get_height() - ps.get_height() - 2))
        y += fps_surf.get_height() + self._px(3)

        if mapping_mode:
            pdat = getattr(e, "_perf_ms", None)
            if pdat:
                disp = getattr(e, "_disp_ms", 0.0)
                btxt = "clip %.0f · gen %.0f · fx %.0f · warp %.0f · disp %.0f ms" % (
                    pdat["clip"], pdat["gen"], pdat["fx"], pdat["warp"], disp)
                bsurf = self.font_s.render(btxt, True, (175, 180, 200))
                surface.blit(bsurf, (x, y))
                y += bsurf.get_height() + self._px(3)
                rs = getattr(e.mapping, "render_scale", 1.0)
                rtxt = "map res %d%%  (%d×%d)  F9/F10" % (
                    round(rs * 100), int(e.cfg.width * rs), int(e.cfg.height * rs))
                rcol = (150, 200, 150) if rs >= 0.999 else (210, 190, 120)
                rsurf = self.font_s.render(rtxt, True, rcol)
                surface.blit(rsurf, (x, y))
                y += rsurf.get_height() + self._px(3)
        return y

    # ── Number-jump picker overlay ───────────────────────────────────

    def _draw_number_entry(self, surface):
        e = self.engine
        ne = e.number_entry
        if ne is None:
            return
        win_w, win_h = self.size
        total = e.number_entry_total()
        is_gen = ne["target"] == "gen"
        title = "JUMP TO GENERATOR" if is_gen else "JUMP TO CLIP"
        accent = (150, 220, 255) if is_gen else (255, 210, 130)
        buf = ne["buffer"] or "—"
        target_hint = ""
        if e.mode == "mapping":
            g = e.mapping.selected_group()
            if g is not None:
                target_hint = f"  → {g.name}"

        bw = min(self._px(440), win_w - self._px(24))
        bh = self._px(150)
        bx, by = (win_w - bw) // 2, (win_h - bh) // 2
        box = pygame.Rect(bx, by, bw, bh)
        shade = pygame.Surface((win_w, win_h), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 150))
        surface.blit(shade, (0, 0))
        pygame.draw.rect(surface, (28, 32, 44), box, border_radius=10)
        pygame.draw.rect(surface, accent, box, 2, border_radius=10)

        ts = self.font_h.render(title + target_hint, True, accent)
        surface.blit(ts, (bx + self._px(18), by + self._px(14)))
        ns = self.font_big.render(f"{buf} / {total}", True, (245, 245, 255))
        surface.blit(ns, (bx + self._px(18), by + self._px(48)))
        hint = self.font_s.render("type a number · Enter = go · Esc = cancel",
                                  True, (170, 175, 195))
        surface.blit(hint, (bx + self._px(18),
                            by + bh - hint.get_height() - self._px(10)))

    # ── Key reference overlay ────────────────────────────────────────

    def _draw_keys_overlay(self, surface, mapping_mode):
        win_w, win_h = self.size
        rows = MAPPING_KEY_CHEAT if mapping_mode else KEY_CHEAT
        shade = pygame.Surface((win_w, win_h), pygame.SRCALPHA)
        shade.fill((8, 9, 14, 232))
        surface.blit(shade, (0, 0))

        p = self._px(16)
        title = self.font_h.render(
            ("MAPPING KEYS" if mapping_mode else "KEYS") + "   (/ to close)",
            True, (230, 230, 245))
        surface.blit(title, (p, p))
        top = p + title.get_height() + self._px(8)

        col_gap = self._px(20)
        col_w = (win_w - 2 * p - col_gap) // 2
        line = self.line_s + self._px(3)
        rows_per_col = -(-len(rows) // 2)  # ceil
        key_w = int(col_w * 0.42)
        for i, (keys, desc) in enumerate(rows):
            col = i // rows_per_col
            row_i = i % rows_per_col
            cx = p + col * (col_w + col_gap)
            cy = top + row_i * line
            if cy + line > win_h:
                continue
            ks = self.font_s.render(keys, True, (150, 205, 255))
            surface.blit(ks, (cx, cy))
            ds = self.font_s.render(desc, True, (195, 195, 210))
            surface.blit(ds, (cx + key_w, cy))

    # ── Preview ──────────────────────────────────────────────────────

    def _draw_preview(self, surface, x, y, frame):
        self._preview_rect = pygame.Rect(x, y, self.preview_w, self.preview_h)
        if self.preview_enabled and frame is not None:
            preview = pygame.image.frombuffer(
                frame.tobytes(), (frame.shape[1], frame.shape[0]), "RGB")
            preview = pygame.transform.scale(preview,
                                             (self.preview_w, self.preview_h))
            surface.blit(preview, (x, y))
        else:
            pygame.draw.rect(surface, (40, 40, 50),
                             (x, y, self.preview_w, self.preview_h))
            if not self.preview_enabled:
                off_label = self.font_m.render("PREVIEW OFF", True, (100, 100, 120))
                surface.blit(off_label,
                             (x + (self.preview_w - off_label.get_width()) // 2,
                              y + (self.preview_h - off_label.get_height()) // 2))
        pygame.draw.rect(surface, (90, 90, 120),
                         (x, y, self.preview_w, self.preview_h), 1)

        # Clickable toggle button just below the preview.
        tog_w = self._px(76)
        tog_h = self._toggle_h
        tog_x = x + self.preview_w - tog_w
        tog_y = y + self.preview_h + self._px(3)
        self._preview_toggle_rect = pygame.Rect(tog_x, tog_y, tog_w, tog_h)
        if self.preview_enabled:
            bg, border, fg = (50, 90, 60), (100, 180, 120), (180, 255, 190)
            label = "LIVE"
        else:
            bg, border, fg = (60, 45, 45), (160, 100, 100), (220, 160, 160)
            label = "OFF"
        pygame.draw.rect(surface, bg, self._preview_toggle_rect, border_radius=4)
        pygame.draw.rect(surface, border, self._preview_toggle_rect, 1,
                         border_radius=4)
        tog_label = self.font_s.render(label, True, fg)
        surface.blit(tog_label,
                     (tog_x + (tog_w - tog_label.get_width()) // 2,
                      tog_y + (tog_h - tog_label.get_height()) // 2))

    # ── Mapping preview overlays (normalized coords → scale-independent) ──

    @staticmethod
    def _mesh_segments(corners, steps=4):
        if len(corners) != 4:
            return []
        tl, tr, br, bl = corners
        segs = []
        for i in range(1, steps):
            t = i / steps
            top = (tl[0] * (1.0 - t) + tr[0] * t,
                   tl[1] * (1.0 - t) + tr[1] * t)
            bottom = (bl[0] * (1.0 - t) + br[0] * t,
                      bl[1] * (1.0 - t) + br[1] * t)
            left = (tl[0] * (1.0 - t) + bl[0] * t,
                    tl[1] * (1.0 - t) + bl[1] * t)
            right = (tr[0] * (1.0 - t) + br[0] * t,
                     tr[1] * (1.0 - t) + br[1] * t)
            segs.append((top, bottom))
            segs.append((left, right))
        return segs

    def _preview_xy(self, pt):
        rect = self._preview_rect
        return (int(round(rect.x + pt[0] * rect.w)),
                int(round(rect.y + pt[1] * rect.h)))

    def _draw_preview_mesh(self, surface, corners, color):
        for a, b in self._mesh_segments(corners):
            pygame.draw.line(surface, color, self._preview_xy(a),
                             self._preview_xy(b), 1)

    def _draw_create_points_preview(self, surface):
        m = self.engine.mapping
        dropped = [tuple(p) for p in m.create_points]
        pts = list(dropped)
        if m.hover_norm is not None and len(pts) < 4:
            pts.append(tuple(m.hover_norm))
        if not pts:
            return
        px = [self._preview_xy(p) for p in pts]
        for p in px:
            pygame.draw.circle(surface, (20, 20, 30), p, 6)
            pygame.draw.circle(surface, (120, 255, 160), p, 5)
        for a, b in zip(px, px[1:]):
            pygame.draw.line(surface, (120, 255, 160), a, b, 2)
        if dropped:
            pygame.draw.circle(surface, (90, 230, 255),
                               self._preview_xy(dropped[-1]), 8, 2)
        if len(pts) >= 4:
            corners = pts[:4]
            pygame.draw.polygon(surface, (120, 255, 160),
                                [self._preview_xy(p) for p in corners], 2)
            self._draw_preview_mesh(surface, corners, (120, 255, 160))

    def _draw_space_overlay(self, surface):
        """Outline every group's spaces on the preview, highlight the
        picked-for-edit space + its corner handles, preview quad creation."""
        e = self.engine
        m = e.mapping
        rect = self._preview_rect
        sel_idx = m.selected
        for gi, group in enumerate(m.groups):
            is_selected_group = (gi == sel_idx)
            outline = (200, 220, 255) if is_selected_group else (90, 100, 130)
            for si, space in enumerate(group.spaces):
                pts = [(rect.x + c[0] * rect.w, rect.y + c[1] * rect.h)
                       for c in space.corners]
                pygame.draw.polygon(surface, outline, pts, 1)
                self._draw_preview_mesh(surface, space.corners, outline)
                cx = sum(p[0] for p in pts) / 4
                cy = sum(p[1] for p in pts) / 4
                tag = self.font_s.render(f"G{gi + 1}", True, outline)
                surface.blit(tag, (cx - tag.get_width() / 2,
                                   cy - tag.get_height() / 2))

        if m.selected_space is not None:
            gi, si = m.selected_space
            if 0 <= gi < len(m.groups) and 0 <= si < len(m.groups[gi].spaces):
                picked = m.groups[gi].spaces[si]
                pts = [(rect.x + c[0] * rect.w, rect.y + c[1] * rect.h)
                       for c in picked.corners]
                pygame.draw.polygon(surface, (255, 240, 120), pts, 2)
                self._draw_preview_mesh(surface, picked.corners, (255, 240, 120))
                drag = m.drag
                for ci, c in enumerate(picked.corners):
                    hx = int(rect.x + c[0] * rect.w)
                    hy = int(rect.y + c[1] * rect.h)
                    is_dragging = (drag is not None
                                   and drag.get("kind") == "corner"
                                   and drag.get("space") == (gi, si)
                                   and drag.get("corner") == ci)
                    is_kbd = (ci == m.selected_corner)
                    color = (255, 220, 80) if is_dragging else (255, 240, 180)
                    pygame.draw.circle(surface, (20, 20, 30), (hx, hy), 6)
                    pygame.draw.circle(surface, color, (hx, hy), 5)
                    pygame.draw.circle(surface, (40, 40, 60), (hx, hy), 5, 1)
                    if is_kbd:
                        pygame.draw.circle(surface, (90, 230, 255), (hx, hy), 8, 2)

        if m.drag is not None and m.drag.get("kind") == "create":
            sx, sy = m.drag["start"]
            cx, cy = m.drag["current"]
            x0 = int(rect.x + min(sx, cx) * rect.w)
            x1 = int(rect.x + max(sx, cx) * rect.w)
            y0 = int(rect.y + min(sy, cy) * rect.h)
            y1 = int(rect.y + max(sy, cy) * rect.h)
            pygame.draw.rect(surface, (200, 255, 200),
                             pygame.Rect(x0, y0, x1 - x0, y1 - y0), 1)
        self._draw_create_points_preview(surface)

    # ── Status panels ────────────────────────────────────────────────

    def _draw_mapping_status(self, surface, x, y, width):
        e = self.engine
        g = e.mapping.selected_group()
        lh = self.line_m
        title = self.font_h.render("MAPPING — selected group", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += self.line_h
        if e.mapping.banner_blank and not e.mapping.edit_mode:
            blank = self.font_s.render("— blank — (banner hidden)", True, (150, 170, 200))
            surface.blit(blank, (x, y))
            return y + lh
        if g is None:
            none = self.font_s.render("no groups", True, (180, 120, 120))
            surface.blit(none, (x, y))
            return y + lh
        n_spaces = len(g.spaces)
        spaces_label = f"{n_spaces} space{'' if n_spaces == 1 else 's'}"
        ap = "ON" if g.autopilot_enabled else "off"
        ap_color = (130, 220, 140) if g.autopilot_enabled else (130, 130, 150)
        self._row(surface, "NAME", g.name, x, y, width); y += lh
        self._row(surface, "SPACES", spaces_label, x, y, width); y += lh
        self._row(surface, "CONTENT", self._group_content_label(g), x, y, width); y += lh
        fx_on = [k for k, v in g.fx_state.items() if v]
        self._row(surface, "FX", ", ".join(fx_on) if fx_on else "—", x, y, width)
        y += lh + self._px(2)

        ls = self.font_m.render("AUTOPILOT", True, (130, 130, 160))
        surface.blit(ls, (x, y))
        chip = self.font_s.render(f" {ap} ", True, (20, 20, 30))
        chip_rect = chip.get_rect(topleft=(x + self._label_col, y + 1))
        pygame.draw.rect(surface, ap_color, chip_rect.inflate(self._px(4), self._px(2)),
                         border_radius=3)
        surface.blit(chip, chip_rect)
        info = self.font_s.render(
            f"  content {g.autopilot_interval_s:.0f}s · fx {g.autopilot_fx_interval_s:.0f}s",
            True, (180, 180, 200))
        surface.blit(info, (chip_rect.right + self._px(8),
                            y + (ls.get_height() - info.get_height()) // 2))
        y += lh + self._px(2)

        self._param_bar(surface, "PARAM X", g.param_x, x, y, width); y += lh
        self._param_bar(surface, "PARAM Y", g.param_y, x, y, width); y += lh
        armed = e.mapping.delete_group_armed
        if armed == e.mapping.selected:
            warn = self.font_s.render("Backspace again deletes this group · Esc cancels",
                                      True, (255, 190, 100))
            surface.blit(warn, (x, y))
            y += lh
        return y

    def _draw_groups_list(self, surface, x, y, width):
        e = self.engine
        title = self.font_h.render(
            f"GROUPS  ({len(e.mapping.groups)})  — Tab cycles", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += self.line_h
        bx = x
        chip_h = self.line_m + self._px(6)
        for gi, group in enumerate(e.mapping.groups):
            is_sel = (gi == e.mapping.selected)
            ap_dot = " ●" if group.autopilot_enabled else ""
            label = self.font_m.render(
                f" {group.name} ({len(group.spaces)}){ap_dot} ",
                True, (255, 255, 255) if is_sel else (220, 220, 240))
            rect = pygame.Rect(bx, y, label.get_width() + self._px(10), chip_h)
            if rect.right > x + width:
                bx = x
                y += chip_h + self._px(4)
                rect = pygame.Rect(bx, y, label.get_width() + self._px(10), chip_h)
            bg = (70, 130, 200) if is_sel else (38, 42, 58)
            border = (160, 220, 255) if is_sel else (90, 110, 140)
            pygame.draw.rect(surface, bg, rect, border_radius=4)
            pygame.draw.rect(surface, border, rect, 1, border_radius=4)
            surface.blit(label, (rect.x + self._px(5),
                                 rect.y + (chip_h - label.get_height()) // 2))
            bx = rect.right + self._px(6)
        return y + chip_h + self._px(4)

    def _draw_status(self, surface, x, y, width):
        e = self.engine
        lh = self.line_m
        if e.mode == "cinematic" or getattr(e, "cinematic_status", "off") != "off":
            title = self.font_h.render("4K CINEMATIC", True, (220, 220, 240))
            surface.blit(title, (x, y))
            y += self.line_h
            self._row(surface, "STATUS", getattr(e, "cinematic_status", "off"),
                      x, y, width); y += lh
            self._row(surface, "SOURCE", getattr(e, "cinematic_source", None) or "—",
                      x, y, width); y += lh
            self._row(surface, "KEYS", "Esc/N exit · -/= prev/next",
                      x, y, width); y += lh
            return y

        clip_text = self._library_label(e.clips)
        gen_text = self._generator_label(e)
        fx_on = [k for k, v in e.fx_state.items() if v]
        fx_text = ", ".join(fx_on) if fx_on else "—"

        title = self.font_h.render("NOW PLAYING", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += self.line_h

        self._row(surface, "CLIP", clip_text, x, y, width); y += lh
        self._row(surface, "GEN", gen_text, x, y, width); y += lh
        if getattr(e, "camera_active", False) or e.camera is not None:
            self._row(surface, "CAM", self._camera_label(e), x, y, width); y += lh
        if getattr(e, "face_active", False) or len(getattr(e, "faces", [])):
            face_name = e.faces.name() or "—"
            n = len(e.faces)
            label = (f"{face_name}  ({e.faces.idx + 1}/{n})" if n else
                     "no faces — run Capture Face.sh")
            self._row(surface, "FACE", label, x, y, width); y += lh
        self._row(surface, "FX", fx_text, x, y, width)
        y += lh + self._px(2)

        self._param_bar(surface, "PARAM X", e.param_x, x, y, width); y += lh
        self._param_bar(surface, "PARAM Y", e.param_y, x, y, width); y += lh
        return y

    def _draw_badges(self, surface, x, y):
        e = self.engine
        chip_h = self.line_m + self._px(6)
        msg = getattr(e, "hud_message", None)
        if msg:
            text, expiry = msg
            if time.monotonic() < expiry:
                chip = self.font_m.render(f"  {text}  ", True, (20, 20, 30))
                rect = chip.get_rect(topleft=(x, y))
                pygame.draw.rect(surface, (255, 230, 120),
                                 rect.inflate(self._px(4), self._px(4)), border_radius=4)
                surface.blit(chip, rect)
                y += chip_h + self._px(4)
            else:
                e.hud_message = None
        badges = []
        if e.mode == "mapping":
            if e.mapping.edit_mode:
                badges.append(("MAPPING · EDIT", (255, 240, 120)))
            else:
                badges.append(("MAPPING · PERFORM", (160, 220, 255)))
            if e.mapping.bind_armed:
                badges.append(("BIND: next click", (200, 255, 200)))
        if e.mode == "cinematic":
            badges.append(("4K CINEMATIC", (160, 220, 255)))
        elif getattr(e, "cinematic_status", "off") != "off":
            badges.append(("4K: " + e.cinematic_status, (255, 200, 80)))
        if getattr(e, "camera_active", False):
            badges.append(("LIVE CAM", (255, 150, 200)))
        if getattr(e, "face_active", False):
            badges.append(("FACE CLOUD", (180, 200, 255)))
        if getattr(e, "auto_mode", False):
            badges.append(("AUTOPILOT", (120, 220, 140)))
        if getattr(e, "mushroom_mode", False):
            mush = getattr(e, "mushroom", None)
            linked = mush is not None and mush.connected()
            badges.append(("MUSHROOM" if linked else "MUSHROOM · link…",
                           (210, 130, 255) if linked else (150, 110, 160)))
        if getattr(e, "_bright_scale", 1.0) < 0.97:
            cut = int(round((1.0 - e._bright_scale) * 100))
            badges.append((f"DIM −{cut}%", (255, 230, 120)))
        if e.blackout:
            badges.append(("BLACKOUT", (255, 80, 80)))
        if e.freeze:
            badges.append(("FREEZE", (130, 200, 255)))
        if e.hit_frames_left > 0:
            badges.append((f"HIT: {e.hit_type}", (255, 200, 80)))
        end_y = y
        if badges:
            bx = x
            win_w = self.size[0]
            for text, color in badges:
                chip = self.font_m.render(f"  {text}  ", True, (20, 20, 30))
                rect = chip.get_rect(topleft=(bx, y))
                if rect.right > win_w - self.pad:
                    bx = x
                    y += chip_h + self._px(4)
                    rect = chip.get_rect(topleft=(bx, y))
                pygame.draw.rect(surface, color, rect.inflate(self._px(4), self._px(4)),
                                 border_radius=4)
                surface.blit(chip, rect)
                bx += rect.width + self._px(10)
            end_y = y + chip_h + self._px(4)
        if getattr(e, "auto_mode", False):
            info = self.font_s.render(
                f"clip every {e.auto_clip_interval:4.1f}s  ·  "
                f"fx every {e.auto_fx_interval:4.1f}s  ·  ↑↓ clip · ←→ fx",
                True, (150, 220, 170))
            surface.blit(info, (x, end_y))
            end_y += info.get_height() + self._px(2)
        return end_y

    def _draw_favorites(self, surface, x, y, width, label, keys, favs,
                        active_stem=None):
        """Render one row of 10 favourite-slot chips."""
        cell_h = self.line_m + self._px(6)
        gap = self._px(3)
        title = self.font_m.render(label, True, (165, 165, 185))
        surface.blit(title, (x, y + max(0, (cell_h - title.get_height()) // 2)))
        slot_x = x + title.get_width() + self._px(10)
        cell_w = max(self._px(40), (x + width - slot_x - 9 * gap) // 10)
        char_w = max(5, self.font_s.size("0")[0])
        for i, (k, stem) in enumerate(zip(keys, favs)):
            rect = pygame.Rect(slot_x + i * (cell_w + gap), y, cell_w, cell_h)
            assigned = stem is not None
            is_active = assigned and active_stem == stem
            if is_active:
                bg = (70, 130, 200); border = (160, 220, 255); fg = (255, 255, 255)
            elif assigned:
                bg = (38, 42, 58); border = (90, 110, 140); fg = (210, 220, 240)
            else:
                bg = (28, 30, 40); border = (55, 55, 70); fg = (95, 95, 110)
            pygame.draw.rect(surface, bg, rect, border_radius=3)
            pygame.draw.rect(surface, border, rect, 1, border_radius=3)
            txt = stem or "—"
            max_chars = max(2, (cell_w - self._px(8)) // char_w - 2)
            if len(txt) > max_chars:
                txt = txt[:max_chars - 1] + "…"
            label_s = self.font_s.render(f"{k}·{txt}", True, fg)
            surface.blit(label_s, (rect.x + self._px(3),
                                   rect.y + (cell_h - label_s.get_height()) // 2))
        return y + cell_h + self._px(2)

    def _draw_display_selector(self, surface, x, y, width):
        e = self.engine
        title = self.font_h.render("OUTPUT DISPLAY", True, (220, 220, 240))
        surface.blit(title, (x, y))
        hint = self.font_s.render("(F11 cycle · F12 apply)", True, (140, 140, 170))
        surface.blit(hint, (x + title.get_width() + self._px(8),
                            y + (title.get_height() - hint.get_height())))
        y += self.line_h

        btn_h = self.line_m + self._px(6)
        self._display_btn_rects = []
        bx = x
        cur = self.font_s.render(
            f"current {e.cfg.display} · pending {e.pending_display}",
            True, (170, 170, 195))
        for idx in range(e.num_displays):
            label = self.font_m.render(f" Display {idx} ", True, (240, 240, 255))
            rect = pygame.Rect(bx, y, label.get_width() + self._px(12), btn_h)
            is_current = idx == e.cfg.display
            is_pending = idx == e.pending_display
            if is_pending:
                bg = (70, 130, 200); border = (140, 200, 255)
            else:
                bg = (40, 44, 60); border = (90, 90, 120)
            pygame.draw.rect(surface, bg, rect, border_radius=4)
            pygame.draw.rect(surface, border, rect, 1, border_radius=4)
            surface.blit(label, (rect.x + self._px(6),
                                 rect.y + (btn_h - label.get_height()) // 2))
            if is_current:
                tick = self.font_s.render("✓", True, (180, 255, 180))
                surface.blit(tick, (rect.right - self._px(12), rect.y + 2))
            self._display_btn_rects.append((idx, rect))
            bx = rect.right + self._px(8)

        # Apply button — right-aligned.
        apply_label = self.font_m.render(" APPLY ", True, (20, 22, 30))
        apply_rect = pygame.Rect(0, y, apply_label.get_width() + self._px(14), btn_h)
        apply_rect.right = x + width
        enabled = e.pending_display != e.cfg.display
        bg = (120, 220, 140) if enabled else (60, 70, 60)
        pygame.draw.rect(surface, bg, apply_rect, border_radius=4)
        pygame.draw.rect(surface, (40, 80, 40), apply_rect, 1, border_radius=4)
        surface.blit(apply_label, (apply_rect.x + self._px(7),
                                   apply_rect.y + (btn_h - apply_label.get_height()) // 2))
        self._apply_rect = apply_rect

        surface.blit(cur, (x, y + btn_h + self._px(2)))
        return y + btn_h + cur.get_height() + self._px(6)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _library_label(pool):
        total = len(pool)
        if total == 0:
            return "— (empty)"
        idx = pool.active_idx
        if idx is None:
            return f"—  [0/{total}]"
        return f"{pool.name(idx)}  [{idx + 1}/{total}]"

    @staticmethod
    def _generator_label(e):
        name = e.active_generative
        if not name:
            return "—"
        from engine import GENERATIVES
        total = len(GENERATIVES)
        try:
            idx = GENERATIVES.index(name) + 1
        except ValueError:
            idx = e.current_generator_idx + 1
        disp = name[3:] if name.startswith("pm:") else name
        return f"[{idx}/{total}] {disp}"

    @staticmethod
    def _group_content_label(g):
        if g.content_kind == "generative" and g.gen_name:
            from engine import GENERATIVES
            name = g.gen_name
            disp = name[3:] if name.startswith("pm:") else name
            try:
                idx = GENERATIVES.index(name) + 1
                return f"gen:  [{idx}/{len(GENERATIVES)}] {disp}"
            except ValueError:
                return f"gen:  {disp}"
        return g.content_label()

    @staticmethod
    def _camera_label(e):
        cam = e.camera
        if cam is None:
            return "—"
        mirror = "  ·  mirror" if getattr(cam, "mirror", False) else ""
        if getattr(e, "camera_active", False):
            dev = (f"/dev/video{cam.opened_index}"
                   if cam.opened_index is not None else cam.status)
            return f"LIVE  ({dev}){mirror}"
        return f"{cam.status}{mirror}"

    def _row(self, surface, label, value, x, y, width=None):
        if width is None:
            width = surface.get_width() - x - self.pad
        ls = self.font_m.render(label, True, (135, 135, 165))
        vx = x + self._label_col
        avail = width - self._label_col
        char_w = max(6, self.font_m.size("0")[0])
        max_chars = max(6, int(avail / char_w))
        sval = str(value)
        if len(sval) > max_chars:
            sval = sval[:max_chars - 1] + "…"
        vs = self.font_m.render(sval, True, (240, 240, 255))
        surface.blit(ls, (x, y))
        surface.blit(vs, (vx, y))

    def _param_bar(self, surface, label, value, x, y, width=None):
        if width is None:
            width = self._px(300)
        ls = self.font_m.render(label, True, (135, 135, 165))
        surface.blit(ls, (x, y))
        bar_x = x + self._label_col
        val_w = self._px(46)
        bar_w = max(self._px(60), width - self._label_col - val_w)
        bar_h = max(8, self._px(9))
        bar_y = y + (self.line_m - bar_h) // 2
        pygame.draw.rect(surface, (40, 44, 60), (bar_x, bar_y, bar_w, bar_h),
                         border_radius=2)
        fill_w = int(bar_w * max(0.0, min(1.0, value)))
        pygame.draw.rect(surface, (140, 200, 255), (bar_x, bar_y, fill_w, bar_h),
                         border_radius=2)
        pygame.draw.rect(surface, (90, 90, 120), (bar_x, bar_y, bar_w, bar_h), 1,
                         border_radius=2)
        vs = self.font_s.render(f"{value:.2f}", True, (200, 200, 220))
        surface.blit(vs, (bar_x + bar_w + self._px(6),
                          y + (self.line_m - vs.get_height()) // 2))
