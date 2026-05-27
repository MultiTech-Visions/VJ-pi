"""GTK3 application + GStreamer pipeline.

Architecture (single GL/EGL context, V3D-native on Pi 5):

  CLIP source:
    filesrc ─ decodebin ─[dynamic pad]─▶ downstream
                                          │
        videoconvert ─ videoscale ─ glupload ─ tee ─┬─▶ gldownload ─ videoconvert ─ gtksink (output)
                                                    └─▶ gldownload ─ videoconvert ─ gtksink (HUD)

  GENERATOR source (phase 3 — tunnel, plasma, …):
    videotestsrc ─ glupload ─ glshader[GLSL] ─ tee ─┬─▶ gldownload ─ videoconvert ─ gtksink (output)
                                                    └─▶ gldownload ─ videoconvert ─ gtksink (HUD)

The tee forks GL textures by refcount (zero-copy). Each branch then
downloads back to CPU memory because `gtksink` is a CPU sink — it
accepts `video/x-raw` (system memory), not `video/x-raw(memory:GLMemory)`.

This intentionally avoids `gtkglsink` (the GL-aware variant): each
gtkglsink widget creates its own GL context, and Pi 5's V3D driver
leaks state between contexts in the same process — the bug that
killed the previous architecture. One pipeline-internal GL context
+ CPU presentation = the safe combination.

Phase 3 lands generators as GLSL fragment shaders running through
`glshader` in the same pipeline. CPU stays out of the per-pixel math
entirely; the shader writes the framebuffer on V3D and the tee
forks the GL texture into both sinks. No numpy, no cv2 — just GLSL
and the same dual-sink downstream that already works for clips.

The shader uniforms `time` (float, seconds since start) and
`v_texcoord` (vec2, 0..1) are provided by gstreamer's glshader
plugin. The GLSL targets GLES 2.0 (`#version 100`) for max
compatibility with V3D's exposed profile.
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

# Canvas resolution. Sources get scaled to this before glupload so the
# downstream chain operates on a fixed-size buffer regardless of clip
# resolution. 1280x720 keeps the per-frame cost modest on Pi 5 while
# looking sharp on a 1080p projector after the final upscale.
CANVAS_W = 1280
CANVAS_H = 720

# Downstream chain for the CLIP source — normalises to canvas size,
# uploads to GL, forks via tee, downloads back to CPU for each
# gtksink. Built once via parse_bin_from_description so decodebin's
# dynamic pad can be linked to its single ghost sink.
DOWNSTREAM_CLIP_DESC = (
    "videoconvert ! videoscale ! "
    f"video/x-raw,width={CANVAS_W},height={CANVAS_H} ! "
    "glupload ! "
    "tee name=t allow-not-linked=true "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  gldownload ! videoconvert ! "
    "  gtksink name=output_sink sync=false "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  gldownload ! videoconvert ! "
    "  gtksink name=hud_sink sync=false"
)

# Full pipeline string for GENERATOR sources — videotestsrc gives us a
# dummy framebuffer at canvas size; glshader ignores it and writes its
# own procedural content; tee + two sinks identical to the clip path.
# We use parse_launch here (not parse_bin_from_description) because
# there's no dynamic pad to wrangle.
def _generator_pipeline(shader_name):
    return (
        f"videotestsrc is-live=true pattern=black ! "
        f"video/x-raw,width={CANVAS_W},height={CANVAS_H},framerate=30/1 ! "
        f"glupload ! "
        f"glshader name={shader_name} ! "
        f"tee name=t allow-not-linked=true "
        f"t. ! queue max-size-buffers=2 leaky=downstream ! "
        f"  gldownload ! videoconvert ! "
        f"  gtksink name=output_sink sync=false "
        f"t. ! queue max-size-buffers=2 leaky=downstream ! "
        f"  gldownload ! videoconvert ! "
        f"  gtksink name=hud_sink sync=false"
    )


# ── GLSL fragment shaders ─────────────────────────────────────────
#
# All shaders target GLES 2.0 (#version 100 + precision qualifier)
# for V3D compatibility. gstreamer's glshader plugin provides:
#   varying vec2 v_texcoord   — UV in [0, 1]
#   uniform float time        — running time in seconds
#   uniform sampler2D tex     — input texture (we ignore it; the
#                                shader is fully generative)
# We hardcode canvas dimensions (1280×720) in the shader rather
# than uniforming them — they're stable for a session, and avoiding
# the uniform lookup is a wash either way.

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

# Direct port of the original effects.tunnel() — a radial
# checkerboard tunnel with hue cycling. `chk` (0 or 1) modulates the
# brightness channel of HSV so the checker squares alternate
# colour/black; hue varies along the angle plus a time drift.
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
    // v_texcoord is 0..1; centre on origin and scale to pixel-ish
    // coords so the 200.0 / r factor matches the CPU original.
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

GENERATORS = {
    "plasma": PLASMA_SHADER,
    "tunnel": TUNNEL_SHADER,
}


def _pick_first_clip():
    """Return the first .mp4 / .mov clip in assets/clips/, or None.

    Phase 2 plays a single hardcoded clip — whichever sorts first.
    Phase 4 swaps this for the full clip pool + favourites + keyboard
    selection.
    """
    if not CLIPS_DIR.exists():
        return None
    candidates = (sorted(CLIPS_DIR.glob("*.mp4"))
                  + sorted(CLIPS_DIR.glob("*.mov"))
                  + sorted(CLIPS_DIR.glob("*.MP4"))
                  + sorted(CLIPS_DIR.glob("*.MOV")))
    return candidates[0] if candidates else None


class VJApp(Gtk.Application):
    """GTK3 + GStreamer application skeleton for the VJ rewrite.

    Owns the GStreamer pipeline and the two top-level windows
    (projector output + control HUD). source_kind selects which
    Gst pipeline gets built — currently `clip` (the default, plays
    the first .mp4 in assets/clips/) or `plasma` / `tunnel` (GPU
    generators via glshader).
    """

    def __init__(self, source_kind="clip", single_screen=False):
        super().__init__(
            application_id="com.multitech.vjpi",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.source_kind = source_kind
        self.single_screen = single_screen
        self.pipeline = None
        self.output_window = None
        self.hud_window = None
        self._downstream_bin = None
        self._status_text = ""

    # ── GTK Application lifecycle ──────────────────────────────────

    def do_command_line(self, cmdline):
        self.activate()
        return 0

    def do_activate(self):
        Gst.init(None)
        print(f"[vj] source: {self.source_kind}")

        if self.source_kind in GENERATORS:
            ok = self._build_generator_pipeline(self.source_kind)
        else:
            ok = self._build_clip_pipeline()
        if not ok:
            return

        if not self._bind_sinks_to_windows():
            return

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self.output_window.show_all()
        self.hud_window.show_all()
        if self.single_screen:
            # Both on display 0, neither fullscreen — test mode.
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

    # ── Pipeline construction ─────────────────────────────────────

    def _build_clip_pipeline(self):
        """Phase 2 clip path: filesrc + decodebin → downstream bin.

        Returns True on success, False on fatal setup error (in
        which case _fail has already been called).
        """
        clip = _pick_first_clip()
        if clip is None:
            self._fail(
                f"No clips found in {CLIPS_DIR}\n\n"
                "Drop an .mp4 or .mov file in there and re-launch.\n"
                "Or try Test Tunnel.sh / Test Plasma.sh for a GPU\n"
                "generator that doesn't need a clip."
            )
            return False
        print(f"[vj] playing clip: {clip.name}")
        self._status_text = f"phase 2 — playing: {clip.name}"

        # Build manually so we can attach decodebin's dynamic video
        # pad to the downstream chain at runtime. parse_launch can
        # theoretically handle this for video-only files but races
        # on audio+video mp4 (the deferred link goes to whichever
        # pad emerges first). Doing it explicitly removes the race.
        self.pipeline = Gst.Pipeline.new("vj-pipeline")

        source = Gst.ElementFactory.make("filesrc", "source")
        source.set_property("location", str(clip))

        decoder = Gst.ElementFactory.make("decodebin", "decoder")
        decoder.connect("pad-added", self._on_decoder_pad_added)

        try:
            self._downstream_bin = Gst.parse_bin_from_description(
                DOWNSTREAM_CLIP_DESC, True
            )
        except GLib.Error as exc:
            self._fail(
                "Could not build the downstream GStreamer chain.\n"
                f"  Error: {exc.message}\n\n"
                "Hint: is `gstreamer1.0-gtk3` installed? Re-run setup.sh."
            )
            return False
        self._downstream_bin.set_name("downstream")

        for el in (source, decoder, self._downstream_bin):
            self.pipeline.add(el)
        if not source.link(decoder):
            self._fail("Could not link filesrc → decodebin")
            return False
        # decodebin → downstream is linked in _on_decoder_pad_added
        # once decodebin figures out the stream type.
        return True

    def _build_generator_pipeline(self, name):
        """Phase 3 generator path: videotestsrc → glupload → glshader
        → tee → 2× sinks. The whole pipeline is a single parse_launch
        string — no dynamic pads to handle.
        """
        if name not in GENERATORS:
            self._fail(f"Unknown generator: {name!r}")
            return False
        print(f"[vj] running generator: {name}")
        self._status_text = f"phase 3 — generator: {name}"

        try:
            self.pipeline = Gst.parse_launch(_generator_pipeline(name))
        except GLib.Error as exc:
            self._fail(
                "Could not build the generator pipeline.\n"
                f"  Error: {exc.message}\n\n"
                "Hint: is `gstreamer1.0-gl` installed? Re-run setup.sh."
            )
            return False

        # glshader's `fragment` property takes the GLSL source as a
        # string. Set it via property rather than inline in the
        # parse_launch caps to keep the shader text out of the
        # parse_launch grammar (which would need escaping).
        shader = self.pipeline.get_by_name(name)
        if shader is None:
            self._fail("glshader element not found in parsed pipeline")
            return False
        shader.set_property("fragment", GENERATORS[name])
        return True

    def _bind_sinks_to_windows(self):
        """Pull gtksink widgets out of the pipeline and pack them
        into the two top-level windows. Common to both clip and
        generator paths."""
        output_sink = self.pipeline.get_by_name("output_sink")
        hud_sink = self.pipeline.get_by_name("hud_sink")
        if output_sink is None or hud_sink is None:
            self._fail("gtksink elements missing from the pipeline")
            return False

        output_widget = output_sink.get_property("widget")
        hud_widget = hud_sink.get_property("widget")
        # GTK3 quirk: gtksink's widget defaults to hidden; needs an
        # explicit show() before the window draws.
        output_widget.show()
        hud_widget.show()

        self.output_window = self._build_output_window(output_widget)
        self.hud_window = self._build_hud_window(hud_widget)
        self.add_window(self.output_window)
        self.add_window(self.hud_window)
        return True

    # ── decodebin dynamic pad ──────────────────────────────────────

    def _on_decoder_pad_added(self, _decoder, new_pad):
        """decodebin emits a pad per detected stream. We only want
        video — ignore audio (most mp4s have both). The downstream
        bin has a single ghost sink (the videoconvert input)."""
        caps = new_pad.get_current_caps()
        if caps is None:
            return
        struct_name = caps.get_structure(0).get_name()
        if not struct_name.startswith("video/"):
            return
        sink_pad = self._downstream_bin.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        result = new_pad.link(sink_pad)
        if result == Gst.PadLinkReturn.OK:
            print(f"[vj] decoder → downstream linked ({struct_name})")
        else:
            print(f"[vj] decoder link failed: {result}")

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
        # Vertical box: preview on top, placeholder status below.
        # Later phases add favourites grid, FPS readout, status
        # panel, key cheat sheet etc.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        video_widget.set_size_request(-1, 380)
        box.pack_start(video_widget, False, False, 0)
        status = Gtk.Label()
        status.set_markup(
            "<b>VJ Control HUD</b>\n"
            f"<small>{GLib.markup_escape_text(self._status_text)}</small>"
        )
        status.set_xalign(0.0)
        box.pack_start(status, False, False, 0)
        win.add(box)
        win.connect("destroy", lambda *_: self.quit())
        win.connect("key-press-event", self._on_key_press)
        return win

    # ── Monitor placement ──────────────────────────────────────────

    def _move_window_to_monitor(self, window, monitor_idx, fullscreen):
        """Pin a Gtk.Window to a specific physical monitor.

        Pi setups typically have the projector on monitor 1 and the
        small operator screen on monitor 0. We position via the
        monitor's geometry (absolute screen coords on multi-head X)
        then optionally fullscreen.
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
        if key == Gdk.KEY_Escape and (mod & Gdk.ModifierType.SHIFT_MASK):
            self.quit()
            return True
        if key == Gdk.KEY_Escape:
            self.quit()
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
            # Clip mode: loop by seeking to 0. seek_simple with FLUSH
            # causes a small frame stutter at the loop point; phase 4
            # (clip pool + crossfades) is the right time to refine.
            # Generator mode never EOSes (videotestsrc is is-live).
            if self.source_kind == "clip":
                print("[vj.gst] EOS — looping")
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0,
                )

    # ── Failure surface ────────────────────────────────────────────

    def _fail(self, message):
        print(f"[vj] FATAL: {message}", file=sys.stderr)
        # Best-effort dialog so a double-clicked launch surfaces the
        # error instead of vanishing. The launcher already pops a
        # zenity dialog on non-zero exit; this just gets the same
        # info to the operator one frame sooner.
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
