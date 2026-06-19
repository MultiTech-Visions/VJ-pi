#!/usr/bin/env python3
"""Master software-brightness control for the VJ rig's displays.

WHY THIS EXISTS
---------------
The field projector and the portable monitor ignore their own hardware
brightness controls (changing the number does nothing to the bulb / panel).
The Pi's desktop settings have no brightness slider either, because on a
normal monitor that's handled by the display's own electronics. So we dim
in *software*: we ask the compositor to scale the colour ramp it sends to
each output. That genuinely reduces the light the projector/panel emits,
per-display, and it even dims a fullscreen app (the VJ output) because the
compositor applies it at scan-out, above everything.

HOW (the Wayland bit)
---------------------
The operator's Pi runs labwc (Wayland, wlroots-based). wlroots exposes the
``wlr-gamma-control-unstable-v1`` protocol, which lets a client install a
gamma ramp *per output*. The packaged tools (gammastep, wl-gammactl) only
drive ALL outputs together, which isn't what we want — we want one labelled
slider per screen. So this file speaks the Wayland wire protocol directly
over the display socket (no pip dependencies — just the standard library),
binds every ``wl_output``, and writes a brightness-scaled linear ramp to
each. Pure stdlib means nothing new gets bolted onto the proven VJ venv;
this runs on the system python3.

If the session is X11 instead (e.g. the operator switched away from
Wayland), it falls back to ``xrandr --output NAME --brightness``, which does
the same gamma-scaling trick on X.

IMPORTANT BEHAVIOUR
-------------------
* The dimming lasts only while this program is running. Close the window and
  the compositor restores every output to full brightness. That's a safety
  feature: you can never get permanently stuck on a black screen.
* You can dim but not boost past the panel's native output (that's all
  software can do). The slider floors at 10% so you can't black yourself out
  by accident.
* Last-used levels are remembered in ~/.config/vj-brightness.json and
  reapplied next launch.
"""

import json
import os
import socket
import struct
import sys
import threading
import time


# ─────────────────────────────────────────────────────────────────────────
#  Wayland wire-protocol backend (wlr-gamma-control-unstable-v1)
# ─────────────────────────────────────────────────────────────────────────
#
# Wire format reminder (all integers little-endian):
#   message = object_id:u32 | (size<<16 | opcode):u32 | args...
#   size counts the whole message incl. the 8-byte header.
#   uint/int/object/new_id        -> 4 bytes
#   string                        -> length:u32 (incl. trailing NUL) then the
#                                    bytes + NUL, padded up to a 4-byte boundary
#   fd args travel in SCM_RIGHTS ancillary data, NOT in the message body.
#
# Object id 1 is always wl_display. We allocate our own ids from 2 up.

WL_DISPLAY_ID = 1

# Opcodes we send / receive (from the published protocol XML):
WL_DISPLAY_SYNC = 0
WL_DISPLAY_GET_REGISTRY = 1
WL_DISPLAY_ERR_EVENT = 0
WL_DISPLAY_DELETE_ID_EVENT = 1

WL_REGISTRY_BIND = 0
WL_REGISTRY_GLOBAL_EVENT = 0
WL_REGISTRY_GLOBAL_REMOVE_EVENT = 1

WL_CALLBACK_DONE_EVENT = 0

WL_OUTPUT_GEOMETRY_EVENT = 0
WL_OUTPUT_MODE_EVENT = 1
WL_OUTPUT_DONE_EVENT = 2
WL_OUTPUT_SCALE_EVENT = 3
WL_OUTPUT_NAME_EVENT = 4          # wl_output v4+
WL_OUTPUT_DESCRIPTION_EVENT = 5   # wl_output v4+

GAMMA_MANAGER_GET_CONTROL = 0
GAMMA_MANAGER_DESTROY = 1

GAMMA_CONTROL_SET_GAMMA = 0       # takes an fd
GAMMA_CONTROL_DESTROY = 1
GAMMA_CONTROL_SIZE_EVENT = 0
GAMMA_CONTROL_FAILED_EVENT = 1

GAMMA_MANAGER_IFACE = "zwlr_gamma_control_manager_v1"


def _pack_string(s):
    b = s.encode("utf-8") + b"\x00"
    n = len(b)
    pad = (-n) % 4
    return struct.pack("<I", n) + b + (b"\x00" * pad)


