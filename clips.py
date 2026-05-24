from pathlib import Path
import cv2


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
    """Sorted list of MP4s in a directory, indexed 0..N. Lazy-loaded."""

    def __init__(self, directory, target_size):
        self.target_w, self.target_h = target_size
        self.paths = sorted(Path(directory).glob("*.mp4")) + sorted(Path(directory).glob("*.mov"))
        self.clips = [None] * len(self.paths)
        self.active_idx = None

    def __len__(self):
        return len(self.paths)

    def name(self, idx):
        if 0 <= idx < len(self.paths):
            return self.paths[idx].stem
        return None

    def select(self, idx):
        if not 0 <= idx < len(self.paths):
            return
        if self.clips[idx] is None:
            self.clips[idx] = Clip(self.paths[idx])
        self.active_idx = idx

    def deselect(self):
        self.active_idx = None

    def read(self):
        if self.active_idx is None:
            return None
        frame = self.clips[self.active_idx].read()
        if frame is None:
            return None
        if frame.shape[1] != self.target_w or frame.shape[0] != self.target_h:
            frame = cv2.resize(frame, (self.target_w, self.target_h))
        return frame

    def release_all(self):
        for c in self.clips:
            if c is not None:
                c.release()
