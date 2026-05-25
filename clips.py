import random
from pathlib import Path
import cv2


# How many OpenCV VideoCapture handles to keep open per pool. Each one
# pins decoder state + a few MB of buffers; an LRU avoids burning a few
# GB on a 200-clip library while the operator scrubs through.
MAX_OPEN = 12


class Clip:
    def __init__(self, path):
        self.path = Path(path)
        self.cap = cv2.VideoCapture(str(self.path))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self._last = None

    def read(self):
        ret, frame = self.cap.read()
        if not ret:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
        if ret:
            self._last = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return self._last

    def release(self):
        self.cap.release()


class ClipPool:
    """Sorted list of videos in a directory, indexed 0..N. Lazy-loaded.

    The pool keeps at most MAX_OPEN decoders open at once and LRU-evicts
    older ones as the operator browses through a large library.
    """

    def __init__(self, directory, target_size, max_open=MAX_OPEN):
        self.target_w, self.target_h = target_size
        self.paths = (
            sorted(Path(directory).glob("*.mp4"))
            + sorted(Path(directory).glob("*.mov"))
        )
        self.clips = [None] * len(self.paths)
        self.active_idx = None
        self.max_open = max_open
        self._open_order = []  # least-recent first, most-recent last
        # Track files that need live resizing so we can nudge the
        # operator to re-run Process Assets.sh once per file (saves the
        # per-frame cv2.resize cost on every subsequent play).
        self._warned_about_resize = set()

    def __len__(self):
        return len(self.paths)

    def name(self, idx):
        if idx is None or not 0 <= idx < len(self.paths):
            return None
        return self.paths[idx].stem

    def find_by_stem(self, stem):
        """Return the index of the clip whose filename stem matches, or None."""
        if not stem:
            return None
        for i, p in enumerate(self.paths):
            if p.stem == stem:
                return i
        return None

    # ── Selection ────────────────────────────────────────────────────

    def select(self, idx):
        if not 0 <= idx < len(self.paths):
            return
        if self.clips[idx] is None:
            self.clips[idx] = Clip(self.paths[idx])
            self._touch_lru(idx)
            self._evict_lru(protect=idx)
        else:
            self._touch_lru(idx)
        self.active_idx = idx

    def deselect(self):
        self.active_idx = None

    def step(self, n):
        """Move active_idx by n positions, wrapping around the list."""
        if not self.paths:
            return
        if self.active_idx is None:
            idx = 0 if n >= 0 else len(self.paths) - 1
        else:
            idx = (self.active_idx + n) % len(self.paths)
        self.select(idx)

    def first(self):
        if self.paths:
            self.select(0)

    def last(self):
        if self.paths:
            self.select(len(self.paths) - 1)

    def pick_random(self):
        if self.paths:
            self.select(random.randrange(len(self.paths)))

    # ── LRU bookkeeping ──────────────────────────────────────────────

    def _touch_lru(self, idx):
        if idx in self._open_order:
            self._open_order.remove(idx)
        self._open_order.append(idx)

    def _evict_lru(self, protect):
        # Release oldest open clip(s) until we're back under the cap.
        open_count = sum(1 for c in self.clips if c is not None)
        while open_count > self.max_open and self._open_order:
            victim = self._open_order[0]
            if victim == protect or self.clips[victim] is None:
                # don't evict the just-selected clip; rotate past it
                self._open_order.pop(0)
                if victim != protect:
                    continue
                self._open_order.append(victim)
                break
            self.clips[victim].release()
            self.clips[victim] = None
            self._open_order.pop(0)
            open_count -= 1

    # ── Playback ─────────────────────────────────────────────────────

    def read(self):
        if self.active_idx is None:
            return None
        clip = self.clips[self.active_idx]
        if clip is None:
            # Was evicted between select() and read() — reopen.
            clip = Clip(self.paths[self.active_idx])
            self.clips[self.active_idx] = clip
            self._touch_lru(self.active_idx)
            self._evict_lru(protect=self.active_idx)
        frame = clip.read()
        if frame is None:
            return None
        sh, sw = frame.shape[:2]
        if sw != self.target_w or sh != self.target_h:
            # Anti-aliased area filter when shrinking (e.g. 2K → 720p) for
            # the cleanest possible downsample; bilinear when enlarging.
            interp = (cv2.INTER_AREA
                      if sw >= self.target_w and sh >= self.target_h
                      else cv2.INTER_LINEAR)
            frame = cv2.resize(frame, (self.target_w, self.target_h),
                               interpolation=interp)
            # One-time hint per file. The processor exists exactly so this
            # resize is a no-op at runtime.
            idx = self.active_idx
            if idx is not None and idx not in self._warned_about_resize:
                self._warned_about_resize.add(idx)
                print(f"[vj] live-resizing {self.paths[idx].name} "
                      f"({sw}x{sh} → {self.target_w}x{self.target_h}) "
                      f"— run assets/Process\\ Assets.sh to bake this out")
        return frame

    def release_all(self):
        for c in self.clips:
            if c is not None:
                c.release()
        self.clips = [None] * len(self.paths)
        self._open_order = []
