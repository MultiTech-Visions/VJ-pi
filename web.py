"""Mobile web control panel.

Runs Flask in a daemon thread next to the pygame main loop. Phones on
the same network (LAN today, Pi-AP later) browse to http://<pi-ip>/ and
get a big-button touch UI mirroring the keyboard. Posts to /action are
queued onto the SDL event queue via `pygame.event.post()` (thread-safe in
SDL2 / pygame 2.x) and executed on the main thread the next frame.

Live state streams back to all connected phones over Server-Sent Events
on /events — no websocket library needed, browsers auto-reconnect when
the phone wakes from sleep, and Flask's threaded mode handles each
phone on its own thread so a stalled SSE reader doesn't block /action.

No auth or CSRF: the operator explicitly wanted "overlapping users is
part of the fun." Quit/shutdown is intentionally NOT a registered action
so a phone in the audience can't end the set.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import pygame
from flask import Flask, Response, jsonify, render_template, request

from actions import ACTIONS


# ── Rate limiting ────────────────────────────────────────────────────
#
# Token bucket per source IP. Default: 10 actions/sec burst of 15. The
# render loop drains all queued USEREVENTs every frame so even a flood
# can't block rendering — but we still drop early so the event queue
# doesn't accumulate stale taps.

class TokenBucket:
    def __init__(self, rate: float = 10.0, capacity: float = 15.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# Module-level so the buckets survive across requests; one per phone.
_BUCKETS: dict[str, TokenBucket] = defaultdict(TokenBucket)
_BUCKETS_LOCK = threading.Lock()


def _bucket_for(ip: str) -> TokenBucket:
    with _BUCKETS_LOCK:
        return _BUCKETS[ip]


# ── SSE broadcaster ──────────────────────────────────────────────────
#
# One queue per connected phone. The main loop publishes state diffs
# (~10 Hz, only when something actually changes). Each client's queue is
# bounded so a slow phone can't balloon memory.

class Broadcaster:
    def __init__(self):
        self._subs: list[deque] = []
        self._lock = threading.Lock()

    def subscribe(self) -> deque:
        q: deque = deque(maxlen=32)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: deque) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, msg: dict) -> None:
        payload = json.dumps(msg, separators=(",", ":"))
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.append(payload)


# ── Captive-portal hooks ─────────────────────────────────────────────
#
# Phones probe these URLs to detect if the network has internet. When we
# answer the HTTP detect probe ourselves, the phone pops its captive
# sheet automatically — that's the "auto-open" the operator asked for.
# Works regardless of whether the Pi is an AP (Phase 2) or just on a
# LAN (Phase 1): the iptables redirect is what makes phones HIT these
# URLs in AP mode, but exposing the routes now means Phase 2 is just a
# system-config change with no code edits.

CAPTIVE_PATHS = [
    "/hotspot-detect.html",       # iOS / macOS
    "/library/test/success.html", # iOS variant
    "/generate_204",              # Android, Chrome OS
    "/gen_204",                   # older Android
    "/connecttest.txt",           # Windows
    "/ncsi.txt",                  # Windows NCSI
    "/redirect",                  # Windows
    "/success.txt",               # Firefox
    "/canonical.html",            # Ubuntu
]


# ── App factory ──────────────────────────────────────────────────────

def create_app(engine, broadcaster: Broadcaster) -> Flask:
    here = Path(__file__).parent
    app = Flask(
        __name__,
        template_folder=str(here / "templates"),
        static_folder=str(here / "static"),
    )

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/catalog")
    def catalog():
        return jsonify(engine.catalog())

    @app.route("/state")
    def state():
        return jsonify(engine.snapshot())

    @app.route("/action", methods=["POST"])
    def action():
        ip = request.remote_addr or "?"
        if not _bucket_for(ip).take():
            return jsonify({"error": "rate-limited"}), 429
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        if name not in ACTIONS:
            return jsonify({"error": f"unknown action {name!r}"}), 400
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return jsonify({"error": "args must be an object"}), 400
        try:
            pygame.event.post(pygame.event.Event(
                pygame.USEREVENT, {"action": {"name": name, "args": args}}
            ))
        except pygame.error:
            # Event queue full; rare, would mean the main loop hasn't
            # drained for ~16k events. Surface it instead of swallowing.
            return jsonify({"error": "event queue full"}), 503
        return jsonify({"ok": True})

    @app.route("/events")
    def events():
        # Snapshot the engine once at connect so a fresh phone has full
        # state without waiting for the next change-driven push.
        initial = json.dumps({"type": "state", "state": engine.snapshot()},
                             separators=(",", ":"))
        catalog_msg = json.dumps({"type": "catalog", "catalog": engine.catalog()},
                                 separators=(",", ":"))
        q = broadcaster.subscribe()

        def stream():
            try:
                yield f"data: {catalog_msg}\n\n"
                yield f"data: {initial}\n\n"
                # Keep-alive ping every 15s to keep mobile proxies from
                # closing idle connections.
                last_ping = time.monotonic()
                while True:
                    sent_any = False
                    while q:
                        payload = q.popleft()
                        yield f"data: {payload}\n\n"
                        sent_any = True
                    now = time.monotonic()
                    if not sent_any and now - last_ping > 15.0:
                        yield ": ping\n\n"
                        last_ping = now
                    time.sleep(0.05)
            finally:
                broadcaster.unsubscribe(q)

        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # Captive-portal detect URLs — answer with a 302 to the root so the
    # phone's captive sheet pops the control UI directly.
    def captive_redirect():
        return Response(status=302, headers={"Location": "/"})

    for path in CAPTIVE_PATHS:
        app.add_url_rule(path, f"captive_{path.strip('/').replace('/', '_')}",
                         captive_redirect)

    @app.errorhandler(404)
    def catch_all(_e):
        # Anything else (random captive probes we didn't enumerate, like
        # /fwlink or vendor-specific URLs) also bounces to the root.
        return Response(status=302, headers={"Location": "/"})

    return app


# ── State publisher loop ─────────────────────────────────────────────

class StatePublisher(threading.Thread):
    """Polls engine.snapshot() at 10Hz and publishes a diff when changed.

    Runs in its own thread so the main render loop never spends time
    serialising state. JSON-equality is fine for diffing because every
    field is a primitive / list / dict of the same.
    """
    def __init__(self, engine, broadcaster: Broadcaster, hz: float = 10.0):
        super().__init__(daemon=True, name="vj-state-publisher")
        self.engine = engine
        self.broadcaster = broadcaster
        self.period = 1.0 / hz
        self._last_state_json = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = self.engine.snapshot()
                snap_json = json.dumps(snap, separators=(",", ":"), sort_keys=True)
                if snap_json != self._last_state_json:
                    self._last_state_json = snap_json
                    self.broadcaster.publish({"type": "state", "state": snap})
            except Exception as exc:  # noqa: BLE001
                print(f"[vj.web] state publisher: {exc!r}")
            self._stop.wait(self.period)


# ── Public entry point ───────────────────────────────────────────────

def lan_ip() -> str:
    """Best-effort: the IP a phone on the same network would dial.

    Uses a UDP-connect trick so we get the interface that would route
    out (works without actually sending packets). Falls back to the
    hostname if no interface is up yet.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def start(engine, host: str = "0.0.0.0", port: int = 8080) -> tuple[str, int, Broadcaster]:
    """Launch the web server + state publisher as daemon threads.

    Returns (ip, port, broadcaster) so the HUD can render the URL / QR
    and tests can publish synthetic events later if we want them.
    """
    broadcaster = Broadcaster()
    publisher = StatePublisher(engine, broadcaster)
    app = create_app(engine, broadcaster)

    def serve():
        # Flask's built-in server with threaded=True is good enough for
        # ~20 phones at the rate we're talking. We're not exposing this
        # to the public internet so production WSGI hardening doesn't
        # earn its complexity.
        app.run(host=host, port=port, threaded=True,
                debug=False, use_reloader=False)

    threading.Thread(target=serve, daemon=True, name="vj-web").start()
    publisher.start()
    return lan_ip(), port, broadcaster