def _read_string(body, off):
    (n,) = struct.unpack_from("<I", body, off)
    off += 4
    s = body[off:off + n - 1].decode("utf-8", "replace")
    off += (n + 3) & ~3
    return s, off


class WaylandUnavailable(Exception):
    """Raised when we can't talk Wayland gamma-control on this session."""


class WaylandGamma:
    """A minimal Wayland client that installs per-output gamma ramps."""

    def __init__(self):
        self.sock = None
        self._next_id = 2
        self._id_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._buf = b""

        self.kind = {}              # object_id -> 'registry'|'callback'|'output'|'gamma'
        self.globals = []           # list of (name, interface, version)
        self.callback_done = {}     # callback_id -> bool
        self.outputs = {}           # output_id -> {registry_name, version, name, mode}
        self.gamma = {}             # gamma_id -> {output, size, failed}
        self.manager_id = None
        self._error = None
        self._reader = None
        self._running = False

    # ---- connection -----------------------------------------------------
    def connect(self):
        disp = os.environ.get("WAYLAND_DISPLAY")
        if not disp:
            raise WaylandUnavailable("no WAYLAND_DISPLAY (not a Wayland session)")
        if disp.startswith("/"):
            path = disp
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime:
                raise WaylandUnavailable("no XDG_RUNTIME_DIR set")
            path = os.path.join(runtime, disp)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
        except OSError as exc:
            raise WaylandUnavailable(f"cannot connect to {path}: {exc}")
        self.sock = s

    def alloc_id(self):
        with self._id_lock:
            i = self._next_id
            self._next_id += 1
            return i

    # ---- low-level send / receive --------------------------------------
    def _send(self, obj_id, opcode, body=b"", fd=None):
        size = 8 + len(body)
        msg = struct.pack("<II", obj_id, (size << 16) | opcode) + body
        with self._send_lock:
            if fd is None:
                self.sock.sendall(msg)
            else:
                anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))]
                # sendmsg may not write everything in one go in theory; in
                # practice these messages are tiny. Send ancillary with the
                # first (and only) chunk.
                self.sock.sendmsg([msg], anc)

    def _read_some(self, timeout=2.0):
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not data:
            raise WaylandUnavailable("compositor closed the connection")
        self._buf += data
        self._dispatch_buffer()
        return True

    def _dispatch_buffer(self):
        buf = self._buf
        while len(buf) >= 8:
            obj_id, sz_op = struct.unpack_from("<II", buf, 0)
            size = sz_op >> 16
            opcode = sz_op & 0xFFFF
            if size < 8:
                raise WaylandUnavailable(f"bad message size {size}")
            if len(buf) < size:
                break
            body = buf[8:size]
            buf = buf[size:]
            try:
                self._handle(obj_id, opcode, body)
            except WaylandUnavailable:
                raise
            except Exception as exc:  # never let a parse hiccup kill us
                sys.stderr.write(f"[brightness] dispatch error: {exc}\n")
        self._buf = buf

    # ---- event handling -------------------------------------------------
    def _handle(self, obj_id, opcode, body):
        if obj_id == WL_DISPLAY_ID:
            if opcode == WL_DISPLAY_ERR_EVENT:
                bad_obj, code = struct.unpack_from("<II", body, 0)
                msg, _ = _read_string(body, 8)
                self._error = f"wl_display error obj={bad_obj} code={code}: {msg}"
                raise WaylandUnavailable(self._error)
            return  # delete_id: nothing to recycle, ignore

        knd = self.kind.get(obj_id)
        if knd == "registry":
            if opcode == WL_REGISTRY_GLOBAL_EVENT:
                (name,) = struct.unpack_from("<I", body, 0)
                iface, off = _read_string(body, 4)
                (version,) = struct.unpack_from("<I", body, off)
                self.globals.append((name, iface, version))
        elif knd == "callback":
            if opcode == WL_CALLBACK_DONE_EVENT:
                self.callback_done[obj_id] = True
        elif knd == "output":
            info = self.outputs[obj_id]
            if opcode == WL_OUTPUT_NAME_EVENT:
                info["name"], _ = _read_string(body, 0)
            elif opcode == WL_OUTPUT_GEOMETRY_EVENT:
                # x,y,pw,ph,subpixel, make(str), model(str), transform
                make, off = _read_string(body, 24)
                model, off = _read_string(body, off)
                info.setdefault("model", model)
            elif opcode == WL_OUTPUT_MODE_EVENT:
                flags, w, h = struct.unpack_from("<Iii", body, 0)
                if flags & 0x1:  # current mode
                    info["mode"] = (w, h)
        elif knd == "gamma":
            g = self.gamma[obj_id]
            if opcode == GAMMA_CONTROL_SIZE_EVENT:
                (g["size"],) = struct.unpack_from("<I", body, 0)
            elif opcode == GAMMA_CONTROL_FAILED_EVENT:
                g["failed"] = True

    def roundtrip(self):
        """Block until the compositor has processed everything sent so far."""
        cb = self.alloc_id()
        self.kind[cb] = "callback"
        self.callback_done[cb] = False
        self._send(WL_DISPLAY_ID, WL_DISPLAY_SYNC, struct.pack("<I", cb))
        deadline = time.monotonic() + 10.0
        while not self.callback_done.get(cb):
            if not self._read_some():
                if time.monotonic() > deadline:
                    raise WaylandUnavailable("roundtrip timed out")

    # ---- setup ----------------------------------------------------------
    def _bind(self, registry_id, name, iface, version, new_id):
        body = struct.pack("<I", name) + _pack_string(iface) \
            + struct.pack("<II", version, new_id)
        self._send(registry_id, WL_REGISTRY_BIND, body)

    def discover(self):
        """Connect, enumerate outputs, and create a gamma control for each."""
        self.connect()
        registry_id = self.alloc_id()
        self.kind[registry_id] = "registry"
        self._send(WL_DISPLAY_ID, WL_DISPLAY_GET_REGISTRY,
                   struct.pack("<I", registry_id))
        self.roundtrip()

        mgr = next((g for g in self.globals if g[1] == GAMMA_MANAGER_IFACE), None)
        if mgr is None:
            raise WaylandUnavailable(
                "compositor does not support wlr-gamma-control "
                "(no zwlr_gamma_control_manager_v1)")
        self.manager_id = self.alloc_id()
        self._bind(registry_id, mgr[0], GAMMA_MANAGER_IFACE,
                   min(mgr[2], 1), self.manager_id)

        for name, iface, version in self.globals:
            if iface != "wl_output":
                continue
            out_id = self.alloc_id()
            self.kind[out_id] = "output"
            self.outputs[out_id] = {
                "registry_name": name, "version": version,
                "name": None, "mode": None,
            }
            self._bind(registry_id, name, "wl_output", min(version, 4), out_id)
        self.roundtrip()  # collect output name/mode events

        for out_id in list(self.outputs):
            g_id = self.alloc_id()
            self.kind[g_id] = "gamma"
            self.gamma[g_id] = {"output": out_id, "size": None, "failed": False}
            body = struct.pack("<II", g_id, out_id)
            self._send(self.manager_id, GAMMA_MANAGER_GET_CONTROL, body)
        self.roundtrip()  # collect gamma_size / failed events

    def displays(self):
        """Return [{key,label,gamma_id,size}] for usable outputs, sorted."""
        out = []
        for g_id, g in self.gamma.items():
            if g["failed"] or not g["size"]:
                continue
            info = self.outputs[g["output"]]
            name = info.get("name") or info.get("model") or f"output-{g['output']}"
            label = name
            if info.get("mode"):
                label = f"{name}  ({info['mode'][0]}×{info['mode'][1]})"
            out.append({"key": name, "label": label,
                        "gamma_id": g_id, "size": g["size"]})
        out.sort(key=lambda d: d["key"])
        return out

    def unavailable_outputs(self):
        """Names of outputs whose gamma control failed (already grabbed)."""
        names = []
        for g in self.gamma.values():
            if g["failed"]:
                info = self.outputs.get(g["output"], {})
                names.append(info.get("name") or info.get("model") or "?")
        return names

    # ---- applying brightness -------------------------------------------
    def set_brightness(self, gamma_id, size, level):
        """level in 0..1. Writes a brightness-scaled linear ramp to the fd."""
        level = max(0.0, min(1.0, level))
        ramp = bytearray(size * 3 * 2)
        denom = (size - 1) or 1
        # one channel, then copy to G and B
        chan = bytearray(size * 2)
        for i in range(size):
            v = int(round(i * 65535.0 / denom * level))
            if v > 65535:
                v = 65535
            struct.pack_into("<H", chan, i * 2, v)
        per = size * 2
        ramp[0:per] = chan
        ramp[per:2 * per] = chan
        ramp[2 * per:3 * per] = chan

        fd = os.memfd_create("vj-gamma", 0) if hasattr(os, "memfd_create") \
            else os.open("/dev/shm", os.O_TMPFILE | os.O_RDWR)
        try:
            os.write(fd, bytes(ramp))
            os.lseek(fd, 0, os.SEEK_SET)
            self._send(gamma_id, GAMMA_CONTROL_SET_GAMMA, b"", fd=fd)
        finally:
            os.close(fd)

    # ---- background reader (keeps connection alive while GUI runs) ------
    def start_reader(self):
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def _reader_loop(self):
        while self._running:
            try:
                self._read_some(timeout=1.0)
            except WaylandUnavailable as exc:
                sys.stderr.write(f"[brightness] wayland link lost: {exc}\n")
                return
            except OSError:
                return

    def close(self):
        self._running = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────
