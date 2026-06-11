"""Client for the out-of-process GStreamer/GL generator workers.

We run a small POOL of worker processes — one per generator name —
rather than a single shared worker. The old single worker rebuilt its
entire GStreamer/GL pipeline whenever the requested generator changed.
In a multi-generator projection-mapping scene that meant several
pipeline rebuilds *per frame*; the state transitions are mostly waiting,
so the main loop stalled to single-digit fps while CPU and GPU sat low.

With one persistent worker per generator, each pipeline is built once
and reused — nothing rebuilds frame to frame. Each worker still owns
exactly ONE GL/EGL context in its OWN process, so the V3D dual-context
state leak the architecture exists to avoid still cannot occur; we simply
run a few isolated single-context processes instead of one. The pool is
LRU-capped so memory and idle GPU stay bounded — the least-recently-used
worker is retired when the cap is exceeded (and respawned on demand if
its generator returns).
"""
import json
import os
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

from shader_catalog import GPU_GENERATORS


HERE = Path(__file__).resolve().parent
WORKER = HERE / "gpu_generator_worker.py"

# Upper bound on live worker processes. Each holds a GStreamer/GL pipeline
# (~tens of MB) and runs its shader continuously, so this caps both memory
# and idle GPU. Only generators actually on screen are ever spawned; busy
# mapping scenes rarely exceed a handful of distinct generators.
#
# Tunable at launch via VJ_GPU_MAX_WORKERS so the V3D multi-context limit
# can be probed without a code change:
#   VJ_GPU_MAX_WORKERS=1  → effectively the old single-worker behaviour
#                           (one worker at a time; switching generators
#                           retires + respawns rather than rebuilds)
#   VJ_GPU_MAX_WORKERS=0  → disable GPU generators entirely (CPU fallback)
def _max_workers():
    try:
        return max(0, int(os.environ.get("VJ_GPU_MAX_WORKERS", "8")))
    except ValueError:
        return 8

MAX_WORKERS = _max_workers()

# After a worker dies mid-render, wait this long before respawning it, so a
# persistently broken generator can't respawn-thrash every frame.
RESPAWN_COOLDOWN_S = 3.0


class GpuGeneratorBridge:
    def __init__(self):
        self.workers = OrderedDict()   # name -> Popen ; ordered for LRU
        self._cooldown = {}            # name -> time before which not to respawn
        self._paused = set()           # names of workers currently PAUSED
        self._rendered = set()         # names render()'d since the last pause_idle
        self.disabled = (os.environ.get("VJ_NO_GPU_GENERATORS") == "1"
                         or MAX_WORKERS < 1)
        self.failed = False

    def available(self, name):
        return not self.disabled and not self.failed and name in GPU_GENERATORS

    def render(self, name, width, height, token=0, params=(0.5, 0.5)):
        if not self.available(name):
            return None
        proc = self._worker_for(name)
        if proc is None:
            return None
        try:
            px, py = params
            req = json.dumps({
                "name": name,
                "width": int(width),
                "height": int(height),
                "token": int(token),
                "param_x": float(px),
                "param_y": float(py),
            }, separators=(",", ":"))
            proc.stdin.write((req + "\n").encode("utf-8"))
            proc.stdin.flush()
            header = proc.stdout.readline()
            if not header:
                raise RuntimeError("worker exited")
            msg = json.loads(header.decode("utf-8"))
            if not msg.get("ok"):
                print(f"[vj] GPU generator '{name}' unavailable: "
                      f"{msg.get('error', 'worker error')}")
                return None
            n = int(msg["n"])
            payload = proc.stdout.read(n)
            if len(payload) != n:
                raise RuntimeError("short frame from worker")
            frame = np.frombuffer(payload, dtype=np.uint8)
            self._rendered.add(name)     # on-screen this frame
            self._paused.discard(name)   # a successful render means it's PLAYING
            return frame.reshape((int(msg["height"]), int(msg["width"]), 3)).copy()
        except Exception as exc:
            # Retire just this generator's worker (others keep running) and
            # cool it down so we don't respawn-thrash a broken one.
            print(f"[vj] GPU worker '{name}' failed: {exc!r}; retiring it")
            self._kill(name)
            self._cooldown[name] = time.time() + RESPAWN_COOLDOWN_S
            return None

    def pause(self):
        """Pause every live worker's pipeline (blackout / freeze) so they
        stop churning the GPU while nothing is shown. render() resumes a
        worker on its next request."""
        if self.disabled or self.failed:
            return
        for name in list(self.workers.keys()):
            self._pause_one(name)
            self._paused.add(name)

    def pause_idle(self):
        """Pause every live worker that wasn't render()'d since the last call
        — i.e. generators that just went off-screen. V3D is shared, so a few
        off-screen GL pipelines all running at 30fps starve whatever IS on
        screen (a lone donut dropping to 9fps because 7 cycled-past generators
        are still churning). This is edge-triggered: a worker is paused only
        on the transition to idle (tracked in _paused) and resumes by itself
        on its next render(), so steady state is just a couple of set checks.
        Call once per composed frame."""
        if not self.disabled and not self.failed:
            for name in list(self.workers.keys()):
                if name not in self._rendered and name not in self._paused:
                    self._pause_one(name)
                    self._paused.add(name)
        self._rendered.clear()

    def shutdown(self):
        for name in list(self.workers.keys()):
            self._kill(name)
        self.workers.clear()
        self._paused.clear()

    # ── internals ──────────────────────────────────────────────────

    def _worker_for(self, name):
        """Return a live worker process for `name`, spawning one if needed.
        None if cooling down after a failure, or if the worker can't start."""
        if self._cooldown.get(name, 0.0) > time.time():
            return None
        proc = self.workers.get(name)
        if proc is not None and proc.poll() is None:
            self.workers.move_to_end(name)     # mark most-recently-used
            return proc
        self.workers.pop(name, None)
        try:
            proc = self._spawn()
        except Exception as exc:
            print(f"[vj] could not start GPU worker: {exc!r}; "
                  f"disabling GPU generators")
            self.failed = True
            self.shutdown()
            return None
        self.workers[name] = proc
        self.workers.move_to_end(name)
        self._cooldown.pop(name, None)
        self._enforce_cap()
        return proc

    def _enforce_cap(self):
        while len(self.workers) > MAX_WORKERS:
            old_name = next(iter(self.workers))   # least-recently-used
            self._kill(old_name)

    def _pause_one(self, name):
        proc = self.workers.get(name)
        if proc is None or proc.poll() is not None:
            return
        try:
            req = json.dumps({"cmd": "pause"}, separators=(",", ":"))
            proc.stdin.write((req + "\n").encode("utf-8"))
            proc.stdin.flush()
            if not proc.stdout.readline():
                raise RuntimeError("worker exited during pause")
        except Exception as exc:
            print(f"[vj] GPU worker '{name}' pause failed: {exc!r}")
            self._kill(name)

    def _kill(self, name):
        self._paused.discard(name)
        proc = self.workers.pop(name, None)
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

    def _spawn(self):
        candidates = ["/usr/bin/python3", sys.executable]
        last_exc = None
        for exe in candidates:
            try:
                env = os.environ.copy()
                env.setdefault("GST_GL_PLATFORM", "egl")
                env.setdefault("GST_GL_WINDOW", "surfaceless")
                return subprocess.Popen(
                    [exe, str(WORKER)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    cwd=str(HERE),
                    env=env,
                )
            except OSError as exc:
                last_exc = exc
        raise RuntimeError(f"could not start GPU worker: {last_exc!r}")
