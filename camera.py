"""Live USB-webcam source for the VJ rig.

A webcam is, to the rest of the pipeline, just another base layer: it
hands back an RGB numpy frame exactly like `clips.Clip.read()` does, so
the whole FX chain, overlays, hits, melt and projection-mapping warps
work on the live feed for free.

Two things make a webcam different from a clip file and are handled here:

1. **Blocking reads.** `cv2.VideoCapture.read()` waits for the camera's
   next frame (~33 ms at 30 fps). Calling it inline would peg the render
   loop to the camera's frame rate. A background grab thread keeps the
   newest frame ready so `read()` is a cheap copy/convert on the main
   thread — the standard capture pattern.

2. **Device discovery.** On a Pi the USB webcam is not reliably
   `/dev/video0` (the board exposes its own codec/ISP video nodes too),
   so when no device is forced we probe a handful of indices and keep the
   first one that actually delivers frames. The operator never has to
   know an index — which matters since they drive everything from the GUI.

No GL anywhere — this is pure CPU/V4L2, exactly like the software clip
path, so it stays clear of the V3D dual-context landmine.
"""
import threading
import time

import cv2
import numpy as np


# Indices probed when the device is set to auto (-1). Kept small so a
# cold start doesn't stall opening a dozen non-existent nodes.
_PROBE_INDICES = range(0, 6)


class CameraSource:
    """A threaded V4L2 webcam capture that yields mirrored RGB frames."""

    def __init__(self, device=-1, request_size=(1280, 720), fps=30,
                 mirror=True):
        # device < 0 → auto-probe for the first working camera.
        self.device = device
        self.req_w, self.req_h = request_size
        self.fps = fps
        self.mirror = mirror

        self._cap = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None          # newest raw BGR frame from the grabber
        self._frame_count = 0
        self.opened_index = None     # which /dev/videoN we actually opened
        self.status = "idle"         # short string for the HUD
        self.error = None            # last error string, if any

    # ── Device opening / probing ─────────────────────────────────────

    @staticmethod
    def _open_index(index, req_w, req_h, fps):
        """Open one capture device and request MJPG + size + fps. Returns the
        VideoCapture if it both opens AND delivers a real frame, else None.

        MJPG matters: most USB UVC webcams only reach 720p/1080p@30 over
        MJPG — the default YUYV path caps out at tiny resolutions or ~5 fps.
        """
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, req_w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, req_h)
            cap.set(cv2.CAP_PROP_FPS, fps)
            # Keep latency low — we always want the freshest frame.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # Give the device a few tries to warm up and deliver a real frame;
        # some webcams return empty frames for the first read or two.
        for _ in range(5):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                return cap
            time.sleep(0.05)
        cap.release()
        return None

    def _acquire(self):
        """Open the configured device, or probe for one. Returns True on
        success and leaves self._cap set; logs what it found."""
        if self.device is not None and self.device >= 0:
            cap = self._open_index(self.device, self.req_w, self.req_h, self.fps)
            if cap is not None:
                self._cap = cap
                self.opened_index = self.device
                return True
            self.error = f"camera /dev/video{self.device} gave no frames"
            print(f"[vj] camera: device {self.device} did not deliver frames")
            return False

        # Auto-probe.
        for idx in _PROBE_INDICES:
            cap = self._open_index(idx, self.req_w, self.req_h, self.fps)
            if cap is not None:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                self._cap = cap
                self.opened_index = idx
                print(f"[vj] camera: using /dev/video{idx} ({w}x{h})")
                return True
            print(f"[vj] camera: /dev/video{idx} not usable, trying next")
        self.error = "no working camera found (probed /dev/video0-5)"
        print("[vj] camera: no working camera found on /dev/video0-5")
        return False

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        """Open the device and spin up the grab thread. Returns True if the
        camera is live. Safe to call again once started (no-op)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        self.status = "opening"
        self.error = None
        if not self._acquire():
            self.status = "no camera"
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._grab_loop, daemon=True)
        self._thread.start()
        self.status = "live"
        return True

    def _grab_loop(self):
        """Background: keep the newest frame ready under the lock."""
        misses = 0
        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                break
            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                misses += 1
                if misses > 60:
                    # Camera dropped off (unplugged?). Stop churning.
                    self.error = "camera stopped delivering frames"
                    self.status = "lost signal"
                    print("[vj] camera: lost signal (60 empty reads)")
                    break
                time.sleep(0.01)
                continue
            misses = 0
            with self._lock:
                self._latest = frame
                self._frame_count += 1

    def read(self):
        """Return the newest frame as an RGB numpy array (mirrored if
        enabled), or None if no frame has arrived yet. Cheap: one copy +
        colour convert on the caller's thread, matching Clip.read()."""
        with self._lock:
            frame = self._latest
        if frame is None:
            return None
        if self.mirror:
            frame = cv2.flip(frame, 1)   # selfie-natural left/right mirror
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def toggle_mirror(self):
        self.mirror = not self.mirror
        return self.mirror

    def is_live(self):
        return (self._thread is not None and self._thread.is_alive()
                and self._cap is not None)

    def release(self):
        """Stop the grab thread and release the device."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._latest = None
        self.status = "idle"


def probe_cameras(max_index=6, request_size=(1280, 720), fps=30):
    """Return a list of (index, "WxH") for every working capture device.
    Used by list_cameras.py so the operator can see what's detected
    without touching a terminal. Releases everything it opens."""
    found = []
    for idx in range(max_index):
        cap = CameraSource._open_index(idx, request_size[0], request_size[1], fps)
        if cap is not None:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            found.append((idx, f"{w}x{h}"))
            cap.release()
    return found