#  X11 fallback (xrandr --brightness) — only if the session is X11
# ─────────────────────────────────────────────────────────────────────────
class XrandrGamma:
    def __init__(self):
        import shutil
        if not shutil.which("xrandr"):
            raise WaylandUnavailable("xrandr not found")
        self._outputs = self._list_outputs()
        if not self._outputs:
            raise WaylandUnavailable("xrandr reported no connected outputs")

    def _list_outputs(self):
        import subprocess
        res = subprocess.run(["xrandr", "--query"], capture_output=True,
                             text=True)
        outs = []
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "connected":
                mode = ""
                for p in parts:
                    if "x" in p and p[0].isdigit():
                        mode = p.split("+")[0]
                        break
                outs.append((parts[0], mode))
        return outs

    def displays(self):
        out = []
        for name, mode in self._outputs:
            label = f"{name}  ({mode})" if mode else name
            out.append({"key": name, "label": label,
                        "gamma_id": name, "size": 0})
        return out

    def unavailable_outputs(self):
        return []

    def set_brightness(self, name, _size, level):
        import subprocess
        subprocess.run(["xrandr", "--output", name,
                        "--brightness", f"{max(0.0, min(1.0, level)):.3f}"],
                       check=False)

    def start_reader(self):
        pass

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────────────────────────────────
def _state_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "vj-brightness.json")


