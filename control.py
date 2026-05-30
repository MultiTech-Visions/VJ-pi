"""Control HUD window — runs on the second display (small screen).
Live preview, status panel, favourite-slot grids, output-display picker,
and a tabbed bottom panel (clips / generators / scenes / settings / stats)
— laid out top-to-bottom with the preview and status sharing the top row to
claim back horizontal space. The key cheat sheet now lives behind a clickable
Help (?) popup instead of permanently occupying the bottom half.
"""
import pygame

from shader_catalog import GPU_GENERATOR_ORDER


KEY_CHEAT = [
    ("− / =",        "Prev / next CLIP (hold to scrub)"),
    ("[ / ]",        "Prev / next GENERATOR (hold to scan)"),
    ("1 - 0",        "Clip favourites — tap=play, hold ≥½s=assign current"),
    ("A-L ;",        "Generator favourites — tap=play, hold ≥½s=assign current"),
    ("Z X C V B",    "Hits: strobe / black / inv / zoom / RGB"),
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
    ("Tab",          "Next group (Shift+Tab = prev)"),
    ("EDIT — drag empty",  "rubber-band a new rectangle → new group"),
    ("EDIT — click body",  "pick that space (handles + toolbar appear)"),
    ("EDIT — drag body",   "move the whole space"),
    ("EDIT — drag corner", "reshape the picked space"),
    ("EDIT — toolbar ×",   "delete this space"),
    ("EDIT — toolbar +",   "bind this space into the selected space's group"),
    ("EDIT — toolbar ⊘",   "unbind this space into its own new group"),
    ("EDIT — toolbar frame", "mode / zoom / pan / reset selected group's video"),
    ("EDIT — toolbar G·",  "tag chip = which group this space belongs to"),
    ("EDIT — −/= [/]", "cycle clip / generator for selected box's group"),
    ("EDIT — Esc",   "cancel drag / deselect"),
    ("Ctrl+N",       "New group"),
    ("Ctrl+Back",    "Delete current group"),
    ("Ctrl+= / -",   "Add / remove a space in current group"),
    ("Ctrl+G",       "Cycle grid layout (1·2x1·2x2·3x2·3x3·4x2·4x3)"),
    ("Ctrl+A",       "Toggle autopilot on current group"),
    ("Ctrl+K",       "Cycle autopilot kind"),
    ("Ctrl+, / .",   "Autopilot interval ±1s"),
    ("PERFORM — content keys", "1-0/A-L;/[]/F1-F8/←→↑↓ → selected group"),
]


