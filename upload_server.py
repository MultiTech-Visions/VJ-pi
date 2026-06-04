#!/usr/bin/env python3
"""Phone -> Pi clip uploader for VJ-pi.

A tiny, dependency-free web server so the operator can upload videos shot
on a phone straight into assets/clips/ from the phone's browser — no app,
no cable, no cloud. Built for the campsite case: the Pi runs its own WiFi
hotspot (see "Upload from Phone.sh"), the phone joins it, and you browse
to the Pi.

Design notes (why it looks like this):
  * stdlib only. Nothing to pip-install, so it keeps working right after
    an Update.sh with no setup.sh re-run. The main app's venv is NOT
    required — system python3 runs this fine.
  * Uploads STREAM to disk in chunks. A phone clip can be hundreds of MB
    (or multi-GB at 4K); we never buffer a whole file in RAM.
  * Each upload is written to a ".uploading.*" temp name in the clips dir
    and only os.replace()'d to its real ".mp4"/".mov"/... name once the
    full Content-Length has arrived. That mirrors Process Assets.sh's
    ".processing.*" convention so a half-finished or interrupted upload
    can never be picked up by the processor or shown as a clip.
  * Filenames are reduced to a safe basename; collisions get a _1, _2
    suffix so two "IMG_0001.MOV" from the camera roll don't clobber.

The browser side uploads each file as a raw POST body with the original
name in an X-Filename header (URL-encoded). That sidesteps multipart
parsing entirely and gives clean per-file upload progress via XHR.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# Extensions Process Assets.sh knows how to normalize. Keep in sync with
# the find() filter in that script.
ALLOWED_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".gif", ".m4v", ".wmv", ".flv",
}

# Read uploads in ~1 MiB chunks.
CHUNK = 1024 * 1024

ASSETS_DIR = ""  # absolute path to assets/, set in main()

# Upload destinations -> subfolder under assets/. The phone picks one of
# these; portrait additionally picks a mode (which subfolder). Keep the keys
# in sync with the <select>s in PAGE and with "Process All Assets.sh".
#   clips_hevc : finished 2K HEVC from the PC baker -> plays immediately
#   clips_raw  : raw 2K landscape -> bake on the Pi (Process All)
#   4k         : raw hi-res -> bake to cinematic (Process All)
#   portrait/* : raw vertical phone video -> bake to landscape (Process All)
_PORTRAIT_MODES = {"rotate": "portrait/rotate",
                   "crop": "portrait/crop",
                   "blur": "portrait"}


def _dest_subdir(dest: str, mode: str) -> str:
    """Resolve a (destination, portrait-mode) pair to an absolute folder
    inside assets/. Falls back to the ready-HEVC library for anything
    unrecognized, and refuses to escape assets/."""
    rel = {"clips_hevc": "clips_hevc",
           "clips_raw": "clips",
           "4k": "4k"}.get(dest)
    if rel is None and dest == "portrait":
        rel = _PORTRAIT_MODES.get(mode, "portrait")
    if rel is None:
        rel = "clips_hevc"
    path = os.path.normpath(os.path.join(ASSETS_DIR, rel))
    if path != ASSETS_DIR and not path.startswith(ASSETS_DIR + os.sep):
        return os.path.join(ASSETS_DIR, "clips_hevc")
    return path


def _safe_basename(name: str) -> str:
    """Reduce an arbitrary client-supplied name to a safe single filename.

    Strips any path components and characters that have no business in a
    filename. Always returns something non-empty with an allowed video
    extension (defaulting to .mp4 if the client sent none we recognize).
    """
    name = unquote(name or "")
    # path traversal / directory parts -> basename only
    name = name.replace("\\", "/").split("/")[-1]
    name = name.strip()
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in ALLOWED_EXTS:
        # Unknown/garbage extension: keep the visible name as the stem and
        # default the container to .mp4 (most phone exports are mp4/h264).
        if ext:
            stem = stem + ext.replace(".", "_")
        ext = ".mp4"
    # keep letters, digits, space, dot, dash, underscore, parens
    stem = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", stem).strip(" .") or "clip"
    return stem + ext


def _unique_path(directory: str, filename: str) -> str:
    """Return a path in `directory` that doesn't collide with an existing
    file, appending _1, _2, ... before the extension as needed."""
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{i}{ext}")
        i += 1
    return candidate


def _list_clips() -> list[str]:
    """Names of the playable HEVC library (assets/clips_hevc/), for the
    'N clips in the library' readout on the page."""
    hevc_dir = os.path.join(ASSETS_DIR, "clips_hevc")
    try:
        names = []
        for n in os.listdir(hevc_dir):
            if n.startswith(".") or n.startswith("_"):
                continue
            p = os.path.join(hevc_dir, n)
            if os.path.isfile(p) and os.path.splitext(n)[1].lower() in ALLOWED_EXTS:
                names.append(n)
        return sorted(names)
    except OSError:
        return []


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>VJ-pi — Upload clips</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px;
    font-family: -apple-system, system-ui, Roboto, sans-serif;
    background: #0b0b12; color: #e8e8f0;
    -webkit-text-size-adjust: 100%;
  }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .sub { color: #9aa; font-size: .9rem; margin: 0 0 20px; }
  .pick {
    display: block; width: 100%; padding: 22px; margin: 0 0 16px;
    font-size: 1.15rem; font-weight: 600; text-align: center;
    color: #fff; background: linear-gradient(135deg,#7b2ff7,#f107a3);
    border: none; border-radius: 16px; cursor: pointer;
  }
  .pick:active { filter: brightness(.9); }
  input[type=file] { display: none; }
  .hint { color: #889; font-size: .8rem; margin: -8px 0 20px; }
  label.fld { display: block; color: #9aa; font-size: .78rem; margin: 0 0 6px 2px; text-transform: uppercase; letter-spacing: .04em; }
  select {
    display: block; width: 100%; padding: 13px 12px; margin: 0 0 14px;
    font-size: 1rem; color: #e8e8f0; background: #16161f;
    border: 1px solid #2a2a38; border-radius: 12px; appearance: none;
  }
  #modeWrap.hide { display: none; }
  .row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; margin-bottom: 8px;
    background: #16161f; border-radius: 12px; font-size: .9rem;
  }
  .row .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .stat { font-size: .8rem; color: #9aa; min-width: 52px; text-align: right; }
  .bar { height: 6px; background: #2a2a38; border-radius: 4px; overflow: hidden; margin-top: 6px; }
  .bar > div { height: 100%; width: 0; background: linear-gradient(90deg,#7b2ff7,#f107a3); transition: width .15s; }
  .ok .stat { color: #5fd68a; }
  .err .stat { color: #ff6b6b; }
  .lib { margin-top: 24px; color: #889; font-size: .85rem; }
</style>
</head>
<body>
  <h1>🎥 Upload to VJ-pi</h1>
  <p class="sub">Pick videos from your phone — they drop straight into the right folder on the Pi.</p>

  <label class="fld" for="dest">Where should these go?</label>
  <select id="dest">
    <option value="clips_hevc">2K clip — ready to play (already baked HEVC)</option>
    <option value="clips_raw">2K video — raw, bake on the Pi</option>
    <option value="4k">4K video — cinematic, bake on the Pi</option>
    <option value="portrait">Portrait (vertical) phone video</option>
  </select>

  <div id="modeWrap" class="hide">
    <label class="fld" for="mode">How should the tall video fit 16:9?</label>
    <select id="mode">
      <option value="crop">Crop to centre — good for a person (cuts top &amp; bottom)</option>
      <option value="rotate">Rotate 90° — good for sideways-shot footage</option>
      <option value="blur">Blur-fill — keep the whole frame, blurred side bars</option>
    </select>
  </div>

  <button class="pick" id="pickBtn">Choose videos</button>
  <input type="file" id="file" accept="video/*" multiple>
  <p class="hint" id="hint">Tip: shoot in <b>landscape</b> when you can.</p>

  <div id="list"></div>
  <div class="lib" id="lib"></div>

<script>
const pickBtn = document.getElementById('pickBtn');
const fileInput = document.getElementById('file');
const list = document.getElementById('list');
const lib = document.getElementById('lib');
const dest = document.getElementById('dest');
const mode = document.getElementById('mode');
const modeWrap = document.getElementById('modeWrap');
const hint = document.getElementById('hint');

const HINTS = {
  clips_hevc: 'These are baked HEVC clips from the PC — they play right away, no processing needed.',
  clips_raw: 'Raw 2K video. After uploading, run <b>Process All Assets.sh</b> on the Pi to bake it.',
  '4k': 'Raw hi-res video. After uploading, run <b>Process All Assets.sh</b> on the Pi; press N for cinematic mode.',
  portrait: 'Vertical phone video. After uploading, run <b>Process All Assets.sh</b> on the Pi to make a landscape clip.'
};
function syncDest() {
  const isPortrait = dest.value === 'portrait';
  modeWrap.classList.toggle('hide', !isPortrait);
  hint.innerHTML = HINTS[dest.value] || '';
}
dest.addEventListener('change', syncDest);
syncDest();

pickBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  const files = Array.from(fileInput.files || []);
  fileInput.value = '';            // allow re-picking the same file later
  uploadQueue(files);
});

function fmt(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(0) + ' KB';
  if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
  return (bytes/1073741824).toFixed(2) + ' GB';
}

async function uploadQueue(files) {
  const d = dest.value, m = mode.value;   // snapshot for the whole batch
  for (const f of files) await uploadOne(f, d, m);
  refreshLib();
}

function uploadOne(file, d, m) {
  return new Promise((resolve) => {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML =
      '<div style="flex:1">' +
        '<div class="name"></div>' +
        '<div class="bar"><div></div></div>' +
      '</div><div class="stat">0%</div>';
    row.querySelector('.name').textContent = file.name + '  (' + fmt(file.size) + ')';
    list.prepend(row);
    const bar = row.querySelector('.bar > div');
    const stat = row.querySelector('.stat');

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('X-Dest', d);
    xhr.setRequestHeader('X-Mode', m);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round(e.loaded / e.total * 100);
      bar.style.width = pct + '%';
      stat.textContent = pct + '%';
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        bar.style.width = '100%';
        row.classList.add('ok'); stat.textContent = '✓ done';
      } else {
        row.classList.add('err'); stat.textContent = 'failed';
      }
      resolve();
    };
    xhr.onerror = () => { row.classList.add('err'); stat.textContent = 'error'; resolve(); };
    xhr.send(file);
  });
}

async function refreshLib() {
  try {
    const r = await fetch('/clips');
    const j = await r.json();
    lib.textContent = j.count + ' clip' + (j.count === 1 ? '' : 's') + ' in the library.';
  } catch (e) { /* ignore */ }
}
refreshLib();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "VJpiUpload/1.0"

    # Quieter logging: one line per request to stderr (captured by the
    # launcher's log tee).
    def log_message(self, fmt, *args):
        sys.stderr.write("[upload] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/clips":
            names = _list_clips()
            self._send_json(200, {"count": len(names), "names": names})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/upload":
            self._send_json(404, {"error": "not found"})
            return

        raw_name = self.headers.get("X-Filename", "")
        filename = _safe_basename(raw_name)
        dest_dir = _dest_subdir(self.headers.get("X-Dest", ""),
                                self.headers.get("X-Mode", ""))
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            self._send_json(500, {"error": "cannot create %s: %s" % (dest_dir, exc)})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(411, {"error": "Content-Length required"})
            return

        # Stream to a temp file in the SAME dir (so the final rename is
        # atomic and never crosses a filesystem boundary). The .uploading.
        # prefix keeps partials invisible to the processors.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".uploading.", suffix=os.path.splitext(filename)[1], dir=dest_dir
        )
        received = 0
        try:
            with os.fdopen(fd, "wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    remaining -= len(chunk)
        except (OSError, ConnectionError) as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self._send_json(500, {"error": "write failed: %s" % exc})
            return

        if received != length:
            # Truncated upload (phone walked out of WiFi range, etc.).
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self._send_json(400, {"error": "incomplete upload",
                                  "received": received, "expected": length})
            return

        final_path = _unique_path(dest_dir, filename)
        try:
            os.replace(tmp_path, final_path)
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self._send_json(500, {"error": "rename failed: %s" % exc})
            return

        saved = os.path.basename(final_path)
        sys.stderr.write("[upload] saved %s (%d bytes)\n" % (saved, received))
        self._send_json(200, {"saved": saved, "bytes": received})


def _local_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main(argv=None):
    global ASSETS_DIR
    here = os.path.dirname(os.path.abspath(__file__))
    default_assets = os.path.join(here, "assets")

    ap = argparse.ArgumentParser(description="VJ-pi phone clip uploader")
    ap.add_argument("--assets", default=default_assets,
                    help="path to the assets/ folder (uploads route into its subfolders)")
    ap.add_argument("--port", type=int, default=8000, help="listen port")
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    args = ap.parse_args(argv)

    ASSETS_DIR = os.path.abspath(args.assets)
    os.makedirs(os.path.join(ASSETS_DIR, "clips_hevc"), exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    ips = _local_ips() or ["<this-pi-ip>"]
    sys.stderr.write("[upload] serving assets at %s on port %d\n" % (ASSETS_DIR, args.port))
    for ip in ips:
        sys.stderr.write("[upload]   http://%s:%d\n" % (ip, args.port))
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[upload] shutting down\n")
        httpd.shutdown()


if __name__ == "__main__":
    main()