def load_levels():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_levels(levels):
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(levels, f, indent=2)
    except OSError as exc:
        sys.stderr.write(f"[brightness] could not save levels: {exc}\n")


# ─────────────────────────────────────────────────────────────────────────
#  Tkinter GUI
# ─────────────────────────────────────────────────────────────────────────
MIN_PCT = 10  # never let the operator black themselves out by accident


def run_gui(backend, displays):
    import tkinter as tk
    from tkinter import font as tkfont

    saved = load_levels()
    levels = {}  # key -> int percent

    root = tk.Tk()
    root.title("Display Brightness")
    root.configure(bg="#15151b")
    try:
        root.tk.call("tk", "scaling", 1.4)
    except tk.TclError:
        pass

    head_font = tkfont.Font(family="DejaVu Sans", size=14, weight="bold")
    lbl_font = tkfont.Font(family="DejaVu Sans", size=11)
    small_font = tkfont.Font(family="DejaVu Sans", size=9)

    tk.Label(root, text="Master Brightness", font=head_font,
             bg="#15151b", fg="#f0f0f5").pack(padx=18, pady=(16, 2))
    tk.Label(root,
             text="Software dimming for displays that ignore their own controls.",
             font=small_font, bg="#15151b", fg="#9a9aa8").pack(padx=18, pady=(0, 10))

    rows = []  # (disp, scale, value_label)

    def apply(disp, pct):
        pct = int(float(pct))
        levels[disp["key"]] = pct
        backend.set_brightness(disp["gamma_id"], disp["size"], pct / 100.0)

    def on_slide(disp, value_label):
        def _cb(val):
            pct = int(float(val))
            value_label.config(text=f"{pct}%")
            apply(disp, pct)
        return _cb

    for disp in displays:
        frame = tk.Frame(root, bg="#1e1e27", bd=0)
        frame.pack(fill="x", padx=14, pady=6)

        header = tk.Frame(frame, bg="#1e1e27")
        header.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(header, text=disp["label"], font=lbl_font,
                 bg="#1e1e27", fg="#e8e8f0", anchor="w").pack(side="left")
        value_label = tk.Label(header, text="100%", font=lbl_font,
                               bg="#1e1e27", fg="#7fd4ff", width=5, anchor="e")
        value_label.pack(side="right")

        start = int(saved.get(disp["key"], 100))
        start = max(MIN_PCT, min(100, start))
        scale = tk.Scale(frame, from_=MIN_PCT, to=100, orient="horizontal",
                         showvalue=False, length=380, sliderlength=28,
                         bg="#1e1e27", fg="#e8e8f0", troughcolor="#33333f",
                         highlightthickness=0, bd=0,
                         activebackground="#7fd4ff",
                         command=on_slide(disp, value_label))
        scale.set(start)
        scale.pack(fill="x", padx=12, pady=(2, 10))
        value_label.config(text=f"{start}%")
        rows.append((disp, scale, value_label))
        apply(disp, start)  # push the restored level to the compositor now

    # footer / actions
    footer = tk.Frame(root, bg="#15151b")
    footer.pack(fill="x", padx=14, pady=(4, 12))

    def reset_all():
        for disp, scale, value_label in rows:
            scale.set(100)
            value_label.config(text="100%")
            apply(disp, 100)

    tk.Button(footer, text="Reset all to 100%", font=small_font,
              bg="#2a2a36", fg="#e8e8f0", activebackground="#3a3a48",
              relief="flat", padx=10, pady=4, command=reset_all).pack(side="left")

    unavailable = backend.unavailable_outputs()
    note = "Close this window to restore full brightness."
    if unavailable:
        note += "  (Busy, skipped: " + ", ".join(unavailable) + ")"
    tk.Label(root, text=note, font=small_font,
             bg="#15151b", fg="#9a9aa8").pack(padx=18, pady=(0, 12))

    def on_close():
        save_levels(levels)
        backend.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    backend.start_reader()
    root.mainloop()


