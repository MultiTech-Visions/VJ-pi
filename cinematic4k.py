#!/usr/bin/env python3
"""Clean 4K cinematic player controlled by the main VJ app.

Runs under system Python because PyGObject/GStreamer live there on the Pi.
The decode path is the high-frame-rate one: hardware HEVC decode (playbin3
autoplugs v4l2slh265dec) → GL upload/convert → glimagesink. No FX, no mapping,
no CPU readback.

Keyboard: the glimagesink window holds the keyboard on the projector, and
glimagesink does NOT deliver key events under labwc/Wayland (the old
navigation-event relay never fired). So we read the operator's keyboard
straight from its evdev device — focus-independent — and relay each press to
the main app as "@@KEY <keysym>" on stdout, which the main app drains into its
one keymap.
"""
import glob
import json
import select
import struct
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v"}

# Linux input_event = struct timeval (2 longs) + __u16 type + __u16 code +
# __s32 value. 24 bytes on 64-bit (verified on the Pi).
_EV_FORMAT = "llHHi"
_EV_SIZE = struct.calcsize(_EV_FORMAT)
_EV_KEY = 0x01

# Linux input keycode -> keysym string the main app's relay understands
# (single chars and the X keysym names in engine._KEYSYM_TO_SDL). Covers the
# keys the VJ keymap uses: letters, digits, the punctuation row, arrows, Esc.
_KEYCODE_TO_KEYSYM = {
    1: "Escape",
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5",
    7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    12: "minus", 13: "equal", 14: "BackSpace", 15: "Tab",
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t",
    21: "y", 22: "u", 23: "i", 24: "o", 25: "p",
    26: "bracketleft", 27: "bracketright", 28: "Return",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g",
    35: "h", 36: "j", 37: "k", 38: "l",
    39: "semicolon", 40: "apostrophe", 41: "grave", 43: "backslash",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b",
    49: "n", 50: "m", 51: "comma", 52: "period", 53: "slash",
    57: "space",
    59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
    65: "F7", 66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
    103: "Up", 105: "Left", 106: "Right", 108: "Down", 111: "Delete",
}


def find_clips(clips_dir):
    d = Path(clips_dir)
    if not d.exists():
        return []
    exts = {".mp4"} if d.name == "processed" else VIDEO_EXTS
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("_")
    )


class CinematicPlayer:
    """Resident player: ONE playbin3 + glimagesink, built once. Switching
    clips swaps the `uri` (PLAYING -> READY -> set uri -> PLAYING) instead of
    tearing the pipeline down, so the glimagesink window — and the keyboard
    focus and GL context that ride on it — is created once and never destroyed
    between clips. (Verified on the Pi: the output window stays mapped
    continuously across switches, so focus is stolen exactly once.)"""

    def __init__(self, clips):
        self.clips = clips
        self.idx = 0
        self.pipeline = None
        self.sink = None
        self.mainloop = None
        self.fail_count = 0
        self._stop = False
        self._key_thread = None

    def start(self):
        if self.clips:
            self._build_pipeline()
            self._load_current()

    def stop(self):
        self._stop = True
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.sink = None

    def switch(self, delta):
        """Move through the playlist without rebuilding anything — just point
        the resident pipeline at the next file. The window stays put."""
        if not self.clips:
            return
        self.idx = (self.idx + delta) % len(self.clips)
        self._load_current()

    def _build_pipeline(self):
        # playbin3 autoplugs the decoder by rank, so the Pi's hardware HEVC
        # decoder (v4l2slh265dec, ranked above primary) is still chosen for 4K
        # HEVC exactly as the old hand-built pipeline forced — and decodebin3
        # also copes with the odd non-HEVC file instead of failing not-linked.
        # glsinkbin wraps the same glupload ! glcolorconvert ! glimagesink the
        # old pipeline used (one GL context, frames stay on the GPU — no CPU
        # readback). fakesink for audio: these are silent visuals and we never
        # want the player touching the audio device.
        self.pipeline = (Gst.ElementFactory.make("playbin3", "player")
                         or Gst.ElementFactory.make("playbin", "player"))
        sinkbin = Gst.ElementFactory.make("glsinkbin", "glsinkbin")
        self.sink = Gst.ElementFactory.make("glimagesink", "sink")
        self.sink.set_property("sync", True)
        sinkbin.set_property("sink", self.sink)
        self.pipeline.set_property("video-sink", sinkbin)
        self.pipeline.set_property(
            "audio-sink", Gst.ElementFactory.make("fakesink", "noaudio"))
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

    def _load_current(self):
        if self.pipeline is None:
            self._build_pipeline()
        clip = self.clips[self.idx]
        print(f"[cinematic] clip [{self.idx + 1}/{len(self.clips)}] {clip.name}",
              flush=True)
        # `uri` can only change in NULL/READY; drop to READY (NOT NULL) so the
        # sink — and its on-screen window — survive the swap.
        self.pipeline.set_state(Gst.State.READY)
        self.pipeline.set_property("uri", Gst.filename_to_uri(str(clip)))
        self.pipeline.set_state(Gst.State.PLAYING)

    def start_key_relay(self):
        """Read the operator's keyboard straight from its evdev device and
        relay each press to the main app ("@@KEY <keysym>" on stdout) — the
        same channel the main app already drains into its one keymap. This is
        focus-independent, so it works even though the glimagesink window holds
        the keyboard on the projector (which is why the old glimagesink-
        navigation relay never fired under labwc/Wayland). Esc/q also quit the
        player locally as a guaranteed backstop, so the operator can never be
        trapped even if the main app isn't reading."""
        self._key_thread = threading.Thread(target=self._key_relay_loop,
                                             daemon=True)
        self._key_thread.start()

    def _key_relay_loop(self):
        devs = sorted(glob.glob("/dev/input/by-id/*-event-kbd"))
        if not devs:
            print("[cinematic] key-relay: no /dev/input keyboard found — "
                  "keys won't reach the main app", flush=True)
            return
        fds = []
        for dev in devs:
            try:
                fds.append(open(dev, "rb", buffering=0))
            except OSError as exc:
                print(f"[cinematic] key-relay: can't open {dev}: {exc}",
                      flush=True)
        if not fds:
            return
        print(f"[cinematic] key-relay: reading {', '.join(devs)}", flush=True)
        try:
            while not self._stop:
                try:
                    ready, _, _ = select.select(fds, [], [], 0.5)
                except OSError:
                    break
                for handle in ready:
                    try:
                        data = handle.read(_EV_SIZE)
                    except OSError:
                        continue
                    if not data or len(data) < _EV_SIZE:
                        continue
                    _, _, etype, code, value = struct.unpack(_EV_FORMAT, data)
                    # value 1 = press (2 = autorepeat, 0 = release): one tap
                    # per physical press, matching the rest of the app.
                    if etype != _EV_KEY or value != 1:
                        continue
                    keysym = _KEYCODE_TO_KEYSYM.get(code)
                    if not keysym:
                        continue
                    print(f"@@KEY {keysym}", flush=True)
                    if (keysym.lower() in ("escape", "q")
                            and self.mainloop is not None):
                        GLib.idle_add(lambda: (self.mainloop.quit(), False)[1])
        finally:
            for handle in fds:
                try:
                    handle.close()
                except OSError:
                    pass

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
    player.start_key_relay()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
