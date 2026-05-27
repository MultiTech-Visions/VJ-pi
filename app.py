"""GTK3 application + GStreamer pipeline.

Architecture (phase 4a — swappable source, stable downstream):

  source_bin (rebuilt per source change):
    CLIP:       filesrc ─ decodebin ─ videoconvert ─ videoscale ─ glupload ─[ghost src]
    GENERATOR:  videotestsrc-black ─ glupload ─ glshader[GLSL]   ─────────────[ghost src]
                                                  GL texture, 1280×720
                                                       │
                                                       ▼
  downstream_bin (lives for the whole session):
                tee ─┬─▶ queue ─ gldownload ─ videoconvert ─ gtksink (output)
                     └─▶ queue ─ gldownload ─ videoconvert ─ gtksink (HUD)

Both source-bin variants output the same caps (GL texture at canvas
resolution), so we can tear down and rebuild the source side without
disturbing the GL context that the downstream owns. That avoids both
the glshader recompile cost on each source change AND the
gtkglsink-style dual-context bug we already know about on V3D.

This is the layer the rest of phases 4–6 will build on:
  4a — source switching (clips + generators) via keyboard  ← here
  4b — overlay layer via compositor
  4c — favourites grid (tap/hold on 1-0 and Q-P)
  5  — mapping mode (groups, spaces, fit modes)
  6  — FX chain, hits, autopilot, polish

GTK3 + gtksink because Pi OS Bookworm's apt doesn't carry
gst-plugins-rs (where gtk4paintablesink lives). Single GL context
through gstreamer's gl* family. CPU presentation via gldownload
before the gtksinks — see CLAUDE.md for the V3D-dual-context
post-mortem that justifies all of this.
"""
import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gst, Gdk, Gio, GLib  # noqa: E402


HERE = Path(__file__).resolve().parent
CLIPS_DIR = HERE / "assets" / "clips"

# Canvas resolution. Source bins normalise to this before the
# downstream tee, so the GL texture flowing through is always
# 1280×720 regardless of clip resolution.
CANVAS_W = 1280
CANVAS_H = 720

# Downstream bin — stable for the lifetime of the app. tee forks
# the GL texture by refcount; each branch downloads to CPU just
# before its gtksink (gtksink doesn't accept GLMemory). The
# downstream's ghost sink pad accepts GL textures, so source-bin
# replacement is a clean unlink/link without touching GL state.
DOWNSTREAM_DESC = (
    "tee name=t allow-not-linked=true "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  gldownload ! videoconvert ! "
    "  gtksink name=output_sink sync=false "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  gldownload ! videoconvert ! "
    "  gtksink name=hud_sink sync=false"
)


# ── GLSL fragment shaders ─────────────────────────────────────────
#
# All shaders target GLES 2.0 (#version 100 + precision qualifier)
# for V3D compatibility. gstreamer's glshader plugin provides:
#   varying vec2 v_texcoord   — UV in [0, 1]
#   uniform float time        — running time in seconds
#   uniform sampler2D tex     — input texture (we ignore it; the
#                                shader is fully generative)

PLASMA_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec2 p = v_texcoord * 8.0;
    float t = time;
    float v = (sin(p.x + t) + sin(p.y + t * 1.3)
             + sin((p.x + p.y) * 0.5 + t * 0.7)
             + sin(sqrt(p.x*p.x + p.y*p.y) + t * 1.7)) * 0.25;
    v = (v + 1.0) * 0.5;
    float hue = fract(v + t / 9.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 1.0, 1.0)), 1.0);
}
"""

TUNNEL_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 pix = (v_texcoord - 0.5) * vec2(1280.0, 720.0);
    float r = length(pix) + 1.0;
    float a = atan(pix.y, pix.x);
    float u = mod(200.0 / r + time * 2.0, 1.0);
    float v_ = (a / PI + 1.0) * 0.5;
    float chk = mod(floor(u * 8.0) + floor(v_ * 16.0), 2.0);
    float hue = fract(v_ + time / 6.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 1.0, chk)), 1.0);
}
"""