def run_no_displays(message):
    import tkinter as tk
    root = tk.Tk()
    root.title("Display Brightness")
    root.configure(bg="#15151b")
    tk.Label(root, text="Display Brightness", font=("DejaVu Sans", 14, "bold"),
             bg="#15151b", fg="#f0f0f5").pack(padx=24, pady=(20, 6))
    tk.Label(root, text=message, font=("DejaVu Sans", 10), justify="left",
             wraplength=460, bg="#15151b", fg="#d0d0d8").pack(padx=24, pady=(0, 20))
    tk.Button(root, text="Close", command=root.destroy,
              bg="#2a2a36", fg="#e8e8f0", relief="flat",
              padx=16, pady=5).pack(pady=(0, 20))
    root.mainloop()


def main():
    backend = None
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    errors = []

    # Prefer Wayland gamma-control (the operator's labwc setup). Fall back to
    # xrandr only if there's no Wayland socket (a real X11 session).
    if os.environ.get("WAYLAND_DISPLAY"):
        try:
            wl = WaylandGamma()
            wl.discover()
            backend = wl
        except WaylandUnavailable as exc:
            errors.append(f"Wayland: {exc}")

    if backend is None and (session == "x11" or not os.environ.get("WAYLAND_DISPLAY")):
        try:
            backend = XrandrGamma()
        except WaylandUnavailable as exc:
            errors.append(f"X11/xrandr: {exc}")

    if backend is None:
        msg = ("Couldn't set up software brightness control on this session.\n\n"
               + "\n".join(errors)
               + "\n\nThis tool needs a wlroots Wayland compositor (labwc/wayfire/"
                 "sway) or an X11 session with xrandr.")
        sys.stderr.write(msg + "\n")
        try:
            run_no_displays(msg)
        except Exception:
            pass
        return 1

    displays = backend.displays()
    if not displays:
        busy = backend.unavailable_outputs()
        msg = "No controllable displays were found."
        if busy:
            msg += ("\n\nThese outputs are already controlled by another program "
                    "(e.g. gammastep / wlsunset / night-light) and were skipped:\n  "
                    + ", ".join(busy)
                    + "\n\nClose that program and reopen this one.")
        sys.stderr.write(msg + "\n")
        run_no_displays(msg)
        return 1

    sys.stderr.write("[brightness] controllable displays: "
                     + ", ".join(d["label"] for d in displays) + "\n")
    run_gui(backend, displays)
    return 0


if __name__ == "__main__":
    sys.exit(main())
