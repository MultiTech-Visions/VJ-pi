"""Control HUD window — runs on the second display (small screen).
Shows a live preview of the projected output, current state, a key cheat
sheet, and a display picker so the operator can move the fullscreen
output to a different monitor without restarting.
"""
import pygame


KEY_CHEAT = [
    ("1 - 0",        "Base clips (slot 1 to 10)"),
    ("Q - P",        "Overlays (toggle)"),
    ("A S D F G H",  "Gen: plasma/tunnel/stars/warp/waves/cells"),
    ("J K L",        "Gen: lissajous / moiré / metaballs"),
    ("Z X C V B",    "Hits: strobe / black / inv / zoom / RGB"),
    ("F1 - F7",      "Persistent FX toggles"),
    ("← →",          "Tune PARAM X (active-FX horizontal control)"),
    ("↑ ↓",          "Tune PARAM Y (active-FX vertical control)"),
    ("Space",        "Blackout (panic)"),
    ("Backspace",    "Freeze frame"),
    ("Esc",          "Kill all FX"),
    ("Shift+Esc",    "Quit"),
]


class ControlWindow:
    """Renders the control HUD into a second pygame window.

    Pygame 2's SDL2 Window doesn't expose a get_surface(), so we render
    each frame onto an off-screen Surface, then upload that as a Texture
    via the Window's Renderer and present.
    """

    def __init__(self, engine, window, renderer, size, preview_size):
        from pygame._sdl2.video import Texture
        self.engine = engine
        self.window = window
        self.renderer = renderer
        self.size = size  # (w, h)
        self.surface = pygame.Surface(size)
        # Cap preview height so we keep room for the rest of the HUD.
        max_preview_h = 220
        pw, ph = preview_size
        if ph > max_preview_h:
            pw = int(pw * max_preview_h / ph)
            ph = max_preview_h
        self.preview_w, self.preview_h = pw, ph
        self._Texture = Texture
        self.font_h = pygame.font.SysFont("Sans,Arial,DejaVuSans", 18, bold=True)
        self.font_m = pygame.font.SysFont("Sans,Arial,DejaVuSans", 14)
        self.font_s = pygame.font.SysFont("Sans,Arial,DejaVuSans", 12)
        try:
            self.num_displays = max(1, pygame.display.get_num_displays())
        except pygame.error:
            self.num_displays = 1
        # Pending selection — committed when the operator clicks APPLY.
        self.pending_display = engine.cfg.display

        # Hit-test rects, populated each frame in render().
        self._display_btn_rects = []  # [(idx, pygame.Rect), ...]
        self._apply_rect = None
        # Track which window IDs to accept mouse events from. The pygame
        # _sdl2 Window exposes .id; events from that window carry the same
        # id in event.window.id (or, in older pygame, the Window object).
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
                self.pending_display = idx
                return
        if self._apply_rect is not None and self._apply_rect.collidepoint(pos):
            self.engine.switch_output_display(self.pending_display)

    def _event_is_ours(self, event):
        """True if the mouse event came from the control window.

        Pygame multi-window event.window can be the Window object itself or
        carry an .id — handle both. If we couldn't identify our window at
        startup, accept everything so clicks still work."""
        win = getattr(event, "window", None)
        if self._window_id is None:
            return True
        if win is None:
            # Main display window — not ours.
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

        # ── 1. Live preview ──────────────────────────────────────────
        if frame is not None:
            preview = pygame.image.frombuffer(
                frame.tobytes(), (frame.shape[1], frame.shape[0]), "RGB"
            )
            preview = pygame.transform.scale(preview, (self.preview_w, self.preview_h))
            surface.blit(preview, (pad, pad))
        else:
            pygame.draw.rect(surface, (40, 40, 50),
                             (pad, pad, self.preview_w, self.preview_h))
        pygame.draw.rect(surface, (90, 90, 120),
                         (pad, pad, self.preview_w, self.preview_h), 1)
        label = self.font_s.render("LIVE OUTPUT", True, (140, 140, 160))
        surface.blit(label, (pad + 6, pad + 4))

        # ── 2. Status panel ──────────────────────────────────────────
        e = self.engine
        clip_name = e.clips.name(e.clips.active_idx) if e.clips.active_idx is not None else "—"
        gen_name = e.active_generative or "—"
        ov_name = e.overlays.name(e.overlays.active_idx) if e.overlays.active_idx is not None else "—"
        fx_on = [k for k, v in e.fx_state.items() if v]
        fx_text = ", ".join(fx_on) if fx_on else "—"

        y = pad + self.preview_h + 14
        x = pad

        title = self.font_h.render("NOW PLAYING", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += 24

        self._row(surface, "CLIP",    clip_name, x, y);  y += 20
        self._row(surface, "GEN",     gen_name,  x, y);  y += 20
        self._row(surface, "OVERLAY", ov_name,   x, y);  y += 20
        self._row(surface, "FX",      fx_text,   x, y);  y += 22

        # Param bars
        self._param_bar(surface, "PARAM X", e.param_x, x, y); y += 18
        self._param_bar(surface, "PARAM Y", e.param_y, x, y); y += 22

        # State badges
        badges = []
        if e.blackout:
            badges.append(("BLACKOUT", (255, 80, 80)))
        if e.freeze:
            badges.append(("FREEZE", (130, 200, 255)))
        if e.hit_frames_left > 0:
            badges.append((f"HIT: {e.hit_type}", (255, 200, 80)))
        bx = x
        for text, color in badges:
            chip = self.font_m.render(f"  {text}  ", True, (20, 20, 30))
            rect = chip.get_rect(topleft=(bx, y))
            pygame.draw.rect(surface, color, rect.inflate(4, 4), border_radius=4)
            surface.blit(chip, rect)
            bx += rect.width + 12
        y += 22

        # ── 3. Display selector ──────────────────────────────────────
        y = self._draw_display_selector(surface, x, y, win_w - pad * 2)

        # ── 4. Key cheat sheet at bottom (pre-rendered) ──────────────
        panel = self._cheat_panel
        surface.blit(panel, (pad, win_h - panel.get_height() - pad))

        # Upload the composed surface to a texture and present it
        tex = self._Texture.from_surface(self.renderer, surface)
        self.renderer.clear()
        tex.draw()
        self.renderer.present()

    def _draw_display_selector(self, surface, x, y, width):
        e = self.engine
        title = self.font_h.render("OUTPUT DISPLAY", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += 22

        info = self.font_s.render(
            f"current: display {e.cfg.display}    pending: display {self.pending_display}",
            True, (170, 170, 195),
        )
        surface.blit(info, (x, y))
        y += 18

        self._display_btn_rects = []
        bx = x
        btn_h = 22
        for idx in range(self.num_displays):
            label = self.font_m.render(f" Display {idx} ", True, (240, 240, 255))
            rect = pygame.Rect(bx, y, label.get_width() + 12, btn_h)
            is_current = idx == e.cfg.display
            is_pending = idx == self.pending_display
            if is_pending:
                bg = (70, 130, 200)
                border = (140, 200, 255)
            else:
                bg = (40, 44, 60)
                border = (90, 90, 120)
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
        enabled = self.pending_display != e.cfg.display
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
            panel.blit(ds, (140, y))
            y += row_h
        return panel

    def _row(self, surface, label, value, x, y):
        ls = self.font_m.render(label, True, (130, 130, 160))
        max_chars = max(10, int((surface.get_width() - x - 110) / 7))
        if len(str(value)) > max_chars:
            value = str(value)[: max_chars - 1] + "…"
        vs = self.font_m.render(str(value), True, (240, 240, 255))
        surface.blit(ls, (x, y))
        surface.blit(vs, (x + 85, y))

    def _param_bar(self, surface, label, value, x, y):
        ls = self.font_m.render(label, True, (130, 130, 160))
        surface.blit(ls, (x, y))
        bar_x = x + 85
        bar_w = 200
        bar_h = 10
        bar_y = y + 4
        pygame.draw.rect(surface, (40, 44, 60), (bar_x, bar_y, bar_w, bar_h),
                         border_radius=2)
        fill_w = int(bar_w * max(0.0, min(1.0, value)))
        pygame.draw.rect(surface, (140, 200, 255), (bar_x, bar_y, fill_w, bar_h),
                         border_radius=2)
        pygame.draw.rect(surface, (90, 90, 120), (bar_x, bar_y, bar_w, bar_h),
                         1, border_radius=2)
        vs = self.font_s.render(f"{value:.2f}", True, (200, 200, 220))
        surface.blit(vs, (bar_x + bar_w + 8, y + 1))
