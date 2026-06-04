# CLAUDE.md — handoff context for VJ-pi

> # ⛔ THE OPERATOR NEVER USES A TERMINAL/CLI. EVER. ⛔
>
> Read this twice. It has been said every session and missed every
> session. Stop missing it.
>
> The operator runs **everything by double-clicking `.sh` launchers in
> the GUI file manager** ("Execute"). They do **not** open a terminal,
> do **not** type commands, and will **not** run `git`, `python`,
> `ffmpeg`, or anything else by hand. It is 2026; do not ask a human to
> hand-type commands at a prompt.
>
> **What this means for you, concretely:**
> - **NEVER** give the operator a command to run. Not `git pull`, not
>   `./something.sh`, not `cd`, not one single command. If your reply
>   contains a command for THEM to type, you have failed.
> - To ship a change: commit + push. For them to **get** it, they
>   double-click **`Update.sh`** (it runs the pull). To **run** it, they
>   double-click the relevant launcher (e.g. `Start VJ.sh`).
> - Anything you want them to *do* must be a **double-clickable `.sh`
>   launcher** (log-tee + zenity dialog, like the existing ones) or a
>   keypress inside the running app. If a capability needs the CLI, YOU
>   wrap it in a launcher — that is your job, not theirs.
> - Results come back as a **zenity dialog** and/or a `vj_last_*.log`
>   file they hand you. Design every workflow to end that way.
>
> The CLI is YOUR tool, inside this container. It is never the operator's.

Read this before acting. The project has a long history of expensive
mistakes; the operator has a low tolerance for repeats. The short
version: **the working system is the hybrid described below. Build on
it. Don't try to rebuild it.**

## What this project is

A manual VJ rig for a Raspberry Pi 5 + projector + tiny wireless
keyboard. The operator triggers clips, generators, FX, hits, and
projection-mapping warps from the keyboard while watching a control
HUD on a second screen. Full operator-facing docs (keymap, mapping
mode, autopilot, asset prep) live in `README.md` — that file is
current; trust it.

## Architecture — the hybrid (this is what works)

Two halves, deliberately split across a process boundary:

1. **Main app: pygame + OpenCV/numpy.** The proven CPU compositor.
   Owns clips, the FX chain, hits, favourites, autopilot, and
   projection mapping. One pygame output window (the projector) plus
   a second SDL2 window for the control HUD. Render pipeline per
   frame: base layer (clip / generative / black) → FX chain → hits →
   blit. This is the original working code path, restored after the
   rewrite detour.

2. **GPU generators: a separate GStreamer/GL worker process.**
   `shader_catalog.py` holds GLSL fragment shaders; `gpu_generator_
   worker.py` runs them through `videotestsrc ! glupload ! glshader !
   gldownload ! appsink` and pipes RGB frames back over a pipe.
   `gpu_generators.py` is the client/bridge in the main process.

   **Why a separate process:** V3D leaks GL state between contexts in
   the same process — two coexisting GL/EGL contexts corrupt each
   other (symptom: one surface goes solid black). Isolating all GL in
   its own process is the structural fix. **Do not merge the GL worker
   back into the main pygame process** — that reintroduces the exact
   bug that wasted weeks.

The main app stays on CPU (numpy/cv2); the GPU is reached only through
the out-of-process worker. That separation is the whole point of the
hybrid — respect it.

### Adding a generator (the fun part)

Add a GLSL fragment shader string to the `GPU_GENERATORS` dict in
`shader_catalog.py`. That's it — it auto-wires into the `[`/`]` cycle,
the generator favourite slots, and autopilot, because everything reads
from that dict (`GPU_GENERATOR_ORDER`). Shaders are GLSL ES (`#version
100`) and get `time`, `width`, `height` uniforms for free. For a
texture-mapped generator, see `donut`: the worker routes it through a
`uridecodebin … imagefreeze … glupload ! glshader` path that binds an
image from `assets/images/` as `sampler2D tex`.

## Repo layout

- `main.py` — argparse + pygame init + window setup + main-loop wiring.
- `engine.py` — `Engine`: state, per-frame render pipeline, public
  actions. The big one.
