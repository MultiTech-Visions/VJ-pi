"""
Optional BLE LED output for the VJ rig — drives the physical "mushroom" prop's
Magic Lantern controller so it mirrors the show.

Design goals (why this looks the way it does):
  * It must NEVER affect the render loop. All BLE I/O runs on a private
    background thread with its own asyncio loop; the render thread only drops a
    colour into a lock-guarded slot and returns immediately.
  * It must be a good BLE citizen: the controller allows only ONE central at a
    time, so we don't grab the link until the operator actually turns mushroom
    control on. Until then the phone app / led_tester are free to use it.
  * It must survive the light being off / out of range. Connect failures and
    dropped links are retried with backoff; nothing here ever raises into the
    caller. If `bleak` isn't installed, the whole feature no-ops.

Public API (all thread-safe, all non-blocking — call from the render loop):
    light = MushroomLight(ADDRESS)   # constructs; does NOT connect yet
    light.set_color(r, g, b)         # arm + track this colour ((0,0,0) = off)
    light.set_idle()                 # release control -> run the built-in effect
    light.shutdown()                 # leave on the built-in effect, disconnect

The frame format matches led_tester.py / the documented Magic Lantern protocol
(service FFF0, write char FFF3, write-without-response, 0x7e..0xef frames).
"""

from __future__ import annotations

import asyncio
import threading
import time

try:
    from bleak import BleakClient, BleakScanner
    _HAVE_BLEAK = True
except Exception:                       # bleak missing / import error
    _HAVE_BLEAK = False

WRITE_CHAR_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"


def _frame_color(r, g, b):
    return bytes([0x7E, 0x07, 0x05, 0x03, r & 0xFF, g & 0xFF, b & 0xFF, 0x10, 0xEF])


def _frame_off():
    return bytes([0x7E, 0x04, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])


def _frame_effect(n):
    return bytes([0x7E, 0x05, 0x03, n & 0xFF, 0x06, 0xFF, 0xFF, 0x00, 0xEF])


