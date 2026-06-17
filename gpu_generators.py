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
import select
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

import numpy as np

from shader_catalog import GPU_GENERATORS
from projectm_presets import PROJECTM_GENERATORS


HERE = Path(__file__).resolve().parent
WORKER = HERE / "gpu_generator_worker.py"
PM_WORKER = HERE / "projectm_worker.py"

# Every "pm:*" preset shares ONE projectM worker (keyed PM_KEY). That worker
# owns one EGL context and keeps an LRU pool of warm projectM instances, one
# per active preset, so mapping can show several presets without reloading
# shaders every frame.
PM_KEY = "projectm"


def _worker_key(name):
    return PM_KEY if name.startswith("pm:") else name


def _write_request(proc, name, width, height, token, params):
    px, py = params
    req = json.dumps({
        "name": name, "width": int(width), "height": int(height),
        "token": int(token), "param_x": float(px), "param_y": float(py),
    }, separators=(",", ":"))
    proc.stdin.write((req + "\n").encode("utf-8"))
    proc.stdin.flush()


def _readable(stream):
    """True if `stream` has bytes ready to read right now (non-blocking poll).
    Lets the bridge skip a blocking read on a worker that hasn't finished the
    current frame yet — the difference between a generator that's one frame
    late and a multi-second compositor freeze waiting on a cold worker."""
    try:
        return bool(select.select([stream], [], [], 0)[0])
    except (OSError, ValueError):
        return False


def _read_response(proc):
    """Read one worker response. Returns the np frame, or None if the worker
    reported failure for that request (a header with no payload)."""
    header = proc.stdout.readline()
    if not header:
        raise RuntimeError("worker exited")
    msg = json.loads(header.decode("utf-8"))
    if not msg.get("ok"):
        return None
    n = int(msg["n"])
    payload = proc.stdout.read(n)
    if len(payload) != n:
        raise RuntimeError("short frame from worker")
    return np.frombuffer(payload, dtype=np.uint8).reshape(
        (int(msg["height"]), int(msg["width"]), 3))

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