class ControlWindow:
    """Renders the control HUD into a second pygame window.

    Pygame 2's SDL2 Window doesn't expose a get_surface(), so we render
    each frame onto an off-screen Surface, then upload that as a Texture
    via the Window's Renderer and present.
    """

    PREVIEW_TARGET_H = 180  # tall enough to read, narrow enough to share the row

    def __init__(self, engine, window, renderer, size, preview_size,
                 software_surface=None):
        # Texture is only needed for the GPU present path (renderer set).
        Texture = None
        if renderer is not None:
            from pygame._sdl2.video import Texture
        self.engine = engine
        self.window = window
        self.renderer = renderer  # None when the output owns the GL renderer
        # software_surface set (under --gpu-scale): the HUD is the
        # pygame.display window and we present by blitting to it + flip().
        self.software_surface = software_surface
        self.size = size  # (w, h)
        self.surface = pygame.Surface(size)

        # Preview keeps source aspect, fixed target height. Width is then
        # capped so the status panel beside it always has at least 280px.
        target_h = self.PREVIEW_TARGET_H
        src_w, src_h = preview_size
        pw = int(src_w * target_h / max(1, src_h))
        ph = target_h
        max_pw = max(200, size[0] - 280 - 36)
        if pw > max_pw:
            pw = max_pw
            ph = int(src_h * pw / max(1, src_w))
        self.preview_w, self.preview_h = pw, ph

        self._Texture = Texture
        self.font_h = pygame.font.SysFont("Sans,Arial,DejaVuSans", 18, bold=True)
        self.font_m = pygame.font.SysFont("Sans,Arial,DejaVuSans", 14)
        self.font_s = pygame.font.SysFont("Sans,Arial,DejaVuSans", 12)

        # Hit-test rects, populated each frame in render().
        self._display_btn_rects = []  # [(idx, pygame.Rect), ...]
        self._apply_rect = None
        # Preview rect kept current each frame so corner-handle hit tests
        # know where the preview lives.
        self._preview_rect = pygame.Rect(0, 0, self.preview_w, self.preview_h)
        try:
            self._window_id = window.id
        except AttributeError:
            self._window_id = None
        self._cheat_panel = self._build_cheat_panel(KEY_CHEAT)
        self._mapping_cheat_panel = self._build_cheat_panel(MAPPING_KEY_CHEAT)

        # ── Tabbed bottom panel state ────────────────────────────────
        # Tabs replace the always-on cheat sheet. The keys list now lives
        # behind the Help (?) popup.
        self.active_tab = "clips"
        self._tab_rects = []          # [(tab_id, pygame.Rect), ...]
        self._help_btn_rect = None
        self._help_open = False
        # Per-list scroll offset (in rows), keyed by tab id.
        self._scroll = {"clips": 0, "gens": 0, "scenes": 0}
        # Clickable list rows for the active tab: [(idx, pygame.Rect), ...]
        self._list_row_rects = []

    # ── Event handling ───────────────────────────────────────────────

    def handle_event(self, event):
        """Forward mouse activity from the control window to its buttons,
        the tabbed picker panel, and (in mapping/edit mode) the spaces
        editor."""
        # Wheel scrolls the active picker list. The engine forwards wheel
        # events to the HUD only, and they don't reliably carry a source
        # window id, so handle them before the per-window filter.
        if event.type == pygame.MOUSEWHEEL:
            self._scroll_active(-getattr(event, "y", 0))
            return

        if not self._event_is_ours(event):
            return
        e = self.engine

        if event.type == pygame.MOUSEMOTION:
            pos = getattr(event, "pos", None)
            if (e.mode == "mapping" and e.mapping.edit_mode
                    and pos is not None
                    and self._preview_rect.collidepoint(pos)):
                norm = self._preview_to_norm(pos)
                if e.mapping.drag is not None:
                    e.mapping.update_drag(norm)
                else:
                    e.mapping.update_hover(norm)
            elif e.mode == "mapping" and e.mapping.drag is not None:
                # Drag continues even if the cursor briefly leaves the
                # preview (matches usual UX for click+drag).
                if pos is not None:
                    e.mapping.update_drag(self._preview_to_norm(pos))
            return

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if e.mode == "mapping" and e.mapping.drag is not None:
                e.mapping.end_drag()
                e._persist_mapping()
            return

        if event.type != pygame.MOUSEBUTTONDOWN or event.button != 1:
            return
        pos = getattr(event, "pos", None)
        if pos is None:
            return

        # Help popup is modal — any click dismisses it and is consumed so it
        # can't fall through to whatever sits underneath.
        if self._help_open:
            self._help_open = False
            return

        if (e.mode == "mapping" and e.mapping.edit_mode
                and self._preview_rect.collidepoint(pos)):
            # Delegate to the engine's shared click handler so the HUD
            # preview and the projector share one source of truth for
            # edit-mode gestures (and any future ones).
            e._mapping_handle_click(self._preview_to_norm(pos))
            return

        for idx, rect in self._display_btn_rects:
            if rect.collidepoint(pos):
                self.engine.pending_display = idx
                return
        if self._apply_rect is not None and self._apply_rect.collidepoint(pos):
            self.engine.apply_pending_display()
            return

        # Tabbed bottom panel — tab bar, Help button, and list rows.
        self._handle_panel_click(pos)

    def _scroll_active(self, delta):
        """Scroll the active tab's list by `delta` rows (clamped to >= 0;
        the upper bound is clamped at draw time once row counts are known)."""
        if self.active_tab in self._scroll:
            self._scroll[self.active_tab] = max(
                0, self._scroll[self.active_tab] + delta)

    def _handle_panel_click(self, pos):
        """Route a click within the tabbed bottom panel. Returns True if the
        click hit something."""
        if self._help_btn_rect is not None and self._help_btn_rect.collidepoint(pos):
            self._help_open = not self._help_open
            return True
        for tab_id, rect in self._tab_rects:
            if rect.collidepoint(pos):
                if tab_id != self.active_tab:
                    self.active_tab = tab_id
                    self._scroll_to_active(tab_id)
                return True
        for idx, rect in self._list_row_rects:
            if rect.collidepoint(pos):
                self._activate_list_item(idx)
                return True
        return False

    def _activate_list_item(self, idx):
        """A picker row was clicked. Clips/generators route through the same
        engine entry points the keyboard uses, so in mapping mode they target
        the selected group automatically."""
        if self.active_tab == "clips":
            self.engine.select_clip(idx)
        elif self.active_tab == "gens":
            self.engine.select_generative(idx)
        # scenes / settings / stats: no row actions yet (later phases).

    def _scroll_to_active(self, tab_id):
        """When a tab is opened, scroll its list so the current selection is
        roughly centred — so the clips tab opens on the playing clip."""
        active = self._active_index_for(tab_id)
        if active is None or active < 0:
            return
        # ~6 rows of context above; draw-time clamping keeps it in range.
        self._scroll[tab_id] = max(0, active - 6)

    def _active_index_for(self, tab_id):
        """Index of the currently-selected item for a picker tab, accounting
        for mapping mode (where the selected group's content is what's live).
        Returns None/-1 when nothing matches."""
        e = self.engine
        group = (e.mapping.selected_group()
                 if e.mode == "mapping" else None)
        if tab_id == "clips":
            if group is not None:
                if group.content_kind == "clip" and group.clip_stem:
                    return e.clips.find_by_stem(group.clip_stem)
                return -1
            return e.clips.active_idx if e.clips.active_idx is not None else -1
        if tab_id == "gens":
            name = (group.gen_name if (group is not None
                    and group.content_kind == "generative")
                    else (e.active_generative if group is None else None))
            if name in GPU_GENERATOR_ORDER:
                return GPU_GENERATOR_ORDER.index(name)
            return -1
        return None

    def _preview_to_norm(self, pos):
        """Convert a preview-window click position into normalized (0..1)
        output coords. Clamped so out-of-preview drags still produce a
        valid corner position."""
        rect = self._preview_rect
        nx = (pos[0] - rect.x) / max(1, rect.w)
        ny = (pos[1] - rect.y) / max(1, rect.h)
        return (max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny)))

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
        surface.fill((18, 20, 28))
        win_w, win_h = self.size
        pad = 12
        e = self.engine
        mapping_mode = e.mode == "mapping"

        # ── Top row: preview (left) + status (right) ─────────────
        self._draw_preview(surface, pad, pad, frame)
        if mapping_mode:
            self._draw_space_overlay(surface)
        sx = pad + self.preview_w + 16
        if mapping_mode:
            self._draw_mapping_status(surface, sx, pad, win_w - sx - pad)
        else:
            self._draw_status(surface, sx, pad, win_w - sx - pad)

        # FPS readout on its own line just under the preview — big and
        # colour-coded (green good / amber marginal / red bad) so it stays
        # legible on the small 7" operator screen.
        y = pad + self.preview_h + 6
        fps = getattr(e, "fps_measured", 0.0)
        fcol = (110, 230, 130) if fps >= 25.0 else (245, 215, 110) if fps >= 15.0 else (240, 90, 90)
        fps_surf = self.font_h.render("%.0f FPS" % fps, True, fcol)
        surface.blit(fps_surf, (pad, y))
        y += fps_surf.get_height() + 4

        # In mapping mode, break the frame time down by phase so we can see
        # where it goes (clip decode / generator / per-group FX / warp).
        if mapping_mode:
            p = getattr(e, "_perf_ms", None)
            if p:
                disp = getattr(e, "_disp_ms", 0.0)
                btxt = "clip %.0f · gen %.0f · fx %.0f · warp %.0f · disp %.0f ms" % (
                    p["clip"], p["gen"], p["fx"], p["warp"], disp)
                bsurf = self.font_m.render(btxt, True, (175, 180, 200))
                surface.blit(bsurf, (pad, y))
                y += bsurf.get_height() + 6
                # Mapping render resolution (F9/F10) — show the % and the
                # actual internal pixels so the operator sees the quality/
                # speed dial they're turning.
                rs = getattr(e.mapping, "render_scale", 1.0)
                rtxt = "map res %d%%  (%d×%d)  F9/F10" % (
                    round(rs * 100), int(e.cfg.width * rs), int(e.cfg.height * rs))
                rcol = (150, 200, 150) if rs >= 0.999 else (210, 190, 120)
                rsurf = self.font_m.render(rtxt, True, rcol)
                surface.blit(rsurf, (pad, y))
                y += rsurf.get_height() + 6
        else:
            y += 4

        # Badges (only renders if anything to show; returns same y otherwise)
        y = self._draw_badges(surface, pad, y)

        if mapping_mode:
            y = self._draw_groups_list(surface, pad, y, win_w - pad * 2)
            y += 6

        # Favourites grids — clarify they target the selected group when in
        # mapping mode (header gets a "→ Group N" suffix).
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
        y = self._draw_favorites(surface, pad, y, win_w - pad * 2,
                                 clip_header, "1234567890",
                                 e.clip_favorites,
                                 active_stem=active_clip_stem)
        y += 2
        y = self._draw_favorites(surface, pad, y, win_w - pad * 2,
                                 gen_header, "ASDFGHJKL;",
                                 e.generator_favorites,
                                 active_stem=active_gen_name)
        y += 12

        # Display selector
        y = self._draw_display_selector(surface, pad, y, win_w - pad * 2)
        y += 10

        # Tabbed bottom panel (clips / generators / scenes / settings /
        # stats) — replaces the old always-on cheat sheet, which now lives
        # behind the Help (?) popup.
        panel_h = max(0, win_h - pad - y)
        self._draw_tab_panel(surface, pad, y, win_w - pad * 2, panel_h)

        # Help popup draws last so it sits above everything else.
        if self._help_open:
            self._draw_help_overlay(surface)

        # Present the composed surface. Either the HUD owns the single GL
        # context (renderer set → texture upload), or — under --gpu-scale —
        # the HUD is the pygame.display software window (blit + flip). One GL
        # context in the process either way.
        if self.renderer is not None:
            tex = self._Texture.from_surface(self.renderer, surface)
            self.renderer.clear()
            tex.draw()
            self.renderer.present()
        elif self.software_surface is not None:
            self.software_surface.blit(surface, (0, 0))
            pygame.display.flip()

    # ── Panel parts ──────────────────────────────────────────────────

    def _draw_preview(self, surface, x, y, frame):
        self._preview_rect = pygame.Rect(x, y, self.preview_w, self.preview_h)
        if frame is not None:
            preview = pygame.image.frombuffer(
                frame.tobytes(), (frame.shape[1], frame.shape[0]), "RGB"
            )
            preview = pygame.transform.scale(preview,
                                             (self.preview_w, self.preview_h))
            surface.blit(preview, (x, y))
        else:
            pygame.draw.rect(surface, (40, 40, 50),
                             (x, y, self.preview_w, self.preview_h))
        pygame.draw.rect(surface, (90, 90, 120),
                         (x, y, self.preview_w, self.preview_h), 1)

    def _draw_space_overlay(self, surface):
        """Outline every group's spaces on the preview, highlight the
        picked-for-edit space, draw corner handles on the picked space,
        and rubber-band the create-drag rectangle if one is in flight."""
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
                # Tiny group-index tag at the centre so it's clear which
                # spaces belong together.
                cx = sum(p[0] for p in pts) / 4
                cy = sum(p[1] for p in pts) / 4
                tag = self.font_s.render(f"G{gi + 1}", True, outline)
                surface.blit(tag, (cx - tag.get_width() / 2,
                                   cy - tag.get_height() / 2))

        # Picked space + handles.
        if m.selected_space is not None:
            gi, si = m.selected_space
            if 0 <= gi < len(m.groups) and 0 <= si < len(m.groups[gi].spaces):
                picked = m.groups[gi].spaces[si]
                pts = [(rect.x + c[0] * rect.w, rect.y + c[1] * rect.h)
                       for c in picked.corners]
                pygame.draw.polygon(surface, (255, 240, 120), pts, 2)
                drag = m.drag
                for ci, c in enumerate(picked.corners):
                    hx = int(rect.x + c[0] * rect.w)
                    hy = int(rect.y + c[1] * rect.h)
                    is_dragging = (drag is not None
                                   and drag.get("kind") == "corner"
                                   and drag.get("space") == (gi, si)
                                   and drag.get("corner") == ci)
                    color = (255, 220, 80) if is_dragging else (255, 240, 180)
                    pygame.draw.circle(surface, (20, 20, 30), (hx, hy), 6)
                    pygame.draw.circle(surface, color, (hx, hy), 5)
                    pygame.draw.circle(surface, (40, 40, 60), (hx, hy), 5, 1)

        # Rubber-band rectangle while drag-creating.
        if m.drag is not None and m.drag.get("kind") == "create":
            sx, sy = m.drag["start"]
            cx, cy = m.drag["current"]
            x0 = int(rect.x + min(sx, cx) * rect.w)
            x1 = int(rect.x + max(sx, cx) * rect.w)
            y0 = int(rect.y + min(sy, cy) * rect.h)
            y1 = int(rect.y + max(sy, cy) * rect.h)
            pygame.draw.rect(surface, (200, 255, 200),
                             pygame.Rect(x0, y0, x1 - x0, y1 - y0), 1)

        # Hover toolbars — render on the selected space always, and on the
        # hovered space when it's a different one. Reuses the manager's
        # normalized-coord layout so the projector and HUD show the same
        # buttons in the same relative positions.
        for cand in {m.selected_space, m.hovered_space} - {None}:
            self._draw_preview_toolbar(surface, *cand)

    def _draw_preview_toolbar(self, surface, gi, si):
        m = self.engine.mapping
        rect = self._preview_rect
        for kind, (nx, ny, nw, nh) in m.hover_toolbar_buttons(gi, si):
            x0 = int(rect.x + nx * rect.w)
            y0 = int(rect.y + ny * rect.h)
            x1 = int(rect.x + (nx + nw) * rect.w)
            y1 = int(rect.y + (ny + nh) * rect.h)
            btn_rect = pygame.Rect(x0, y0, x1 - x0, y1 - y0)
            pygame.draw.rect(surface, (28, 30, 40), btn_rect, border_radius=3)
            pygame.draw.rect(surface, (200, 210, 230), btn_rect, 1, border_radius=3)
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            r = max(2, min(x1 - x0, y1 - y0) // 4)
            if kind == "delete":
                pygame.draw.line(surface, (255, 90, 90),
                                 (cx - r, cy - r), (cx + r, cy + r), 2)
                pygame.draw.line(surface, (255, 90, 90),
                                 (cx + r, cy - r), (cx - r, cy + r), 2)
            elif kind == "bind":
                pygame.draw.line(surface, (140, 230, 140),
                                 (cx - r, cy), (cx + r, cy), 2)
                pygame.draw.line(surface, (140, 230, 140),
                                 (cx, cy - r), (cx, cy + r), 2)
            elif kind == "unbind":
                pygame.draw.line(surface, (255, 180, 80),
                                 (cx - r, cy + r), (cx + r, cy - r), 2)
                pygame.draw.rect(surface, (28, 30, 40),
                                 pygame.Rect(cx - 1, cy - 1, 3, 3))
            elif kind in {
                    "fit_mode", "zoom_out", "zoom_in",
                    "pan_left", "pan_right", "pan_up", "pan_down",
                    "reset_frame",
            }:
                group = m.groups[gi]
                labels = {
                    "fit_mode": group.fit_mode.upper()[:5],
                    "zoom_out": "-",
                    "zoom_in": "+",
                    "pan_left": "<",
                    "pan_right": ">",
                    "pan_up": "^",
                    "pan_down": "v",
                    "reset_frame": "0",
                }
                label = self.font_s.render(labels[kind], True, (160, 220, 255))
                surface.blit(label,
                             (cx - label.get_width() // 2,
                              cy - label.get_height() // 2))
            elif kind == "group":
                label = self.font_s.render(f"G{gi + 1}", True, (220, 230, 250))
                surface.blit(label,
                             (cx - label.get_width() // 2,
                              cy - label.get_height() // 2))

    def _draw_mapping_status(self, surface, x, y, width):
        e = self.engine
        g = e.mapping.selected_group()
        title = self.font_h.render("MAPPING — selected group",
                                   True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += 22
        if g is None:
            none = self.font_s.render("no groups", True, (180, 120, 120))
            surface.blit(none, (x, y))
            return
        n_spaces = len(g.spaces)
        spaces_label = f"{n_spaces} space{'' if n_spaces == 1 else 's'}"
        ap = "ON" if g.autopilot_enabled else "off"
        ap_color = (130, 220, 140) if g.autopilot_enabled else (130, 130, 150)
        self._row(surface, "NAME", g.name, x, y, width); y += 19
        self._row(surface, "SPACES", spaces_label, x, y, width); y += 19
        self._row(surface, "CONTENT", g.content_label(), x, y, width); y += 19
        fx_on = [k for k, v in g.fx_state.items() if v]
        self._row(surface, "FX", ", ".join(fx_on) if fx_on else "—",
                  x, y, width); y += 22

        # Autopilot row with a coloured chip.
        ls = self.font_m.render("AUTOPILOT", True, (130, 130, 160))
        surface.blit(ls, (x, y))
        chip = self.font_s.render(f" {ap} ", True, (20, 20, 30))
        chip_rect = chip.get_rect(topleft=(x + 88, y + 1))
        pygame.draw.rect(surface, ap_color, chip_rect.inflate(4, 2),
                         border_radius=3)
        surface.blit(chip, chip_rect)
        info = self.font_s.render(
            f"  {g.autopilot_kind}  ·  every {g.autopilot_interval_s:.0f}s",
            True, (180, 180, 200),
        )
        surface.blit(info, (chip_rect.right + 8,
                            y + (ls.get_height() - info.get_height()) // 2))
        y += 22

        self._param_bar(surface, "PARAM X", g.param_x, x, y, width); y += 18
        self._param_bar(surface, "PARAM Y", g.param_y, x, y, width); y += 22

    def _draw_groups_list(self, surface, x, y, width):
        e = self.engine
        title = self.font_h.render(
            f"GROUPS  ({len(e.mapping.groups)})  — Tab cycles",
            True, (220, 220, 240),
        )
        surface.blit(title, (x, y))
        y += 22
        bx = x
        chip_h = 22
        for gi, group in enumerate(e.mapping.groups):
            is_sel = (gi == e.mapping.selected)
            ap_dot = " ●" if group.autopilot_enabled else ""
            label = self.font_m.render(
                f" {group.name} ({len(group.spaces)}){ap_dot} ",
                True, (255, 255, 255) if is_sel else (220, 220, 240),
            )
            rect = pygame.Rect(bx, y, label.get_width() + 10, chip_h)
            if rect.right > x + width:
                bx = x
                y += chip_h + 4
                rect = pygame.Rect(bx, y, label.get_width() + 10, chip_h)
            bg = (70, 130, 200) if is_sel else (38, 42, 58)
            border = (160, 220, 255) if is_sel else (90, 110, 140)
            pygame.draw.rect(surface, bg, rect, border_radius=4)
            pygame.draw.rect(surface, border, rect, 1, border_radius=4)
            surface.blit(label, (rect.x + 5,
                                 rect.y + (chip_h - label.get_height()) // 2))
            bx = rect.right + 6
        return y + chip_h + 4

    def _draw_status(self, surface, x, y, width):
        e = self.engine
        if e.mode == "cinematic" or getattr(e, "cinematic_status", "off") != "off":
            title = self.font_h.render("4K CINEMATIC", True, (220, 220, 240))
            surface.blit(title, (x, y))
            y += 22
            self._row(surface, "STATUS", getattr(e, "cinematic_status", "off"),
                      x, y, width); y += 19
            self._row(surface, "SOURCE", getattr(e, "cinematic_source", None) or "—",
                      x, y, width); y += 19
            self._row(surface, "KEYS", "N/Esc exit · -/= prev/next",
                      x, y, width); y += 22
            return

        clip_text = self._library_label(e.clips)
        gen_name = e.active_generative or "—"
        fx_on = [k for k, v in e.fx_state.items() if v]
        fx_text = ", ".join(fx_on) if fx_on else "—"

        title = self.font_h.render("NOW PLAYING", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += 22

        self._row(surface, "CLIP",    clip_text, x, y, width); y += 19
        self._row(surface, "GEN",     gen_name,  x, y, width); y += 19
        self._row(surface, "FX",      fx_text,   x, y, width); y += 22

        self._param_bar(surface, "PARAM X", e.param_x, x, y, width); y += 18
        self._param_bar(surface, "PARAM Y", e.param_y, x, y, width)

    def _draw_badges(self, surface, x, y):
        e = self.engine
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
        if getattr(e, "auto_mode", False):
            badges.append(("AUTOPILOT", (120, 220, 140)))
        if e.blackout:
            badges.append(("BLACKOUT", (255, 80, 80)))
        if e.freeze:
            badges.append(("FREEZE", (130, 200, 255)))
        if e.hit_frames_left > 0:
            badges.append((f"HIT: {e.hit_type}", (255, 200, 80)))
        end_y = y
        if badges:
            bx = x
            for text, color in badges:
                chip = self.font_m.render(f"  {text}  ", True, (20, 20, 30))
                rect = chip.get_rect(topleft=(bx, y))
                pygame.draw.rect(surface, color, rect.inflate(4, 4), border_radius=4)
                surface.blit(chip, rect)
                bx += rect.width + 12
            end_y = y + 26
        if getattr(e, "auto_mode", False):
            info = self.font_s.render(
                f"clip every {e.auto_clip_interval:4.1f}s   ·   "
                f"fx every {e.auto_fx_interval:4.1f}s   ·   "
                f"↑↓ tune clip · ←→ tune fx",
                True, (150, 220, 170),
            )
            surface.blit(info, (x, end_y))
            end_y += 18
        return end_y

    def _draw_favorites(self, surface, x, y, width, label, keys, favs,
                        active_stem=None):
        """Render one row of 10 favourite-slot chips."""
        title = self.font_s.render(label, True, (160, 160, 180))
        surface.blit(title, (x, y + 2))
        slot_x = x + 108
        gap = 4
        cell_w = max(36, (x + width - slot_x - 9 * gap) // 10)
        cell_h = 18
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
            max_chars = max(2, (cell_w - 14) // 6)
            if len(txt) > max_chars:
                txt = txt[:max_chars - 1] + "…"
            label_s = self.font_s.render(f"{k}·{txt}", True, fg)
            surface.blit(label_s, (rect.x + 3, rect.y + 2))
        return y + cell_h + 2

    def _draw_display_selector(self, surface, x, y, width):
        e = self.engine
        title = self.font_h.render("OUTPUT DISPLAY", True, (220, 220, 240))
        surface.blit(title, (x, y))
        hint = self.font_s.render("(F11 cycle · F12 apply · saved)",
                                  True, (140, 140, 170))
        surface.blit(hint, (x + title.get_width() + 8,
                            y + (title.get_height() - hint.get_height())))
        y += 22

        info = self.font_s.render(
            f"current: display {e.cfg.display}    pending: display {e.pending_display}",
            True, (170, 170, 195),
        )
        surface.blit(info, (x, y))
        y += 18

        self._display_btn_rects = []
        bx = x
        btn_h = 22
        for idx in range(e.num_displays):
            label = self.font_m.render(f" Display {idx} ", True, (240, 240, 255))
            rect = pygame.Rect(bx, y, label.get_width() + 12, btn_h)
            is_current = idx == e.cfg.display
            is_pending = idx == e.pending_display
            if is_pending:
                bg = (70, 130, 200); border = (140, 200, 255)
            else:
                bg = (40, 44, 60); border = (90, 90, 120)
            pygame.draw.rect(surface, bg, rect, border_radius=4)
            pygame.draw.rect(surface, border, rect, 1, border_radius=4)
            surface.blit(label, (rect.x + 6, rect.y + (btn_h - label.get_height()) // 2))
            if is_current:
                tick = self.font_s.render("✓", True, (180, 255, 180))
                surface.blit(tick, (rect.right - 12, rect.y + 2))
            self._display_btn_rects.append((idx, rect))
            bx = rect.right + 8

        # Apply button — right-aligned
        apply_label = self.font_m.render(" APPLY ", True, (20, 22, 30))
        apply_rect = pygame.Rect(0, y, apply_label.get_width() + 14, btn_h)
        apply_rect.right = x + width
        enabled = e.pending_display != e.cfg.display
        bg = (120, 220, 140) if enabled else (60, 70, 60)
        pygame.draw.rect(surface, bg, apply_rect, border_radius=4)
        pygame.draw.rect(surface, (40, 80, 40), apply_rect, 1, border_radius=4)
        surface.blit(apply_label,
                     (apply_rect.x + 7,
                      apply_rect.y + (btn_h - apply_label.get_height()) // 2))
        self._apply_rect = apply_rect

        return y + btn_h + 8

    # ── Tabbed bottom panel ──────────────────────────────────────────

    def _draw_tab_panel(self, surface, x, y, w, h):
        """Tab bar + the selected tab's content (replaces the cheat sheet).
        Repopulates _tab_rects / _help_btn_rect / _list_row_rects each frame."""
        pygame.draw.line(surface, (60, 60, 80), (x, y), (x + w, y), 1)
        ty = y + 6
        chip_h = 22
        tabs = [("clips", "CLIPS"), ("gens", "GENS"), ("scenes", "SCENES"),
                ("settings", "SETTINGS"), ("stats", "STATS")]

        self._tab_rects = []
        bx = x
        for tab_id, label in tabs:
            is_sel = self.active_tab == tab_id
            ls = self.font_m.render(
                f" {label} ", True,
                (255, 255, 255) if is_sel else (200, 210, 230))
            rect = pygame.Rect(bx, ty, ls.get_width() + 6, chip_h)
            bg = (70, 130, 200) if is_sel else (38, 42, 58)
            border = (160, 220, 255) if is_sel else (90, 110, 140)
            pygame.draw.rect(surface, bg, rect, border_radius=4)
            pygame.draw.rect(surface, border, rect, 1, border_radius=4)
            surface.blit(ls, (rect.x + 3,
                              rect.y + (chip_h - ls.get_height()) // 2))
            self._tab_rects.append((tab_id, rect))
            bx = rect.right + 5

        # Help (?) button, right-aligned in the tab bar.
        help_label = self.font_m.render(" ? ", True, (20, 22, 30))
        hrect = pygame.Rect(0, ty, help_label.get_width() + 12, chip_h)
        hrect.right = x + w
        pygame.draw.rect(surface,
                         (200, 200, 120) if self._help_open else (90, 150, 200),
                         hrect, border_radius=4)
        surface.blit(help_label, (hrect.x + 6,
                                  hrect.y + (chip_h - help_label.get_height()) // 2))
        self._help_btn_rect = hrect

        # Content area below the tab bar.
        cy = ty + chip_h + 8
        area = pygame.Rect(x, cy, w, max(0, y + h - cy))
        self._list_row_rects = []
        if self.active_tab == "clips":
            self._draw_tab_clips(surface, area)
        elif self.active_tab == "gens":
            self._draw_tab_gens(surface, area)
        elif self.active_tab == "scenes":
            self._draw_tab_scenes(surface, area)
        elif self.active_tab == "settings":
            self._draw_tab_settings(surface, area)
        elif self.active_tab == "stats":
            self._draw_tab_stats(surface, area)

    def _draw_list(self, surface, area, items, active_idx, scroll_key,
                   empty_msg="(empty)"):
        """Scrollable, clickable vertical list. Populates _list_row_rects
        with (item_index, rect) for the rows currently on screen."""
        row_h = 19
        n = len(items)
        if n == 0:
            msg = self.font_m.render(empty_msg, True, (150, 150, 175))
            surface.blit(msg, (area.x + 2, area.y + 2))
            return
        visible = max(1, area.h // row_h)
        max_scroll = max(0, n - visible)
        scroll = max(0, min(self._scroll.get(scroll_key, 0), max_scroll))
        self._scroll[scroll_key] = scroll
        has_bar = n > visible
        list_w = area.w - (10 if has_bar else 0)
        for row in range(visible):
            idx = scroll + row
            if idx >= n:
                break
            rect = pygame.Rect(area.x, area.y + row * row_h, list_w, row_h - 2)
            is_active = (idx == active_idx)
            if is_active:
                pygame.draw.rect(surface, (70, 130, 200), rect, border_radius=3)
                pygame.draw.rect(surface, (160, 220, 255), rect, 1, border_radius=3)
                fg = (255, 255, 255)
            else:
                pygame.draw.rect(surface, (30, 33, 44), rect, border_radius=3)
                fg = (210, 220, 240)
            txt = f"{idx + 1:>3}  {items[idx]}"
            max_chars = max(4, (rect.w - 14) // 7)
            if len(txt) > max_chars:
                txt = txt[:max_chars - 1] + "…"
            s = self.font_m.render(txt, True, fg)
            surface.blit(s, (rect.x + 6,
                             rect.y + (rect.height - s.get_height()) // 2))
            self._list_row_rects.append((idx, rect))
        if has_bar:
            track = pygame.Rect(area.right - 6, area.y, 4, visible * row_h)
            pygame.draw.rect(surface, (40, 44, 60), track, border_radius=2)
            thumb_h = max(12, int(track.h * visible / n))
            thumb_y = track.y + int(track.h * scroll / n)
            pygame.draw.rect(surface, (110, 140, 180),
                             pygame.Rect(track.x, thumb_y, track.w, thumb_h),
                             border_radius=2)

    def _draw_text_block(self, surface, area, lines):
        yy = area.y
        for ln in lines:
            s = self.font_m.render(ln, True, (200, 210, 230))
            surface.blit(s, (area.x + 2, yy))
            yy += s.get_height() + 4

    def _draw_tab_clips(self, surface, area):
        e = self.engine
        items = [e.clips.name(i) or "—" for i in range(len(e.clips))]
        active = self._active_index_for("clips")
        self._draw_list(surface, area, items,
                        active if active is not None else -1,
                        "clips", empty_msg="No clips in assets/clips/")

    def _draw_tab_gens(self, surface, area):
        items = list(GPU_GENERATOR_ORDER)
        active = self._active_index_for("gens")
        self._draw_list(surface, area, items,
                        active if active is not None else -1,
                        "gens", empty_msg="No generators")

    def _draw_tab_scenes(self, surface, area):
        self._draw_text_block(surface, area, [
            "Scenes — save & recall a projection-mapping setup,",
            "so a known-good mapping can be kept for comparison.",
            "Coming in the next update.",
        ])

    def _draw_tab_settings(self, surface, area):
        self._draw_text_block(surface, area, [
            "Settings — global brightness, display filter, render",
            "scales, HUD rotation / fullscreen.",
            "Coming in upcoming updates.",
        ])

    def _draw_tab_stats(self, surface, area):
        e = self.engine
        p = getattr(e, "_perf_ms", None) or {}
        disp = getattr(e, "_disp_ms", 0.0)
        lines = [
            "FPS: %.0f   (cap %d)" % (getattr(e, "fps_measured", 0.0), e.cfg.fps),
            "canvas: %dx%d   gen x%.2f  fx x%.2f" % (
                e.cfg.width, e.cfg.height,
                e.cfg.gen_render_scale, e.cfg.fx_render_scale),
        ]
        if p:
            lines.append(
                "pipeline: clip %.0f  gen %.0f  fx %.0f  warp %.0f  disp %.0f ms"
                % (p.get("clip", 0), p.get("gen", 0), p.get("fx", 0),
                   p.get("warp", 0), disp))
        lines.append("library: %d clips · %d generators" % (
            len(e.clips), len(GPU_GENERATOR_ORDER)))
        lines.append("CPU / GPU / per-thread stats: coming in a later update.")
        self._draw_text_block(surface, area, lines)

    def _draw_help_overlay(self, surface):
        """Modal cheat-sheet popup over the whole HUD. Click to dismiss."""
        win_w, win_h = self.size
        overlay = pygame.Surface((win_w, win_h), pygame.SRCALPHA)
        overlay.fill((8, 10, 16, 236))
        surface.blit(overlay, (0, 0))
        pad = 16
        title = self.font_h.render(
            "KEYBOARD / MOUSE  —  click anywhere to close",
            True, (230, 235, 255))
        surface.blit(title, (pad, pad))
        panel = (self._mapping_cheat_panel if self.engine.mode == "mapping"
                 else self._cheat_panel)
        top = pad + title.get_height() + 8
        avail = max(0, win_h - top - pad)
        src = panel.subsurface(
            pygame.Rect(0, 0, panel.get_width(),
                        min(panel.get_height(), avail)))
        surface.blit(src, (pad, top))

    def _build_cheat_panel(self, rows):
        """Pre-render a static key cheat sheet to a Surface."""
        w = self.size[0] - 24
        row_h = 16
        h = len(rows) * row_h + 28
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.line(panel, (60, 60, 80), (0, 0), (w, 0), 1)
        title = self.font_h.render("KEYS", True, (220, 220, 240))
        panel.blit(title, (0, 6))
        y = 28
        for keys, desc in rows:
            ks = self.font_s.render(keys, True, (140, 200, 255))
            ds = self.font_s.render(desc, True, (180, 180, 200))
            panel.blit(ks, (0, y))
            panel.blit(ds, (170, y))
            y += row_h
        return panel

    @staticmethod
    def _library_label(pool):
        total = len(pool)
        if total == 0:
            return "— (empty)"
        idx = pool.active_idx
        if idx is None:
            return f"—  [0/{total}]"
        return f"{pool.name(idx)}  [{idx + 1}/{total}]"

    def _row(self, surface, label, value, x, y, width=None):
        if width is None:
            width = surface.get_width() - x - 12
        ls = self.font_m.render(label, True, (130, 130, 160))
        max_chars = max(8, int((width - 90) / 7))
        if len(str(value)) > max_chars:
            value = str(value)[: max_chars - 1] + "…"
        vs = self.font_m.render(str(value), True, (240, 240, 255))
        surface.blit(ls, (x, y))
        surface.blit(vs, (x + 78, y))

    def _param_bar(self, surface, label, value, x, y, width=None):
        if width is None:
            width = 290
        ls = self.font_m.render(label, True, (130, 130, 160))
        surface.blit(ls, (x, y))
        bar_x = x + 78
        bar_w = max(60, width - 78 - 42)
        bar_h = 10
        bar_y = y + 4
        pygame.draw.rect(surface, (40, 44, 60),
                         (bar_x, bar_y, bar_w, bar_h), border_radius=2)
        fill_w = int(bar_w * max(0.0, min(1.0, value)))
        pygame.draw.rect(surface, (140, 200, 255),
                         (bar_x, bar_y, fill_w, bar_h), border_radius=2)
        pygame.draw.rect(surface, (90, 90, 120),
                         (bar_x, bar_y, bar_w, bar_h), 1, border_radius=2)
        vs = self.font_s.render(f"{value:.2f}", True, (200, 200, 220))
        surface.blit(vs, (bar_x + bar_w + 6, y + 1))
