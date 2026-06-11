#!/usr/bin/env python3
"""Clean 4K cinematic player controlled by the main VJ app.

Runs under system Python because PyGObject/GStreamer live there on the Pi.
The pipeline deliberately stays tiny:

    filesrc ! qtdemux ! h265parse ! v4l2slh265dec ! glupload !
    glcolorconvert ! glimagesink

That is the high-frame-rate path: hardware HEVC decode, no CPU frame copy,
no FX, no mapping, no readback.
"""
import json
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

# GstVideo provides the navigation-event helpers we use to read key presses
# the glimagesink window receives. Optional: if it's missing, the player still
# runs (just without in-window keys), driven by stdin from the main app.
try:
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo  # noqa: E402
    _HAVE_NAV = True
except (ValueError, ImportError):
    GstVideo = None
    _HAVE_NAV = False


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v"}


def find_clips(clips_dir):
    d = Path(clips_dir)
    if not d.exists():
        return []
    exts = {".mp4"} if d.name == "processed" else VIDEO_EXTS
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("_")
    )


def gst_escape(path):
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


class CinematicPlayer:
    def __init__(self, clips):
        self.clips = clips
        self.idx = 0
        self.pipeline = None
        self.mainloop = None
        self.fail_count = 0

    def start(self):
        if self.clips:
            self._build_current()

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def switch(self, delta):
        if not self.clips:
            return
        self.idx = (self.idx + delta) % len(self.clips)
        self.stop()
        self._build_current()

    def _build_current(self):
        clip = self.clips[self.idx]
        print(f"[cinematic] clip [{self.idx + 1}/{len(self.clips)}] {clip.name}",
              flush=True)
        desc = (
            f'filesrc location="{gst_escape(clip)}" ! qtdemux ! h265parse ! '
            "v4l2slh265dec ! glupload ! glcolorconvert ! "
            "glimagesink name=sink sync=true"
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            print(f"[cinematic] pipeline build failed: {exc}", flush=True)
            self.pipeline = None
            self._skip_failed()
            return
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        # Let the sink's own window drive prev/next/quit by keyboard. The
        # glimagesink window grabs focus on the projector, so without this the
        # operator's keys never reach the main VJ app — they land here. Key
        # presses arrive as upstream NAVIGATION events on the sink pad.
        if _HAVE_NAV:
            sink = self.pipeline.get_by_name("sink")
            pad = sink.get_static_pad("sink") if sink is not None else None
            if pad is not None:
                pad.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, self._nav_probe)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _nav_probe(self, _pad, info):
        event = info.get_event()
        if event is None or event.type != Gst.EventType.NAVIGATION:
            return Gst.PadProbeReturn.OK
        try:
            if (GstVideo.navigation_event_get_type(event)
                    == GstVideo.NavigationEventType.KEY_PRESS):
                ok, key = GstVideo.navigation_event_parse_key_event(event)
                if ok:
                    GLib.idle_add(lambda k=key: (self.on_key(k), False)[1])
        except Exception as exc:  # noqa: BLE001
            print(f"[cinematic] nav parse failed: {exc!r}", flush=True)
        return Gst.PadProbeReturn.OK

    def on_key(self, key):
        """A key press landed on the 4K window (which holds the keyboard on
        the projector). Relay it to the main VJ app so its ONE keymap decides
        what it means — cycling, mapping, favourites, exit, all the same keys
        as everywhere else. Esc/q also quit locally as a guaranteed backstop
        so the operator can never be trapped, even if the app isn't reading."""
        if key:
            # Relay marker the main app parses off our stdout.
            print(f"@@KEY {key}", flush=True)
        k = (key or "").lower()
        if k in ("escape", "q") and self.mainloop is not None:
            self.mainloop.quit()

    def _on_bus(self, _bus, msg):
        if msg.type == Gst.MessageType.ASYNC_DONE:
            self.fail_count = 0
        elif msg.type == Gst.MessageType.EOS:
            if self.pipeline is not None:
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0,
                )
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[cinematic] ERROR ({self.clips[self.idx].name}): "
                  f"{err.message} :: {dbg}", flush=True)
            self._skip_failed()

    def _skip_failed(self):
        self.fail_count += 1
        if self.fail_count >= len(self.clips):
            print("[cinematic] every clip failed. Run assets/Process 4K Assets.sh "
                  "to create Pi-playable HEVC MP4s.", flush=True)
            if self.mainloop is not None:
                self.mainloop.quit()
            return
        GLib.idle_add(lambda: (self.switch(1), False)[1])


def stdin_reader(dispatch):
    for raw in sys.stdin:
        line = raw.strip()
        if line:
            GLib.idle_add(lambda l=line: (dispatch(l), False)[1])


def main(argv):
    clips_dir = argv[1] if len(argv) > 1 else "assets/4k/processed"
    Gst.init(None)
    clips = find_clips(clips_dir)
    if not clips:
        print(f"[cinematic] no clips in {clips_dir}", flush=True)
        return 1
    print(f"[cinematic] {len(clips)} clip(s) from {clips_dir}", flush=True)

    loop = GLib.MainLoop()
    player = CinematicPlayer(clips)
    player.mainloop = loop

    def dispatch(line):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"cmd": line.split()[0].lower()}
        cmd = payload.get("cmd")
        if cmd == "next":
            player.switch(1)
        elif cmd == "prev":
            player.switch(-1)
        elif cmd == "quit":
            loop.quit()

    threading.Thread(target=stdin_reader, args=(dispatch,), daemon=True).start()
    player.start()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