# Two-source rippling interference pattern — two moving centres,
# each emanating sinusoidal rings, summed and hue-cycled.
WAVES_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    float t = time;
    vec2 c1 = vec2(1280.0 * 0.3 + sin(t * 0.5) * 1280.0 * 0.15,
                    720.0 * 0.5 + cos(t * 0.4) *  720.0 * 0.2);
    vec2 c2 = vec2(1280.0 * 0.7 + cos(t * 0.6) * 1280.0 * 0.15,
                    720.0 * 0.5 + sin(t * 0.45) * 720.0 * 0.2);
    float period = 52.0;
    float r1 = distance(pix, c1) / period;
    float r2 = distance(pix, c2) / period;
    float v = (sin(r1 * PI * 2.0 - t * 2.0)
             + sin(r2 * PI * 2.0 + t * 1.5)) * 0.25 + 0.5;
    float hue = fract(v + t * 0.1);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.86, v)), 1.0);
}
"""

# Animated quasi-voronoi cellular pattern. The sin*sin product
# creates a grid of "cells" that wobble as the input coordinates
# get phase-shifted by their neighbours over time.
CELLS_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    float t = time;
    float scale = 0.038;
    float u = pix.x * scale + sin(pix.y * scale * 0.6 + t)      * 0.4;
    float vv = pix.y * scale + cos(pix.x * scale * 0.6 + t * 1.1) * 0.4;
    float pat = abs(sin(u * PI) * sin(vv * PI));
    pat = pow(pat, 0.6);
    float hue = fract((u * 28.0 + t * 14.0) / 180.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.82, pat)), 1.0);
}
"""

# Concentric-ring moiré from two slowly orbiting sources. The
# interference between two ring patterns gives the optical-illusion
# look that's hard to look away from at the projector.
MOIRE_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    vec2 ctr = vec2(640.0, 360.0);
    float t = time;
    float ox = 1280.0 * 0.10;
    float oy =  720.0 * 0.10;
    vec2 c1 = ctr + vec2(sin(t * 0.5) * ox, cos(t * 0.4) * oy);
    vec2 c2 = ctr - vec2(sin(t * 0.5) * ox, cos(t * 0.4) * oy);
    float spacing = 14.0;
    float r1 = distance(pix, c1) / spacing;
    float r2 = distance(pix, c2) / spacing;
    float pat = (sin(r1 + t * 2.0) + sin(r2 - t * 1.5)) * 0.25 + 0.5;
    float hue = fract(pat + t * 22.0 / 180.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.82, pat)), 1.0);
}
"""

# Classic sum-of-fields metaballs — six orbiting points each
# contribute an inverse-square "blob" field; threshold (via
# brightness) gives the merging-blob look that's been in every
# VJ tool since the demoscene days.
METABALLS_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    float t = time;
    float influence = 1280.0 * 26.0 * 1.4;
    float field = 0.0;
    for (int i = 0; i < 6; i++) {
        float phase = float(i) * 2.0 * PI / 6.0;
        float bx = 640.0 + cos(t * 0.5 + phase * 1.3) * 1280.0 * 0.35;
        float by = 360.0 + sin(t * 0.7 + phase * 1.7) *  720.0 * 0.35;
        float dx = pix.x - bx;
        float dy = pix.y - by;
        float r2 = dx * dx + dy * dy + 1.0;
        field += influence / r2;
    }
    float intensity = clamp(field / 2.5, 0.0, 1.0);
    float hue = fract((intensity * 80.0 + t * 20.0) / 180.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.9, intensity)), 1.0);
}
"""

GENERATORS = {
    "plasma": PLASMA_SHADER,
    "tunnel": TUNNEL_SHADER,
    "waves": WAVES_SHADER,
    "cells": CELLS_SHADER,
    "moire": MOIRE_SHADER,
    "metaballs": METABALLS_SHADER,
}