class PmStreamWorker:
    """Drives the single projectM worker from a BACKGROUND THREAD so several
    pm:* presets can be mixed at once (mapping boxes) without blocking the
    compositor. The thread owns the pipe exclusively and renders every
    on-screen preset round-robin into a cache; the main thread only ever
    touches lock-guarded dicts (request the latest size, grab the latest
    frame), so it never waits on the GL round-trip.

    A single worker process / one EGL context still holds all the presets (its
    instance pool keeps them compiled), so the V3D one-context-per-process rule
    is unchanged. Rendering is paced to one shared VJ_PM_STREAM_FPS budget
    (default 30 total frames/sec across all visible pm boxes) so adding mapping
    boxes divides the budget instead of multiplying GPU pressure."""

    def __init__(self, spawn_fn):
        self._spawn = spawn_fn
        self._lock = threading.Lock()
        self._desired = {}        # name -> (w, h, token, params, last_request_ts)
        self._frames = {}         # name -> latest np frame
        self._last_any = None     # newest frame of any preset (switch fallback)
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._failed_until = 0.0  # cooldown after a worker death
        try:
            fps = max(1, int(os.environ.get("VJ_PM_STREAM_FPS", "30")))
        except ValueError:
            fps = 30
        self._target_fps = fps
        self._min_interval = 1.0 / fps
        self.EXPIRE_S = 0.5       # drop presets not requested for this long
        # After this long with no preset on screen, fully shut the worker
        # process down (releasing its EGL context, memory, and the always-on
        # audio mic-capture thread) so a plain clip gets all the cycles. It
        # respawns on the next pm:* request. Generous enough that brief clip
        # interludes between presets don't thrash respawns.
        try:
            self.IDLE_SHUTDOWN_S = max(1.0, float(
                os.environ.get("VJ_PM_IDLE_S", "6.0")))
        except ValueError:
            self.IDLE_SHUTDOWN_S = 6.0
        self._render_ts = deque(maxlen=48)   # completed-render timestamps

    def active_streams(self):
        with self._lock:
            return len(self._desired)

    def stream_fps(self):
        """(per_box_fps, active_streams) — the HONEST refresh rate of the
        projectM boxes, which is the worker's render throughput divided across
        the on-screen presets. The compositor fps is separate and higher."""
        with self._lock:
            ts = list(self._render_ts)
            active = len(self._desired)
        # No presets on screen -> report honest zero so the HUD stops showing
        # a phantom "pm 0fps×1" while nothing is actually rendering.
        if active == 0 or len(ts) < 2 or ts[-1] <= ts[0]:
            return (0.0, active)
        throughput = (len(ts) - 1) / (ts[-1] - ts[0])
        return (throughput / active, active)

    def request(self, name, width, height, token, params):
        now = time.time()
        with self._lock:
            if now < self._failed_until:
                return None
            self._desired[name] = (int(width), int(height), int(token),
                                   params, now)
            frame = self._frames.get(name)
            if frame is None:
                frame = self._last_any
        self._ensure_thread()
        return None if frame is None else frame.copy()

    def pause(self):
        # Blackout/freeze: stop rendering so the thread idles (no GPU churn).
        with self._lock:
            self._desired.clear()

    def cooldown(self, seconds=8.0, reason=""):
        """Drop projectM load for a short window.

        Used by the compositor safety breaker when SDL present starts blocking
        for hundreds of ms. Killing the worker releases the EGL context and
        stops the readback loop; future pm requests return None until the
        cooldown expires, then the worker respawns lazily.
        """
        try:
            seconds = max(0.5, float(seconds))
        except (TypeError, ValueError):
            seconds = 8.0
        until = time.time() + seconds
        with self._lock:
            self._failed_until = max(self._failed_until, until)
            self._desired.clear()
            self._frames.clear()
            self._last_any = None
            self._render_ts.clear()
        suffix = f" ({reason})" if reason else ""
        print(f"[vj] projectM stream cooling down {seconds:.0f}s{suffix}",
              flush=True)
        self._stop.set()
        self._kill_proc()
        t = self._thread
        if t is not None:
            t.join(timeout=0.5)
            if not t.is_alive():
                self._thread = None
                self._stop.clear()

    def shutdown(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=1.5)
        self._kill_proc()

    def _ensure_thread(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pm-stream",
                                        daemon=True)
        self._thread.start()

    def _kill_proc(self):
        proc, self._proc = self._proc, None
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

    def _run(self):
        try:
            self._proc = self._spawn()
        except Exception as exc:
            print(f"[vj] projectM stream: could not start worker: {exc!r}")
            with self._lock:
                self._failed_until = time.time() + RESPAWN_COOLDOWN_S
            return
        print(f"[vj] projectM stream: budget {self._target_fps}fps total",
              flush=True)
        proc = self._proc
        last_render = {}
        last_active = time.time()
        last_global_render = 0.0
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                snapshot = [(n, v) for n, v in self._desired.items()
                            if now - v[4] < self.EXPIRE_S]
                if len(snapshot) != len(self._desired):
                    self._desired = {n: v for n, v in snapshot}
            if not snapshot:
                # Nothing on screen. After a grace period, fully release the
                # worker (EGL context, memory, audio thread) so clips get the
                # GPU; the thread exits and request() respawns it on demand.
                if now - last_active >= self.IDLE_SHUTDOWN_S:
                    print("[vj] projectM idle %.0fs — releasing worker to free "
                          "resources (respawns on next pm preset)"
                          % self.IDLE_SHUTDOWN_S)
                    with self._lock:
                        self._frames.clear()
                        self._last_any = None
                        self._render_ts.clear()
                    self._kill_proc()
                    return
                time.sleep(0.01)
                continue
            last_active = now
            wait = self._min_interval - (now - last_global_render)
            if wait > 0:
                time.sleep(min(0.01, wait))
                continue
            # Fairness: render the visible preset that has waited longest.
            # New presets have a zero timestamp, so they fill in promptly,
            # then steady state becomes round-robin.
            name, (w, h, token, params, _ts) = min(
                snapshot, key=lambda item: last_render.get(item[0], 0.0))
            try:
                _write_request(proc, name, w, h, token, params)
                frame = _read_response(proc)
            except Exception as exc:
                print(f"[vj] projectM stream worker died: {exc!r}; "
                      f"cooling down")
                with self._lock:
                    self._failed_until = max(
                        self._failed_until, time.time() + RESPAWN_COOLDOWN_S)
                    self._desired.clear()
                    self._frames.clear()
                    self._last_any = None
                    self._render_ts.clear()
                self._kill_proc()
                return
            done = time.time()
            last_render[name] = done
            last_global_render = done
            if frame is not None:
                with self._lock:
                    self._frames[name] = frame
                    self._last_any = frame
                    self._render_ts.append(done)


