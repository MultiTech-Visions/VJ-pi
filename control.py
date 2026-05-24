"""Control HUD window — runs on the second display (small screen).
Shows a live preview of the projected output, current state, and a
key cheat sheet so the operator never has to remember bindings.
"""
import pygame


KEY_CHEAT = [
    ("1 - 0",     "Base clips (slot 1 to 10)"),
    ("Q - P",     "Overlays (toggle)"),
    ("A S D",     "Generative: plasma / tunnel / stars"),
    ("Z X C V B", "Hits: strobe / black / inv / zoom / RGB"),
    ("F1 - F7",   "Persistent FX toggles"),
    ("Space",     "Blackout (panic)"),
    ("Backspace", "Freeze frame"),
    ("Esc",       "Kill all FX"),
    ("Shift+Esc", "Quit"),
    ("Mouse XY",  "Tunes the active FX parameter"),
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
        self.preview_w, self.preview_h = preview_size
        self._Texture = Texture
        self.font_h = pygame.font.SysFont("Sans,Arial,DejaVuSans", 20, bold=True)
        self.font_m = pygame.font.SysFont("Sans,Arial,DejaVuSans", 15)
        self.font_s = pygame.font.SysFont("Sans,Arial,DejaVuSans", 12)
        self._cheat_panel = self._build_cheat_panel()

    def render(self, frame):
        surface = self.surface
        surface.fill((18, 20, 28))
        win_w, win_h = self.size

        # ── 1. Live preview ──────────────────────────────────────────
        pad = 12
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

        y = pad + self.preview_h + 18
        x = pad

        title = self.font_h.render("NOW PLAYING", True, (220, 220, 240))
        surface.blit(title, (x, y))
        y += 30

        self._row(surface, "CLIP",    clip_name, x, y);  y += 24
        self._row(surface, "GEN",     gen_name, x, y);   y += 24
        self._row(surface, "OVERLAY", ov_name, x, y);    y += 28
        self._row(surface, "FX",      fx_text, x, y);    y += 30

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

        # ── 3. Key cheat sheet at bottom (pre-rendered) ──────────────
        panel = self._cheat_panel
        surface.blit(panel, (pad, win_h - panel.get_height() - pad))

        # Upload the composed surface to a texture and present it
        tex = self._Texture.from_surface(self.renderer, surface)
        self.renderer.clear()
        tex.draw()
        self.renderer.present()

    def _build_cheat_panel(self):
        """Pre-render the static key cheat sheet to a Surface."""
        w = self.size[0] - 24
        h = len(KEY_CHEAT) * 18 + 36
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.line(panel, (60, 60, 80), (0, 0), (w, 0), 1)
        title = self.font_h.render("KEYS", True, (220, 220, 240))
        panel.blit(title, (0, 8))
        y = 36
        for keys, desc in KEY_CHEAT:
            ks = self.font_s.render(keys, True, (140, 200, 255))
            ds = self.font_s.render(desc, True, (180, 180, 200))
            panel.blit(ks, (0, y))
            panel.blit(ds, (110, y))
            y += 18
        return panel

    def _row(self, surface, label, value, x, y):
        ls = self.font_m.render(label, True, (130, 130, 160))
        # Truncate long values to fit the panel
        max_chars = max(10, int((surface.get_width() - x - 110) / 8))
        if len(str(value)) > max_chars:
            value = str(value)[: max_chars - 1] + "…"
        vs = self.font_m.render(str(value), True, (240, 240, 255))
        surface.blit(ls, (x, y))
        surface.blit(vs, (x + 95, y))