- `control.py` — `ControlWindow`: HUD preview, state badges, key sheet.
- `effects.py` — numpy/OpenCV FX + CPU generator fallbacks.
- `mapping.py` — projection-mapping mode (spaces, groups, warps).
- `clips.py` — `ClipPool`: lazy MP4 loader, LRU-evicted handles.
- `keymap.py` — pygame key → engine action dispatch.
- `shader_catalog.py` — GLSL generator catalogue (`GPU_GENERATORS`).
- `gpu_generator_worker.py` — out-of-process GStreamer/GL renderer.
- `gpu_generators.py` — client/bridge that talks to the worker.
- `config.py`, `state.py`, `display_helpers.py` — config dataclass,
  `vj_state.json` persistence, display geometry helpers.
- `assets/clips/` — operator's MP4 library. **Gitignored** (see
  workflow rules — the library was destroyed once).
- `assets/images/` — stills for texture generators (donut). Gitignored.
- `setup.sh` — system libs (SDL2/GL/GStreamer) + a Python venv with
  pygame + opencv + numpy. Re-runnable.
- `Process Assets.sh` — bulk-downsamples/re-encodes clips to render
  resolution; originals preserved in `assets/clips/_originals/`.
- `Start VJ.sh` / `Test (single screen).sh` — launchers (log-tee +
  zenity error dialog; see operator note below).
- `Update.sh` — git pull + setup-change warning.
- `vj_last_run.log`, `vj_last_update.log`, `vj_last_process.log` —
  last-run logs, overwritten each run.

## Hard hardware / platform facts (non-negotiable)

1. **V3D leaks GL state between contexts in one process.** This is why
   GPU generators live in a separate process. Don't co-locate GL
   contexts. (This killed two earlier GPU attempts — see history.)

2. **Pi 5 has no H.264 hardware decode** (the block was removed), **but it
   DOES hardware-decode HEVC (H.265)** — and that's the current direction
   for 2K clips (see "2K HEVC migration" below). The legacy H.264 path
   still works: clips decode in software via OpenCV `VideoCapture`, and
   `Start VJ.sh` plays `assets/clips/` at render resolution.

3. **The operator launches via the GUI file manager** ("Execute", not
   "Execute in Terminal"), so stdout/stderr go nowhere unless captured.
   The log-tee + zenity dialog in the launchers exist for exactly this.
   Don't break them.

## 2K HEVC migration (current direction)

The Pi 5 hardware-decodes HEVC but can't hardware-*encode* it, so the
clip pipeline is moving to: **encode once to HEVC (fast on a PC, slow on
the Pi), then let the Pi hardware-decode.** Key pieces:

- **Playback:** `--hevc` mode (`Start VJ (2K HEVC).sh`) reads
  `assets/clips_hevc/` and decodes via `hevc_clips.HevcClipPool` +
  `hevc_decode_worker.py` — an out-of-process worker doing HW HEVC decode
  (`v4l2slh265dec`) + **ISP detile (`pispconvert`), NOT GL**. ⚠️ The worker
  MUST stay GL-free: the main app holds a V3D GL context (`--gpu-scale`),
  and a *second* GL context in the worker (the old
  `glupload!glcolorconvert!gldownload` path) silently outputs ALL-BLACK
  frames — the V3D dual-context blackout, manifesting **across processes**.
  This wasted a debug cycle (symptom: clips "cycle" but projector stays
  black). The converter order is `pisp → videoconvert → gl` (gl last,
  in-app-broken); override with `VJ_HEVC_CONV`. Worker errors log to
  `vj_last_hevc_worker.log` (never `/dev/null` again — that hid the cause).
  Needs `gstreamer1.0-pispconvert`. Clips are baked to **2048×1152 HEVC**
  (hvc1 / main / yuv420p); canvas runs `--width 2048 --height 1152
  --gpu-scale`.
- **PC baking (fast path):** `pc_clip_baker/` (`Bake Clips.bat` /
  `bake_clips.py`) uses NVENC to make those 2048×1152 HEVC clips; copy
  them into `assets/clips_hevc/`.
- **On-Pi processing (fallback):** `Process All Assets.sh` is the unified
  processor — 2K (`assets/clips/`→`clips_hevc/`), portrait
  (`assets/portrait/{rotate,crop}/` + loose → `clips_hevc/…-landscape`),
  and 4K (`assets/4k/`→`assets/4k/processed/`), all HEVC, skipping done.
  `Process Assets.sh` / `Process Portrait Assets.sh` are thin wrappers
  (`VJ_ONLY=clips|portrait`). On-Pi HEVC encode is software (slow) — fine
  for a few field clips, not a whole library.
