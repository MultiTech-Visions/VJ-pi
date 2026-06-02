"""play4k.py — dead-simple 4K HEVC clip player for the projector.

ONE job: play H.265/HEVC clips fullscreen on the projector at the highest
frame rate the hardware allows, using the proven zero-copy GPU path
(hardware decode -> GL -> display, ~42fps at 4K, no CPU touch). No FX, no
generators, no compositing — that's what keeps it fast and stable.

Separate process from the VJ app: it cannot affect the main rig.

Run (SYSTEM python3 — the one with gi):
    python3 play4k.py [clips_dir]          # default: assets/clips

Type + Enter in the terminal:  n = next   p = previous   q = quit
Each clip loops until you move on. Fullscreen-on-projector is handled by
the labwc window rule (run 'Apply Fullscreen Rule.sh' once).

Clips MUST be H.265/HEVC in MP4 — the Pi 5 only hardware-decodes HEVC.
Non-HEVC files are skipped with a log line.
"""
import json
import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

HERE = Path(__file__).resolve().parent
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".MP4", ".MOV", ".MKV", ".M4V")


def find_clips(clips_dir):
    d = Path(clips_dir)
    if not d.exists():
        return []
    return [p for p in sorted(d.iterdir())
            if p.suffix in VIDEO_EXTS and not p.name.startswith("_")]


class Player:
    def __init__(self, clips):
        self.clips = clips
        self.idx = 0
        self.pipeline = None
        self.mainloop = None
        self.err_streak = 0

    def _build(self):
        clip = self.clips[self.idx]
        print(f"[play4k] [{self.idx + 1}/{len(self.clips)}] {clip.name}",
              flush=True)
        # The proven path: explicit HEVC decoder -> GL -> display. No FX
        # pass, so it runs at full speed (~42fps at 4K).
        loc = str(clip).replace("\\", "\\\\").replace('"', '\\"')
        desc = (
            f'filesrc location="{loc}" ! qtdemux ! h265parse ! '
            'v4l2slh265dec ! glupload ! glcolorconvert ! '
            'glimagesink name=sink sync=true'
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            print(f"[play4k] build failed: {exc}", flush=True)
            self.pipeline = None
            return
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _teardown(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def start(self):
        if self.clips:
            self._build()

    def switch(self, delta):
        if not self.clips:
            return
        self.idx = (self.idx + delta) % len(self.clips)
        self._teardown()
        self._build()

    def _on_bus(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            self.err_streak = 0
        elif t == Gst.MessageType.EOS:
            self.err_streak = 0
            if self.pipeline is not None:
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[play4k] ERROR ({self.clips[self.idx].name}): "
                  f"{err.message} :: {dbg}", flush=True)
            self.err_streak += 1
            if self.err_streak > len(self.clips):
                print("[play4k] every clip failed — stopping. Clips must be "
                      "H.265/HEVC in MP4.", flush=True)
                if self.mainloop is not None:
                    self.mainloop.quit()
                return
            GLib.idle_add(lambda: (self.switch(1), False)[1])


def _reader(dispatch):
    for raw in sys.stdin:
        line = raw.strip()
        if line:
            GLib.idle_add(lambda l=line: (dispatch(l), False)[1])


def main():
    clips_dir = sys.argv[1] if len(sys.argv) > 1 else str(
        HERE / "assets" / "clips")
    Gst.init(None)
    clips = find_clips(clips_dir)
    if not clips:
        print(f"[play4k] no clips in {clips_dir}", flush=True)
        return 1
    print(f"[play4k] {len(clips)} clip(s) from {clips_dir}", flush=True)
    player = Player(clips)

    def dispatch(line):
        try:
            if line.startswith("{"):
                cmd = json.loads(line).get("cmd")
                if cmd == "next":
                    player.switch(1)
                elif cmd == "prev":
                    player.switch(-1)
                elif cmd == "quit":
                    loop.quit()
                return
            k = line.split()[0].lower()
            if k in ("n", "next"):
                player.switch(1)
            elif k in ("p", "prev"):
                player.switch(-1)
            elif k in ("q", "quit"):
                loop.quit()
        except Exception as exc:  # noqa: BLE001
            print(f"[play4k] bad command {line!r}: {exc!r}", flush=True)

    loop = GLib.MainLoop()
    player.mainloop = loop
    threading.Thread(target=_reader, args=(dispatch,), daemon=True).start()
    player.start()
    print("[play4k] keys: n=next  p=prev  q=quit", flush=True)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        player._teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
