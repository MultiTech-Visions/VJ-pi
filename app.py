"""GTK3 application + GStreamer pipeline.

Architecture (single GL/EGL context, V3D-native on Pi 5):

    videotestsrc ─ glupload ─ tee ─┬─▶ glcolorconvert ─ gtksink (output)
                                   └─▶ glcolorconvert ─ gtksink (HUD)

Both sinks share the same GL pipeline state — no second context, no
SDL/EGL state-leak fight, no per-frame readback. The HUD's preview
widget IS the same pipeline output as the projector, just packed
into a smaller GTK widget.

Phase 1: this scaffold. videotestsrc only. Two windows on two
displays. No clips, no generators, no FX, no mapping. Goal is to
prove the dual-sink GL pipeline actually works on real Pi 5 V3D
before any VJ logic depends on it.

GTK3 (not GTK4) because Pi OS Bookworm's apt repo doesn't carry
gst-plugins-rs (which is where gtk4paintablesink lives). gtksink
from gstreamer1.0-gtk3 is in apt and works the same way for our
purposes — `widget` property exposes a Gtk.Widget we pack into a
window. Upgrade path to GTK4 is contained when Pi OS adopts it.
"""
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gst, Gdk, Gio, GLib  # noqa: E402


# One source, GL-uploaded, forked via `tee` into two `gtksink`s.
# Each gtksink exposes a Gtk.Widget via its `widget` property — we
# pack those into the two windows. The GL context is shared across
# the whole pipeline so the tee just refcounts the GL textures
# instead of copying pixels.
PIPELINE = (
    "videotestsrc is-live=true pattern=smpte ! "
    "video/x-raw,width=1280,height=720,framerate=30/1 ! "
    "glupload ! "
    "tee name=t allow-not-linked=true "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  glcolorconvert ! gtksink name=output_sink sync=false "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  glcolorconvert ! gtksink name=hud_sink sync=false"
)


class VJApp(Gtk.Application):
    """GTK3 + GStreamer application skeleton for the VJ rewrite.

    Owns the GStreamer pipeline and the two top-level windows
    (projector output + control HUD). Phase 1 has zero VJ behaviour;
    later phases hang sources, FX, mapping, and UI off this skeleton.
    """

    def __init__(self):
        super().__init__(
            application_id="com.multitech.vjpi",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.pipeline = None
        self.output_window = None
        self.hud_window = None

    # ── GTK Application lifecycle ──────────────────────────────────

    def do_command_line(self, cmdline):
        self.activate()
        return 0

    def do_activate(self):
        Gst.init(None)
        try:
            self.pipeline = Gst.parse_launch(PIPELINE)
        except GLib.Error as exc:
            self._fail(
                "Could not build the GStreamer pipeline.\n"
                f"  Error: {exc.message}\n\n"
                "Hint: is `gstreamer1.0-gtk3` installed? Re-run setup.sh."
            )
            return

        output_sink = self.pipeline.get_by_name("output_sink")
        hud_sink = self.pipeline.get_by_name("hud_sink")
        if output_sink is None or hud_sink is None:
            self._fail("gtksink elements missing from the parsed pipeline")
            return

        output_widget = output_sink.get_property("widget")
        hud_widget = hud_sink.get_property("widget")
        # GTK3 quirk: gtksink's widget needs an explicit show() before
        # the window draws — its default visible state is False.
        output_widget.show()
        hud_widget.show()

        self.output_window = self._build_output_window(output_widget)
        self.hud_window = self._build_hud_window(hud_widget)
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

    # ── Window construction ────────────────────────────────────────

    def _build_output_window(self, video_widget):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("pi-paint VJ — Output")
        win.set_default_size(1280, 720)
        # Hide the cursor over the projection — operator can park it
        # somewhere off-screen if they want it back. (Phase 5+ flips
        # this on for mapping/edit mode.)
        win.add(video_widget)
        win.connect("destroy", lambda *_: self.quit())
        # Esc / Shift+Esc panic exit; F11 / F12 will be display
        # cycling in phase 4. For phase 1 just Esc-to-quit.
        win.connect("key-press-event", self._on_key_press)
        return win

    def _build_hud_window(self, video_widget):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("VJ Control")
        win.set_default_size(680, 720)
        # Vertical box: preview on top, placeholder status below.
        # Later phases add the favourites grid, FPS readout, status
        # panel, key cheat sheet etc. underneath the preview.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        # Constrain the preview height so the rest of the HUD has
        # room as we add to it. 380px ≈ same proportion the pygame
        # HUD used.
        video_widget.set_size_request(-1, 380)
        box.pack_start(video_widget, False, False, 0)
        placeholder = Gtk.Label()
        placeholder.set_markup(
            "<b>VJ Control HUD</b>\n"
            "<small>phase 1 scaffold — videotestsrc dual-sink proof</small>"
        )
        placeholder.set_xalign(0.0)
        box.pack_start(placeholder, False, False, 0)
        win.add(box)
        win.connect("destroy", lambda *_: self.quit())
        win.connect("key-press-event", self._on_key_press)
        return win

    # ── Monitor placement ──────────────────────────────────────────

    def _move_window_to_monitor(self, window, monitor_idx, fullscreen):
        """Pin a Gtk.Window to a specific physical monitor.

        Pi setups typically have the projector on monitor 1 and the
        small operator screen on monitor 0. We position via the
        monitor's geometry (Gdk.Monitor.get_geometry returns absolute
        screen coords on multi-head X) then optionally fullscreen.
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
        # Move to the monitor's top-left corner before realising
        # fullscreen so the WM picks the right physical screen.
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
            # Phase 1 has no FX/clip state to panic-clear, so Esc
            # alone just quits for now. Later phases route this to
            # the "panic" command (clear FX, overlays, hits).
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
            print("[vj.gst] end of stream")
            self.quit()

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
