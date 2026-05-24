"""Control HUD window — runs on the second display (small screen).
Live preview, status panel, favourite-slot grids, output-display picker,
and a key cheat sheet — laid out top-to-bottom with the preview and
status sharing the top row to claim back horizontal space.
"""
import pygame


KEY_CHEAT = [
    ("− / =",        "Prev / next CLIP (hold to scrub)"),
    ("[ / ]",        "Prev / next OVERLAY (hold to scrub)"),
    ("1 - 0",        "Clip favourites — tap=play, hold ≥½s=assign current"),
    ("Q - P",        "Overlay favourites — tap=play, hold ≥½s=assign current"),
    ("A S D F G H",  "Gen: plasma / tunnel / stars / warp / waves / cells"),
    ("J K L",        "Gen: lissajous / moiré / metaballs"),
    ("Z X C V B",    "Hits: strobe / black / inv / zoom / RGB"),
    ("F1 - F7",      "Persistent FX toggles"),
    ("← → ↑ ↓",      "Tune PARAM X / Y (active-FX controls)"),
    ("F11 / F12",    "Cycle output display / APPLY"),
    ("Space",        "Blackout (panic)"),
    ("Backspace",    "Freeze frame"),
    ("Esc",          "Panic: clear FX / overlay / hits (keeps the clip playing)"),
    ("Shift+Esc",    "Quit"),
]


class ControlWindow:
    """Renders the control HUD into a second pygame window.

    Pygame 2's SDL2 Window doesn't expose a get_surface(), so we render
    each frame onto an off-screen Surface, then upload that as a Texture
    via the Window's Renderer and present.
    """

    PREVIEW_TARGET_H = 180  # tall enough to read, narrow enough to share the row

    def __init__(self, engine, window, renderer, size, preview_size):
        from pygame._sdl2.video import Texture
        self.engine = engine
        self.window = window
        self.renderer = renderer
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
        try:
            self._window_id = window.id
        except AttributeError:
            self._window_id = None
        self._cheat_panel = self._build_cheat_panel()

    # ── Event handling ───────────────────────────────────────────────

    def handle_event(self, event):
        """Forward mouse clicks from the control window to its buttons."""
        if event.type != pygame.MOUSEBUTTONDOWN or event.button != 1:
            return
        if not self._event_is_ours(event):
            return
        pos = getattr(event, "pos", None)
        if pos is None:
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
        surface.fill((18, 20, 28))
        win_w, win_h = self.size
        pad = 12
        e = self.engine

        # ── Top row: preview (left) + status (right) ─────────────
        self._draw_preview(surface, pad, pad, frame)
        sx = pad + self.preview_w + 16
        self._draw_status(surface, sx, pad, win_w - sx - pad)

        # Cursor flowing down the page from below the preview row.
        y = pad + self.preview_h + 14

        # Badges (only renders if anything to show; returns same y otherwise)
        y = self._draw_badges(surface, pad, y)

        # Favourites grids
        y = self._draw_favorites(surface, pad, y, win_w - pad * 2,
                                 "CLIP FAVS  (1-0)", "1234567890",
                                 e.clip_favorites,
                                 active_stem=e.clips.name(e.clips.active_idx))
        y += 2
        y = self._draw_favorites(surface, pad, y, win_w - pad * 2,
                                 "OVL  FAVS  (Q-P)", "QWERTYUIOP",
                                 e.overlay_favorites,
                                 active_stem=e.overlays.name(e.overlays.active_idx))
        y += 12

        # Display selector
        y = self._draw_display_selector(surface, pad, y, win_w - pad * 2)
        y += 10

        # Key cheat sheet — flows naturally after the display selector.
        # If room runs out we just clip at the window bottom (still useful
        # since the top rows are the more important ones).
        panel = self._cheat_panel
        avail = max(0, win_h - pad - y)
        if avail > 0:
            src = panel.subsurface(
                pygame.Rect(0, 0, panel.get_width(), min(panel.get_height(), avail))
            )
            surface.blit(src, (pad, y))

        # Upload the composed surface and present it.
        tex = self._Texture.from_surface(self.renderer, surface)
        self.renderer.clear()
        tex.draw()
        self.renderer.present()

    # ── Panel parts ──────────────────────────────────────────────────

    def _draw_preview(self, surface, x, y, frame):
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
        label = self.font_s.render("LIVE OUTPUT", True, (140, 140, 160))
        surface.blit(label, (x + 6, y + 4))

    def _draw_status(self, surface, x, y, width):
        e = self.engine
        clip_text = self._library_label(e.clips)
        ov_text = self._library_label(e.overlays)
        gen_name = e.active_generative or "—"
        fx_on = [k for k, v in e.fx_state.items() if v]
        fx_text = ", ".join(fx_on) if fx_on else "—"

        title = self.font_h.render("NOW PLAYING", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += 22

        self._row(surface, "CLIP",    clip_text, x, y, width); y += 19
        self._row(surface, "GEN",     gen_name,  x, y, width); y += 19
        self._row(surface, "OVERLAY", ov_text,   x, y, width); y += 19
        self._row(surface, "FX",      fx_text,   x, y, width); y += 22

        self._param_bar(surface, "PARAM X", e.param_x, x, y, width); y += 18
        self._param_bar(surface, "PARAM Y", e.param_y, x, y, width)

    def _draw_badges(self, surface, x, y):
        e = self.engine
        badges = []
        if e.blackout:
            badges.append(("BLACKOUT", (255, 80, 80)))
        if e.freeze:
            badges.append(("FREEZE", (130, 200, 255)))
        if e.hit_frames_left > 0:
            badges.append((f"HIT: {e.hit_type}", (255, 200, 80)))
        if not badges:
            return y
        bx = x
        for text, color in badges:
            chip = self.font_m.render(f"  {text}  ", True, (20, 20, 30))
            rect = chip.get_rect(topleft=(bx, y))
            pygame.draw.rect(surface, color, rect.inflate(4, 4), border_radius=4)
            surface.blit(chip, rect)
            bx += rect.width + 12
        return y + 26

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

        if getattr(e, "restart_pending", False):
            warn = self.font_s.render(
                "⚠ Shift+Esc and re-launch to apply on the fullscreen output",
                True, (255, 200, 90),
            )
            surface.blit(warn, (x, y))
            y += 16

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

    def _build_cheat_panel(self):
        """Pre-render the static key cheat sheet to a Surface."""
        w = self.size[0] - 24
        row_h = 16
        h = len(KEY_CHEAT) * row_h + 28
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.line(panel, (60, 60, 80), (0, 0), (w, 0), 1)
        title = self.font_h.render("KEYS", True, (220, 220, 240))
        panel.blit(title, (0, 6))
        y = 28
        for keys, desc in KEY_CHEAT:
            ks = self.font_s.render(keys, True, (140, 200, 255))
            ds = self.font_s.render(desc, True, (180, 180, 200))
            panel.blit(ks, (0, y))
            panel.blit(ds, (130, y))
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
