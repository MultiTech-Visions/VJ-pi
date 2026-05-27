"""GTK3 application + GStreamer pipeline.

Architecture (single GL/EGL context, V3D-native on Pi 5):

    filesrc ─ decodebin ─┐
                         │
                         ▼
    videoconvert ─ videoscale ─ glupload ─ tee ─┬─▶ gldownload ─ videoconvert ─ gtksink (output)
                                                └─▶ gldownload ─ videoconvert ─ gtksink (HUD)

The tee forks GL textures by refcount (zero-copy). Each branch then
downloads back to CPU memory because `gtksink` is a CPU sink — it
accepts `video/x-raw` (system memory), not `video/x-raw(memory:GLMemory)`.

This intentionally avoids `gtkglsink` (the GL-aware variant): each
gtkglsink widget creates its own GL context, and Pi 5's V3D driver
leaks state between contexts in the same process — the bug that
killed the previous architecture. One pipeline-internal GL context
+ CPU presentation = the safe combination.

The download cost is one memcpy per sink per frame — at the HUD's
small preview size + a single projector readout, that's <1ms per
frame on Pi 5. The win comes later when the GL pipeline does real
work (decode → composite → shaders → tee → download) and the
expensive bits stay GPU-resident.

Phase 2: real clip playback. filesrc + decodebin replace
videotestsrc. The first .mp4 / .mov in assets/clips/ plays in a
loop on both windows. No clip selection, no FX, no mapping yet —
just proves H.264 / HEVC decode → glupload → tee → display works.

GTK3 (not GTK4) because Pi OS Bookworm's apt doesn't carry
gst-plugins-rs (where gtk4paintablesink lives). gtksink from
gstreamer1.0-gtk3 is in apt. Upgrade path to GTK4 is contained
when Pi OS adopts Trixie.
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

# Downstream chain — everything from videoconvert to the two sinks.
# Built once as a Gst.Bin via parse_bin_from_description so we can
# hand a single sink pad to decodebin's dynamic pad linker.
DOWNSTREAM_DESC = (
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
    (projector output + control HUD). Phase 2 plays one looped clip
    through the dual-sink GL pipeline; later phases hang sources,
    FX, mapping, and UI off this skeleton.
    """

    def __init__(self):
        super().__init__(
            application_id="com.multitech.vjpi",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.pipeline = None
        self.output_window = None
        self.hud_window = None
        self._downstream_bin = None

    # ── GTK Application lifecycle ──────────────────────────────────

    def do_command_line(self, cmdline):
        self.activate()
        return 0

    def do_activate(self):
        Gst.init(None)

        clip = _pick_first_clip()
        if clip is None:
            self._fail(
                f"No clips found in {CLIPS_DIR}\n\n"
                "Drop an .mp4 or .mov file in there and re-launch.\n"
                "Phase 2 plays whichever clip sorts first."
            )
            return
        print(f"[vj] playing clip: {clip.name}")

        # Build the pipeline manually so we can attach decodebin's
        # dynamic video pad to the downstream chain at runtime.
        # parse_launch can theoretically handle this for video-only
        # files but breaks on audio+video mp4 (the deferred link
        # races to whichever pad emerges first). Doing it explicitly
        # is ~10 lines and removes the race.
        self.pipeline = Gst.Pipeline.new("vj-pipeline")

        source = Gst.ElementFactory.make("filesrc", "source")
        source.set_property("location", str(clip))

        decoder = Gst.ElementFactory.make("decodebin", "decoder")
        decoder.connect("pad-added", self._on_decoder_pad_added)

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

        for el in (source, decoder, self._downstream_bin):
            self.pipeline.add(el)

        if not source.link(decoder):
            self._fail("Could not link filesrc → decodebin")
            return
        # decodebin → downstream is linked in _on_decoder_pad_added
        # once decodebin figures out the stream type.

        output_sink = self.pipeline.get_by_name("output_sink")
        hud_sink = self.pipeline.get_by_name("hud_sink")
        if output_sink is None or hud_sink is None:
            self._fail("gtksink elements missing from the parsed downstream bin")
            return

        output_widget = output_sink.get_property("widget")
        hud_widget = hud_sink.get_property("widget")
        # GTK3 quirk: gtksink's widget needs an explicit show() before
        # the window draws — its default visible state is False.
        output_widget.show()
        hud_widget.show()

        self.output_window = self._build_output_window(output_widget)
        self.hud_window = self._build_hud_window(hud_widget, clip.name)
        self.add_window(self.output_window)
        self.add_window(self.hud_window)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self.output_window.show_all()
        self.hud_window.show_all()
        self._move_window_to_monitor(self.output_window, 1, fullscreen=True)
        self._move_window_to_monitor(self.hud_window, 0, fullscreen=False)

        self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[vj] pipeline up: {Gst.version_string()}")

    def do_shutdown(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        Gtk.Application.do_shutdown(self)

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

    def _build_hud_window(self, video_widget, clip_name):
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
            f"<b>VJ Control HUD</b>\n"
            f"<small>phase 2 — playing: {GLib.markup_escape_text(clip_name)}</small>"
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
            # Loop the clip: seek back to position 0. seek_simple with
            # FLUSH causes a small frame stutter at the loop point; a
            # proper segment-seek loop is smoother but requires more
            # state. Phase 4 (where we add the clip pool + crossfades)
            # is the right time to refine this.
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
