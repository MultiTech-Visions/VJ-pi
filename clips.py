import random
from pathlib import Path
import cv2


# How many OpenCV VideoCapture handles to keep open per pool. Each one
# pins decoder state + a few MB of buffers; an LRU avoids burning a few
# GB on a 200-clip library while the operator scrubs through.
MAX_OPEN = 12

VIDEO_EXTS = (".mp4", ".mov")


def _discover(root):
    """Recursively find clips under `root`, so the operator can foldery the
    library however makes sense. Skips `_originals` (the pre-process backup)
    and any hidden / underscore-prefixed directory. Sorted folder-first then
    by name so the picker groups each folder into one contiguous run, with
    top-level clips first."""
    root = Path(root)
    out = []
    for p in root.rglob("*"):
        if p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
            continue
        rel = p.relative_to(root)
        # Drop anything inside a backup/hidden/underscore dir at any depth.
        if any(part == "_originals" or part.startswith((".", "_"))
               for part in rel.parts[:-1]):
            continue
        out.append(p)
    # Sort key: top-level (depth 0) before foldered, then by folder path,
    # then by filename — all case-insensitive for a natural-looking list.
    def _key(p):
        rel = p.relative_to(root)
        depth = len(rel.parts) - 1
        folder = rel.parent.as_posix().lower()
        return (1 if depth else 0, folder, rel.name.lower())
    return sorted(out, key=_key)


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
        self.root = Path(directory)
        self.paths = _discover(self.root)
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

    def _make_clip(self, path):
        """Factory for the decoder backing one clip. Subclasses override this
        to swap the decode path (e.g. HevcClipPool → hardware HEVC worker)
        while reusing all the selection / LRU / naming logic here."""
        return Clip(path)

    def name(self, idx):
        if idx is None or not 0 <= idx < len(self.paths):
            return None
        return self.paths[idx].stem

    def rel_dir(self, idx):
        """Folder this clip lives in, relative to the pool root, as a
        forward-slash string. Top-level clips return "" (no folder)."""
        if not 0 <= idx < len(self.paths):
            return ""
        try:
            rel = self.paths[idx].relative_to(self.root).parent
        except ValueError:
            return ""
        s = rel.as_posix()
        return "" if s == "." else s

    def grouped(self):
        """Ordered grouping of clip indices by folder, for the picker tree:
        [(folder_label, [idx, ...]), ...]. The "" label holds top-level
        (unfoldered) clips and always sorts first. Relies on `paths` being
        pre-sorted folder-then-name so each group is a contiguous run."""
        groups = []
        pos = {}
        for i in range(len(self.paths)):
            label = self.rel_dir(i)
            if label not in pos:
                pos[label] = len(groups)
                groups.append((label, []))
            groups[pos[label]][1].append(i)
        return groups

    def find_by_stem(self, stem):
        """Return the index of the clip whose filename stem matches, or None.
        Stems stay bare (no folder) so favourites/mapping refs survive a clip
        being moved into a subfolder — keep filenames unique across folders."""
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
            self.clips[idx] = self._make_clip(self.paths[idx])
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
        return self.read_at(self.active_idx)

    def ensure_open(self, idx):
        """Open the clip at `idx` if not already, and refresh its LRU
        position. Use this when multiple subscribers (e.g. mapping groups)
        want to keep a clip alive without taking over `active_idx`."""
        if not 0 <= idx < len(self.paths):
            return
        if self.clips[idx] is None:
            self.clips[idx] = self._make_clip(self.paths[idx])
        self._touch_lru(idx)
        self._evict_lru(protect=idx)

    def read_at(self, idx):
        """Read one frame from clip `idx` without changing `active_idx`.
        Opens the clip lazily if it was LRU-evicted. Returns None if the
        index is invalid or the clip can't be read."""
        if not 0 <= idx < len(self.paths):
            return None
        clip = self.clips[idx]
        if clip is None:
            clip = self._make_clip(self.paths[idx])
            self.clips[idx] = clip
            self._touch_lru(idx)
            self._evict_lru(protect=idx)
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
