"""Hardware-HEVC clip pool — drop-in for ClipPool, backed by the out-of-process
gl decode worker (hevc_decode_worker.py).

Each clip gets its own worker process (HW HEVC decode + GPU detile/convert),
frames shared through /dev/shm with a 3-slot prefetch so the worker decodes the
next frame while the engine composites the current one. GL stays out of the main
pygame process (V3D dual-context rule), exactly like the GPU generators.

Only the decoder factory differs from ClipPool — all selection / LRU / naming /
mapping logic is inherited. Clips MUST be baked to the canvas size (2048x1152),
the one geometry where the gl path negotiates the Pi's tiled HEVC format; the
inherited read_at() then does no runtime resize.
"""
import gc
import mmap
import os
import subprocess
from pathlib import Path

import numpy as np

from clips import ClipPool

HERE = Path(__file__).resolve().parent
WORKER = str(HERE / "hevc_decode_worker.py")
SYS_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
SLOTS = 3            # ring depth; returned frame stays valid for SLOTS-1 reads
FMT = "RGB"          # matches the engine's RGB clip frames (Clip.read cvtColors)
BPP = 3


def _probe_dims(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        stderr=subprocess.DEVNULL).decode().strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


class HevcClip:
    """Mirrors clips.Clip (.read() -> RGB ndarray, .release()) but decodes via
    the hardware HEVC worker. Degrades to a black frame if the worker can't
    start, so a bad clip never crashes the engine."""

    def __init__(self, path):
        self.path = Path(path)
        self.fps = 30
        self.dead = False
        self._last = None
        self.proc = None
        self.mm = None
        self.fd = None
        self.shm_path = None
        try:
            self.w, self.h = _probe_dims(path)
            self.fb = self.w * self.h * BPP
            self.shm_path = f"/dev/shm/vj_hevc_{os.getpid()}_{id(self)}"
            self.fd = os.open(self.shm_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.ftruncate(self.fd, SLOTS * self.fb)
            self.mm = mmap.mmap(self.fd, SLOTS * self.fb)
            self.views = [
                np.frombuffer(self.mm, np.uint8, self.fb, s * self.fb)
                .reshape(self.h, self.w, 3) for s in range(SLOTS)]
            self.proc = subprocess.Popen(
                [SYS_PY, WORKER, str(path), self.shm_path,
                 str(self.w), str(self.h), str(SLOTS), FMT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
            self._seq = 0
            self._pending = 0
            self._req(0)              # one frame in flight; first read() collects it
        except Exception as exc:       # noqa: BLE001
            print(f"[vj] HevcClip failed to start {self.path.name}: {exc!r}")
            self.dead = True
            self.w = getattr(self, "w", 0)
            self.h = getattr(self, "h", 0)

    def _req(self, slot):
        try:
            self.proc.stdin.write(f"{slot}\n".encode())
            self.proc.stdin.flush()
        except Exception:
            self.dead = True

    def _wait(self):
        try:
            line = self.proc.stdout.readline()
            return bool(line) and line.strip() == b"1"
        except Exception:
            return False

    def _black(self):
        if self.w and self.h:
            return np.zeros((self.h, self.w, 3), np.uint8)
        return None

    def read(self):
        if self.dead:
            return self._last if self._last is not None else self._black()
        if not self._wait():           # the in-flight frame failed → give up
            self.dead = True
            return self._last if self._last is not None else self._black()
        done = self._pending
        self._seq = (self._seq + 1) % SLOTS
        self._req(self._seq)           # prefetch next while engine uses this one
        self._pending = self._seq
        self._last = self.views[done]
        return self._last

    def release(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.views = None
        self._last = None
        gc.collect()
        try:
            self.mm.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass
        try:
            os.unlink(self.shm_path)
        except OSError:
            pass


class HevcClipPool(ClipPool):
    # Each worker holds a GL context + decoder + ISP + ~18 MB shm, so keep the
    # open set small (vs cv2's 12). Plenty for the active clip + a few mapping
    # groups; spawning a worker on clip-switch costs ~1s (GStreamer init).
    def __init__(self, directory, target_size, max_open=4):
        super().__init__(directory, target_size, max_open=max_open)

    def _make_clip(self, path):
        return HevcClip(path)
