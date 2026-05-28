"""Client for the out-of-process GStreamer/GL generator worker."""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from shader_catalog import GPU_GENERATORS


HERE = Path(__file__).resolve().parent
WORKER = HERE / "gpu_generator_worker.py"


class GpuGeneratorBridge:
    def __init__(self):
        self.proc = None
        self.disabled = os.environ.get("VJ_NO_GPU_GENERATORS") == "1"
        self.failed = False

    def available(self, name):
        return not self.disabled and not self.failed and name in GPU_GENERATORS

    def render(self, name, width, height, token=0):
        if not self.available(name):
            return None
        try:
            self._start()
            req = json.dumps({
                "name": name,
                "width": int(width),
                "height": int(height),
                "token": int(token),
            }, separators=(",", ":"))
            self.proc.stdin.write((req + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            header = self.proc.stdout.readline()
            if not header:
                raise RuntimeError("GPU worker exited")
            msg = json.loads(header.decode("utf-8"))
            if not msg.get("ok"):
                print(f"[vj] GPU generator unavailable: {msg.get('error', 'worker error')}")
                return None
            n = int(msg["n"])
            payload = self.proc.stdout.read(n)
            if len(payload) != n:
                raise RuntimeError("short frame from GPU worker")
            frame = np.frombuffer(payload, dtype=np.uint8)
            return frame.reshape((int(msg["height"]), int(msg["width"]), 3)).copy()
        except Exception as exc:
            print(f"[vj] GPU generator disabled after failure: {exc!r}")
            self.failed = True
            self.shutdown()
            return None

    def shutdown(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        candidates = ["/usr/bin/python3", sys.executable]
        last_exc = None
        for exe in candidates:
            try:
                env = os.environ.copy()
                env.setdefault("GST_GL_PLATFORM", "egl")
                env.setdefault("GST_GL_WINDOW", "surfaceless")
                self.proc = subprocess.Popen(
                    [exe, str(WORKER)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    cwd=str(HERE),
                    env=env,
                )
                return
            except OSError as exc:
                last_exc = exc
        raise RuntimeError(f"could not start GPU worker: {last_exc!r}")