class MushroomLight:
    def __init__(self, address, idle_effect=0x00, send_hz=12, min_delta=6,
                 log=print):
        """
        address     : BLE MAC of the controller.
        idle_effect : built-in effect number to run when control is released
                      (0x00 = AutoPlay, the controller's own cycling show).
        send_hz     : max BLE writes per second.
        min_delta   : minimum per-channel colour change worth sending.
        """
        self.address = address
        self.idle_effect = idle_effect & 0xFF
        self.send_interval = 1.0 / max(1, send_hz)
        self.min_delta = min_delta
        self._log = log

        self._lock = threading.Lock()
        self._want_conn = False          # True once first armed (set_color/idle)
        self._mode = "idle"              # "color" | "idle"
        self._color = (0, 0, 0)
        self._idle_dirty = False         # need to (re)send the idle effect
        self._stop = False
        self._connected = False

        if not _HAVE_BLEAK:
            self._log("[mushroom] bleak not installed — LED output disabled "
                      "(double-click 'Enable Mushroom Light.sh')")
            self._thread = None
            return

        self._thread = threading.Thread(target=self._run, name="mushroom-ble",
                                         daemon=True)
        self._thread.start()

    # ── public, thread-safe, non-blocking ──────────────────────────────
    def available(self):
        return self._thread is not None

    def connected(self):
        with self._lock:
            return self._connected

    def set_color(self, r, g, b):
        with self._lock:
            self._want_conn = True
            self._mode = "color"
            self._color = (int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF)

    def set_idle(self):
        with self._lock:
            self._want_conn = True
            if self._mode != "idle":
                self._mode = "idle"
                self._idle_dirty = True

    def shutdown(self):
        if self._thread is None:
            return
        with self._lock:
            self._stop = True
        self._thread.join(timeout=3.0)

    # ── private: background thread + asyncio worker ─────────────────────
    def _run(self):
        try:
            asyncio.run(self._worker())
        except Exception as e:               # never let the thread crash loudly
            self._log(f"[mushroom] worker stopped: {e!r}")

    async def _try_connect(self):
        try:
            dev = await BleakScanner.find_device_by_address(self.address,
                                                            timeout=8.0)
            if dev is None:
                return None
            client = BleakClient(dev)
            await client.connect()
            has_fff3 = any(
                ch.uuid.lower() == WRITE_CHAR_UUID
                for s in client.services for ch in s.characteristics
            )
            if not has_fff3:
                await client.disconnect()
                self._log("[mushroom] device has no FFF3 — wrong device?")
                return None
            self._log("[mushroom] connected")
            return client
        except Exception:
            return None

    async def _safe_write(self, client, payload):
        await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)

    async def _safe_disconnect(self, client):
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    async def _reset_adapter(self):
        """Power-cycle the BLE adapter to recover a wedged stack.

        The Pi's adapter can stop hearing advertisements after a lot of
        connect/disconnect churn — scans find fewer and fewer devices and the
        light goes missing even though it's powered and in range. A
        'bluetoothctl power off/on' clears it (verified). Best-effort: if it
        lacks permission we just keep retrying the plain connect — no harm."""
        self._log("[mushroom] bluetooth reset (recovering adapter)")
        for args in (["power", "off"], ["power", "on"]):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bluetoothctl", *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception as exc:
                self._log(f"[mushroom] adapter reset unavailable ({exc!r})")
                return
            await asyncio.sleep(2.0)

    async def _worker(self):
        client = None
        backoff = 1.0
        fail_count = 0
        last_send = 0.0
        sent_color = None
        sent_idle = False

        while True:
            with self._lock:
                want = self._want_conn
                stop = self._stop
                mode = self._mode
                color = self._color
                idle_dirty = self._idle_dirty
                self._idle_dirty = False

            # Shutdown: drop to the built-in effect, then disconnect and exit.
            if stop:
                if client is not None and client.is_connected:
                    try:
                        await self._safe_write(client,
                                               _frame_effect(self.idle_effect))
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass
                await self._safe_disconnect(client)
                return

            # Not armed yet — stay off the radio so other centrals can use it.
            if not want:
                await asyncio.sleep(0.1)
                continue

            # (Re)connect as needed, with backoff on failure.
            if client is None or not client.is_connected:
                with self._lock:
                    self._connected = False
                await self._safe_disconnect(client)
                client = await self._try_connect()
                if client is None:
                    fail_count += 1
                    self._log("[mushroom] searching for light…")
                    # Self-heal a wedged adapter after a few misses, then retry.
                    if fail_count % 3 == 0:
                        await self._reset_adapter()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.6, 8.0)
                    continue
                fail_count = 0
                backoff = 1.0
                with self._lock:
                    self._connected = True
                sent_color = None
                sent_idle = False
                if mode == "idle":
                    idle_dirty = True       # set the effect on a fresh link

            now = time.monotonic()
            try:
                if mode == "color":
                    is_off = (color == (0, 0, 0))
                    was_off = (sent_color == (0, 0, 0))
                    big = (sent_color is None or
                           any(abs(a - b) >= self.min_delta
                               for a, b in zip(color, sent_color)))
                    heartbeat = (now - last_send) >= 2.0   # recover lost packets
                    if (big or (is_off != was_off) or heartbeat) and \
                            (now - last_send) >= self.send_interval:
                        await self._safe_write(
                            client, _frame_off() if is_off else _frame_color(*color))
                        sent_color = color
                        sent_idle = False
                        last_send = now
                else:  # idle
                    if idle_dirty or not sent_idle:
                        await self._safe_write(
                            client, _frame_effect(self.idle_effect))
                        sent_idle = True
                        sent_color = None
                        last_send = now
            except Exception:
                self._log("[mushroom] write failed — will reconnect")
                await self._safe_disconnect(client)
                client = None
                with self._lock:
                    self._connected = False
                continue

            await asyncio.sleep(self.send_interval)