def _list_clips():
    """Sorted list of .mp4 / .mov / .MP4 / .MOV files in assets/clips/.

    Empty list if the folder doesn't exist or is empty. Phase 4
    cycles through this list with -/= keys.
    """
    if not CLIPS_DIR.exists():
        return []
    return (sorted(CLIPS_DIR.glob("*.mp4"))
            + sorted(CLIPS_DIR.glob("*.mov"))
            + sorted(CLIPS_DIR.glob("*.MP4"))
            + sorted(CLIPS_DIR.glob("*.MOV")))


class VJApp(Gtk.Application):
    """GTK3 + GStreamer application owning the pipeline and the two
    top-level windows.

    Pipeline structure: one stable downstream bin (tee + 2 sinks) +
    one swappable source bin (clip OR generator). Source switches
    happen via keyboard; the downstream stays alive.
    """

    def __init__(self, source_kind="clip", single_screen=False):
        super().__init__(
            application_id="com.multitech.vjpi",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        # Initial source from CLI flag — phase 4 keys flip it at
        # runtime; the flag is just the starting state.
        self.initial_source_kind = source_kind
        self.single_screen = single_screen

        self.pipeline = None
        self.output_window = None
        self.hud_window = None
        self._downstream_bin = None
        self._source_bin = None
        self._status_label = None

        # Clip pool state — list of paths + index of the last clip
        # that was active. We remember the index even when the
        # current source is a generator so `-/=` can return to the
        # previous-or-next clip naturally.
        self._clips = _list_clips()
        self._current_clip_idx = 0
        # Tracks what the current source bin is rendering, for
        # status display and key routing. ("clip", path) or
        # ("generator", name).
        self._current_source = None

    # ── GTK Application lifecycle ──────────────────────────────────

    def do_command_line(self, cmdline):
        self.activate()
        return 0

    def do_activate(self):
        Gst.init(None)
        print(f"[vj] initial source: {self.initial_source_kind}")

        # Build the stable downstream bin once. The source bin gets
        # constructed below and added to the same pipeline.
        self.pipeline = Gst.Pipeline.new("vj-pipeline")
        try:
            self._downstream_bin = Gst.parse_bin_from_description(
                DOWNSTREAM_DESC, True
            )
        except GLib.Error as exc:
            self._fail(
                "Could not build the downstream GStreamer chain.\n"
                f"  Error: {exc.message}\n\n"
                "Hint: is `gstreamer1.0-gtk3` installed? Re-run setup.sh."
            )
            return
        self._downstream_bin.set_name("downstream")
        self.pipeline.add(self._downstream_bin)

        # Pick the initial source. If the user asked for clip mode
        # but there are no clips, fall back to plasma so they get
        # something on-screen rather than a silent failure.
        if self.initial_source_kind in GENERATORS:
            self._install_generator(self.initial_source_kind, start_state=Gst.State.NULL)
        elif self._clips:
            self._install_clip(0, start_state=Gst.State.NULL)
        else:
            print("[vj] no clips found; falling back to plasma generator")
            self._install_generator("plasma", start_state=Gst.State.NULL)

        if not self._bind_sinks_to_windows():
            return

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self.output_window.show_all()
        self.hud_window.show_all()
        if self.single_screen:
            self._move_window_to_monitor(self.output_window, 0, fullscreen=False)
            self._move_window_to_monitor(self.hud_window, 0, fullscreen=False)
        else:
            self._move_window_to_monitor(self.output_window, 1, fullscreen=True)
            self._move_window_to_monitor(self.hud_window, 0, fullscreen=False)

        self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[vj] pipeline up: {Gst.version_string()}")

    def do_shutdown(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        Gtk.Application.do_shutdown(self)

    # ── Source-bin builders ────────────────────────────────────────

    def _build_clip_source_bin(self, clip_path):
        """Build a Gst.Bin: filesrc → decodebin → videoconvert →
        videoscale → capsfilter → glupload. The bin has a ghost src
        pad outputting GL textures at CANVAS_W × CANVAS_H.

        decodebin's pad-added is hooked up to link into the post-
        decoder normalisation chain at runtime — same pattern as
        phase 2, just localised to this bin.
        """
        bin_name = f"clip-source-{id(clip_path) & 0xffff:04x}"
        outer = Gst.Bin.new(bin_name)

        filesrc = Gst.ElementFactory.make("filesrc", None)
        filesrc.set_property("location", str(clip_path))

        decoder = Gst.ElementFactory.make("decodebin", None)

        # Post-decoder normalisation: bring whatever decodebin
        # produces up/down to canvas resolution + raw video, then
        # upload to GL. parse_bin_from_description gives us ghost
        # pads on the unconnected ends (videoconvert sink + glupload
        # src), which we use below.
        norm = Gst.parse_bin_from_description(
            f"videoconvert ! videoscale ! "
            f"video/x-raw,width={CANVAS_W},height={CANVAS_H} ! "
            f"glupload",
            True,
        )

        outer.add(filesrc)
        outer.add(decoder)
        outer.add(norm)
        if not filesrc.link(decoder):
            raise RuntimeError("failed to link filesrc → decodebin")

        # decodebin → norm gets linked once decodebin figures out the
        # stream type. Bind the handler to this bin's `norm` so the
        # closure doesn't reach into self.
        norm_sink = norm.get_static_pad("sink")
        def on_pad_added(_decoder, new_pad):
            caps = new_pad.get_current_caps()
            if caps is None:
                return
            if not caps.get_structure(0).get_name().startswith("video/"):
                return  # ignore audio
            if norm_sink.is_linked():
                return
            new_pad.link(norm_sink)
        decoder.connect("pad-added", on_pad_added)

        # Ghost the GL texture out of the bin so the outer pipeline
        # can link directly to downstream.
        norm_src = norm.get_static_pad("src")
        outer.add_pad(Gst.GhostPad.new("src", norm_src))
        return outer

    def _build_generator_source_bin(self, name):
        """Build a Gst.Bin: videotestsrc-black → glupload → glshader.
        Output: GL textures at canvas resolution. parse_bin_from_
        description ghosts the unconnected glshader src pad as the
        bin's "src" pad — perfect for linking to downstream.
        """
        if name not in GENERATORS:
            raise ValueError(f"unknown generator {name!r}")
        outer = Gst.parse_bin_from_description(
            f"videotestsrc is-live=true pattern=black ! "
            f"video/x-raw,width={CANVAS_W},height={CANVAS_H},framerate=30/1 ! "
            f"glupload ! "
            f"glshader name=shader",
            True,
        )
        outer.set_name(f"generator-source-{name}")
        shader = outer.get_by_name("shader")
        shader.set_property("fragment", GENERATORS[name])
        return outer

    # ── Source-bin install / replace ───────────────────────────────

    def _install_clip(self, idx, start_state=Gst.State.PLAYING):
        """Make assets/clips/<sorted>[idx] the active source.

        Used by both first-launch (start_state=NULL — caller will
        promote the pipeline to PLAYING after) and runtime swaps
        (start_state=PLAYING — we restart the source side).
        """
        if not self._clips:
            print("[vj] no clips loaded — ignoring clip switch")
            return
        idx = idx % len(self._clips)
        clip = self._clips[idx]
        try:
            new_bin = self._build_clip_source_bin(clip)
        except Exception as exc:
            print(f"[vj] failed to build clip source ({clip.name}): {exc!r}")
            return
        self._current_clip_idx = idx
        self._current_source = ("clip", clip)
        self._swap_source_bin(new_bin, start_state)
        print(f"[vj] → clip {idx + 1}/{len(self._clips)}: {clip.name}")
        self._refresh_status()

    def _install_generator(self, name, start_state=Gst.State.PLAYING):
        """Make a glshader-driven generator the active source."""
        try:
            new_bin = self._build_generator_source_bin(name)
        except Exception as exc:
            print(f"[vj] failed to build generator source ({name}): {exc!r}")
            return
        self._current_source = ("generator", name)
        self._swap_source_bin(new_bin, start_state)
        print(f"[vj] → generator: {name}")
        self._refresh_status()

    def _swap_source_bin(self, new_bin, start_state):
        """Tear down the existing source bin (if any), add the new
        one, link to downstream. Returns the pipeline to PLAYING if
        start_state == PLAYING; on the initial install the caller
        promotes state once everything is wired up.
        """
        if self._source_bin is not None:
            # PAUSED first so the GL elements don't hold buffers
            # across the unlink; then NULL to release resources.
            old = self._source_bin
            self._source_bin = None
            self.pipeline.set_state(Gst.State.READY)
            old.unlink(self._downstream_bin)
            old.set_state(Gst.State.NULL)
            self.pipeline.remove(old)

        self.pipeline.add(new_bin)
        if not new_bin.link(self._downstream_bin):
            print("[vj] source → downstream link failed")
        self._source_bin = new_bin

        if start_state == Gst.State.PLAYING:
            self.pipeline.set_state(Gst.State.PLAYING)

    # ── Sinks → windows binding ────────────────────────────────────

    def _bind_sinks_to_windows(self):
        """Pull gtksink widgets out of the downstream bin and pack
        them into the two top-level windows."""
        output_sink = self.pipeline.get_by_name("output_sink")
        hud_sink = self.pipeline.get_by_name("hud_sink")
        if output_sink is None or hud_sink is None:
            self._fail("gtksink elements missing from the pipeline")
            return False
        output_widget = output_sink.get_property("widget")
        hud_widget = hud_sink.get_property("widget")
        # GTK3 quirk: gtksink's widget defaults to hidden.
        output_widget.show()
        hud_widget.show()
        self.output_window = self._build_output_window(output_widget)
        self.hud_window = self._build_hud_window(hud_widget)
        self.add_window(self.output_window)
        self.add_window(self.hud_window)
        return True

    # ── Window construction ────────────────────────────────────────

    def _build_output_window(self, video_widget):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("pi-paint VJ — Output")
        win.set_default_size(CANVAS_W, CANVAS_H)
        win.add(video_widget)
        win.connect("destroy", lambda *_: self.quit())
        win.connect("key-press-event", self._on_key_press)
        return win

    def _build_hud_window(self, video_widget):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("VJ Control")
        win.set_default_size(680, 720)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        video_widget.set_size_request(-1, 380)
        box.pack_start(video_widget, False, False, 0)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        self._status_label.set_line_wrap(True)
        box.pack_start(self._status_label, False, False, 0)
        self._refresh_status()

        # Key cheat sheet. Phase 6 will replace with a richer
        # panel + favourites grid; for now the operator can read
        # the active bindings off the HUD.
        keymap = Gtk.Label()
        keymap.set_markup(
            "<small>"
            "<b>Keys</b>:\n"
            "  <tt>- / =</tt>  prev / next clip\n"
            "  <tt>A</tt>      plasma\n"
            "  <tt>S</tt>      tunnel\n"
            "  <tt>G</tt>      waves\n"
            "  <tt>H</tt>      cells\n"
            "  <tt>K</tt>      moiré\n"
            "  <tt>L</tt>      metaballs\n"
            "  <tt>`</tt>      toggle nerd stats\n"
            "  <tt>Esc</tt>    quit"
            "</small>"
        )
        keymap.set_xalign(0.0)
        box.pack_start(keymap, False, False, 0)

        win.add(box)
        win.connect("destroy", lambda *_: self.quit())
        win.connect("key-press-event", self._on_key_press)
        return win

    def _refresh_status(self):
        if self._status_label is None:
            return
        kind, what = self._current_source if self._current_source else (None, None)
        if kind == "clip":
            text = (f"phase 4a — clip {self._current_clip_idx + 1}"
                    f"/{len(self._clips)}: {what.name}")
        elif kind == "generator":
            text = f"phase 4a — generator: {what}"
        else:
            text = "phase 4a — no source"
        self._status_label.set_markup(
            f"<b>VJ Control HUD</b>\n"
            f"<small>{GLib.markup_escape_text(text)}</small>"
        )

    # ── Monitor placement ──────────────────────────────────────────

    def _move_window_to_monitor(self, window, monitor_idx, fullscreen):
        """Pin a Gtk.Window to a specific physical monitor.

        Pi setups typically have the projector on monitor 1 and the
        small operator screen on monitor 0.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return
        n = display.get_n_monitors()
        if n == 0:
            return
        idx = min(max(0, monitor_idx), n - 1)
        monitor = display.get_monitor(idx)
        geo = monitor.get_geometry()
        window.move(geo.x, geo.y)
        if fullscreen:
            window.fullscreen_on_monitor(display.get_default_screen(), idx)

    # ── Input ──────────────────────────────────────────────────────

    def _on_key_press(self, _widget, event):
        key = event.keyval
        mod = event.state

        # Quit
        if key == Gdk.KEY_Escape:
            self.quit()
            return True

        # Clip cycling — `-` and `=` (plus their shifted twins for
        # operators who Shift-mash). Always step ±1 from the
        # remembered clip index, regardless of whether the current
        # source is a clip or a generator. Generators are "off to
        # the side" — they don't move the cycle pointer, so
        # generator → `=` lands on clip[idx+1], where idx is
        # whichever clip was active before the operator picked the
        # generator. Index wraps via modulo in _install_clip.
        if key in (Gdk.KEY_minus, Gdk.KEY_underscore):
            self._install_clip(self._current_clip_idx - 1)
            return True
        if key in (Gdk.KEY_equal, Gdk.KEY_plus):
            self._install_clip(self._current_clip_idx + 1)
            return True

        # Generator hotkeys — preserve the old pygame app's layout
        # so muscle memory carries over. Particle/line-based
        # generators (D=starfield, F=warp, J=lissajous) aren't
        # ported yet — they need different shader primitives than
        # a single fragment shader can do cleanly. The keys are
        # left unbound until those land.
        gen_keys = {
            Gdk.KEY_a: "plasma",
            Gdk.KEY_s: "tunnel",
            Gdk.KEY_g: "waves",
            Gdk.KEY_h: "cells",
            Gdk.KEY_k: "moire",
            Gdk.KEY_l: "metaballs",
        }
        if key in gen_keys and not (mod & Gdk.ModifierType.CONTROL_MASK):
            self._install_generator(gen_keys[key])
            return True

        return False

    # ── GStreamer bus ──────────────────────────────────────────────

    def _on_bus_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[vj.gst] ERROR: {err.message}")
            if dbg:
                print(f"[vj.gst] debug: {dbg}")
            self.quit()
        elif t == Gst.MessageType.WARNING:
            err, _ = msg.parse_warning()
            print(f"[vj.gst] warn: {err.message}")
        elif t == Gst.MessageType.EOS:
            # Only clip mode produces EOS — videotestsrc is is-live
            # so generators never end. seek_simple with FLUSH causes
            # a small frame stutter at the loop point; phase 4c/4d
            # is where we refine to a segment-seek loop.
            if self._current_source and self._current_source[0] == "clip":
                print("[vj.gst] EOS — looping current clip")
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0,
                )

    # ── Failure surface ────────────────────────────────────────────

    def _fail(self, message):
        print(f"[vj] FATAL: {message}", file=sys.stderr)
        try:
            dlg = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="pi-paint VJ failed to start",
            )
            dlg.format_secondary_text(message)
            dlg.run()
            dlg.destroy()
        except Exception:
            pass
        self.quit()