class GpuGeneratorBridge:
    def __init__(self):
        self.workers = OrderedDict()   # name -> Popen ; ordered for LRU
        self._cooldown = {}            # name -> time before which not to respawn
        self._paused = set()           # names of workers currently PAUSED
        self._rendered = set()         # names render()'d since the last pause_idle
        # 1-deep render pipeline: per worker, a FIFO of sent-but-unread requests
        # and the latest decoded frame per generator name. render() reads the
        # PREVIOUS frame's response (ready by now) and returns the cached frame
        # instead of blocking on the worker — taking the GL round-trip off the
        # compositor's critical path (Phase-1 I/O is serial on the main thread).
        self._inflight = {}            # key -> deque[(name, w, h)] sent, unread
        self._last_frame = {}          # (key, name) -> latest np frame
        self._last_by_key = {}         # key -> last frame (any name) for switch fallback
        # All pm:* presets are served by a background-threaded stream worker so
        # several can be mixed at once without blocking the compositor.
        self._pm = None                # lazily created PmStreamWorker
        self.disabled = (os.environ.get("VJ_NO_GPU_GENERATORS") == "1"
                         or MAX_WORKERS < 1)
        self.failed = False

    def available(self, name):
        return (not self.disabled and not self.failed
                and (name in GPU_GENERATORS or name in PROJECTM_GENERATORS))

    def pm_stream_fps(self):
        """(per_box_fps, active_streams) for the projectM boxes, or (0, 0)."""
        return self._pm.stream_fps() if self._pm is not None else (0.0, 0)

    def pm_active_streams(self):
        """Cheap count of on-screen projectM presets (0 if none / no worker)."""
        return self._pm.active_streams() if self._pm is not None else 0

    def relieve_pm(self, seconds=8.0, reason=""):
        """Temporarily stop projectM rendering to let the display recover."""
        if self._pm is not None:
            self._pm.cooldown(seconds, reason)

    def render(self, name, width, height, token=0, params=(0.5, 0.5)):
        if not self.available(name):
            return None
        key = _worker_key(name)
        if key == PM_KEY:
            # projectM: hand off to the background stream (non-blocking).
            if self._pm is None:
                self._pm = PmStreamWorker(lambda: self._spawn(PM_KEY))
            return self._pm.request(name, width, height, token, params)
        proc = self._worker_for(key)
        if proc is None:
            return None
        try:
            dq = self._inflight.get(key)
            if dq is None:
                dq = self._inflight[key] = deque()
            # 1-deep pipeline. In steady state last frame's request finished
            # while the compositor did its other work, so draining it is free.
            # But a COLD worker (still importing GStreamer / compiling its
            # shader on a fresh switch) or a slow frame hasn't responded yet —
            # and reading then would BLOCK the whole compositor for the
            # worker's entire startup (the generator-switch freeze). So only
            # drain when the pipe actually has a response waiting; otherwise
            # leave the request in flight and return the cached frame. The wall
            # keeps running and the new generator just fades in a frame or two
            # late instead of freezing everything.
            if dq and _readable(proc.stdout):
                pname = dq.popleft()[0]
                frame = _read_response(proc)
                if frame is not None:
                    self._last_frame[(key, pname)] = frame
                    self._last_by_key[key] = frame
            # Issue a new request only when nothing is still in flight, so the
            # pipeline stays exactly 1-deep even on the frames we skipped the
            # drain (otherwise requests would pile up and the next read would
            # pair a frame with the wrong generator name).
            if not dq:
                _write_request(proc, name, width, height, token, params)
                dq.append((name, width, height))
            self._rendered.add(key)      # on-screen this frame
            self._paused.discard(key)    # being requested means it's PLAYING
            # Prefer this name's own frame; on a fresh switch fall back to the
            # worker's previous frame for one frame rather than flashing black.
            cached = self._last_frame.get((key, name))
            if cached is None:
                cached = self._last_by_key.get(key)
            return None if cached is None else cached.copy()
        except Exception as exc:
            # Retire just this generator's worker (others keep running) and
            # cool it down so we don't respawn-thrash a broken one.
            print(f"[vj] GPU worker '{name}' failed: {exc!r}; retiring it")
            self._kill(key)
            self._cooldown[key] = time.time() + RESPAWN_COOLDOWN_S
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
        if self._pm is not None:
            self._pm.pause()

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
        if self._pm is not None:
            self._pm.shutdown()
            self._pm = None

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
            proc = self._spawn(name)
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
            # Drain any pipelined render responses first, or the pause ack
            # would read a leftover frame instead (pipe desync).
            dq = self._inflight.get(name)
            while dq:
                pname = dq.popleft()[0]
                frame = _read_response(proc)
                if frame is not None:
                    self._last_frame[(name, pname)] = frame
                    self._last_by_key[name] = frame
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
        self._inflight.pop(name, None)
        self._last_by_key.pop(name, None)
        for k in [k for k in self._last_frame if k[0] == name]:
            self._last_frame.pop(k, None)
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

    def _spawn(self, key):
        script = PM_WORKER if key == PM_KEY else WORKER
        candidates = ["/usr/bin/python3", sys.executable]
        last_exc = None
        for exe in candidates:
            try:
                env = os.environ.copy()
                env.setdefault("GST_GL_PLATFORM", "egl")
                env.setdefault("GST_GL_WINDOW", "surfaceless")
                return subprocess.Popen(
                    [exe, str(script)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    cwd=str(HERE),
                    env=env,
                )
            except OSError as exc:
                last_exc = exc
        raise RuntimeError(f"could not start GPU worker: {last_exc!r}")