- **Uploads:** `upload_server.py` + `Upload from Phone.sh` route phone
  uploads to the right folder (ready-HEVC→`clips_hevc/`, raw 2K→`clips/`,
  4K→`4k/`, portrait→`portrait/{rotate,crop}/` or loose) via the page's
  destination picker.
- **Legacy H.264 path is intentionally kept** (`Start VJ.sh` +
  `assets/clips/`) until HEVC is confirmed on the operator's hardware —
  do not rip it out. `assets/clips/` doubles as the raw-input folder the
  HEVC bake reads from.

## How to test changes

1. Make the edit.
2. Commit + push to the active branch.
3. Operator does `git pull` on the Pi and runs the relevant launcher.
4. Ask for the matching `vj_last_*.log` (or the observed behaviour).
5. Iterate.

GLSL can't be compile-checked in most dev environments (no validator);
the worker on the Pi is the real test — on failure, grab the
`[gpu-worker]` line from the log. If agent + operator share the Pi via
`claude --teleport`, the loop collapses to edit → run → observe.

## Workflow rules — DO NOT VIOLATE

0. **NEVER hand the operator a CLI command or a "run this in a terminal"
   instruction.** They work GUI-only (see the banner at the top). Every
   action you want them to take is a double-click on a `.sh` launcher or
   a keypress in the app. Need them to update? `Update.sh`. Need them to
   run a new tool? Build a launcher for it. This rule has been broken
   every session — do not be the next.

1. **NEVER `git stash push -u` without confirming what's untracked
   first.** This destroyed the operator's clip library once: the mp4s
   were untracked, `-u` grabbed them, the stash got dropped, library
   gone. `assets/clips/*` and `assets/images/*` are gitignored now, but
   the principle is general: any destructive git op needs `git status`
   + a sanity check first.

2. **NEVER run `git clean`, `git reset --hard`, `rm -rf`, or any
   destructive op on the operator's files** without explicit permission
   AND showing them what will be affected first.

3. **NEVER trust that `git stash` is safe.** Inspect with
   `git stash show stash@{N} --stat` before dropping.

4. **Do web research before guessing about hardware behaviour.** Past
   work wasted days guessing at V3D quirks. Look it up.

5. **Plan before coding for anything bigger than a one-line tweak.**
   Real-hardware deploy loops are expensive. For bigger changes, write
   the plan in chat first and get sign-off.

6. **Don't break what works.** The hybrid is verified and live. Don't
   refactor it "while you're in there"; propose refactors explicitly
   and get a yes.

## Why the architecture is what it is (compressed history)

- **Original pygame/cv2 app:** worked, CPU-bound. This is the base the
  current hybrid is built on.
- **GPU rewrite attempts (moderngl, then single-process GStreamer +
  GTK/VLC):** tried to move the whole pipeline onto the GPU in one
  process. All hit the V3D dual-context state-leak (HUD or output goes
  black the moment a second GL context initialises) or other V3D bugs.
  All reverted. **Don't resurrect the single-process-GL or VLC/GTK
  window path.**
- **The hybrid (current):** keep the working CPU app; reach the GPU
  only through an isolated worker process for generators. Dual-context
  bug can't recur because there's only ever one GL context per process.
  This is the path forward.

## Operator context

- Pi 5 + Pi OS Bookworm + projector + small operator screen.
- ~100+ MP4 loops, mostly 2K H.264 ~5s. **The library was lost once**
  during a git stash mistake. Treat operator data as sacred — losing a
  single file is a serious incident.
- Frustration tolerance is low. Be honest about uncertainty. Own
  mistakes plainly without grovelling. Move forward.

## Not yet built (genuine future work, not a rebuild)

- **Auto/beat mode:** aubio beat detection on a USB mic; scenes swap on
  downbeats, hits on kick onsets, FX intensity tracks RMS.
- **Hailo person matte:** silhouette the performer from a webcam and
  composite over the base.
- **Pre-baked Shadertoy MP4s:** bake favourite shaders offline to H.264
  loops for playback without live GLSL.
