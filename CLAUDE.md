# CLAUDE.md — handoff context for VJ-pi

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

2. **Pi 5 has no H.264 hardware decode** (the block was removed). Clips
   decode in software via OpenCV `VideoCapture`. `Process Assets.sh`
   downsamples + re-encodes the library to render resolution so the
   only per-frame cost is decode + a BGR→RGB shuffle. Re-run it
   whenever you change render resolution.

3. **The operator launches via the GUI file manager** ("Execute", not
   "Execute in Terminal"), so stdout/stderr go nowhere unless captured.
   The log-tee + zenity dialog in the launchers exist for exactly this.
   Don't break them.

## GPU / 4K playback — PROVEN, MEASURED FACTS (do not re-derive)

A long spike series settled how to play video on the GPU on this exact
Pi 5. These are measured results on the hardware, not theory. **The rig's
job is: play 4K HEVC clips at the highest frame rate, full-screen and
projection-mapped onto warped surfaces.** Real-time generation
(generators, MilkDrop/feedback shaders) was tried and **abandoned** —
hand-authored generated visuals look bad and do not approach real
MilkDrop. Trippy/psychedelic content comes from **curated 4K clips**, not
from generation. Only `kaliset` out of the generators is worth keeping.

### The working 4K pipeline (GStreamer GL, ~42 fps at 4K on the projector, zero CPU copy)
```
filesrc location="X.mp4" ! qtdemux ! h265parse ! v4l2slh265dec ! glupload ! glcolorconvert ! glimagesink
```
- Decode alone (to appsink, no display): **~162 fps** at 3840×2160.
- Decode → GL → glimagesink (the line above): **~42 fps** at 4K, fullscreen.
- \+ one `glshader` fragment pass: **~21 fps**. Every full-4K shader pass
  roughly **halves** the frame rate (V3D fill rate over 8.3M px). Be
  frugal with passes; run FX/warp at reduced res, reserve full 4K for the
  clean present.
- \+ GPU geometry warp (`gltransformation`): **~28 fps**. Mapping is a
  geometry/quad warp, NOT a per-pixel fragment warp.
- Live shader uniforms work:
  `glshader.set_property("uniforms", Gst.Structure.new_from_string("uniforms,u_amt=(float)0.5"))`

### Hard facts / dead ends (these cost real time to find)
- **Pi 5 hardware-decodes ONLY HEVC/H.265** (`v4l2slh265dec`), up to 4K.
  NO H.264 hw decode. **Source clips must be H.265/HEVC in MP4.**
- The HEVC decoder emits a tiled **NV12_128C8 ("SAND") buffer in DMABUF**.
  Plain sinks fail `not-negotiated`. The DMABUF→GL import works on
  **GStreamer 1.26.2** (the DMABUF+DRM-modifier `glupload` support is
  recent; older GStreamer fails).
- **CPU `videoconvert` on a 4K frame ≈ 6 fps.** NEVER pull a 4K frame into
  CPU/numpy. The entire win is keeping the frame on the GPU.
- **`playbin3` auto-selects the CPU path ≈ 7 fps.** Don't use it; use the
  explicit pipeline above.
- **`decodebin ! glupload` fails `not-negotiated`.** Use the explicit
  `qtdemux ! h265parse ! v4l2slh265dec` chain.
- **`kmssink` = "Permission denied (13)"** under the running labwc desktop
  (the compositor owns the DRM planes).

### Environment (this Pi)
- **GStreamer / `gi` (PyGObject) live in SYSTEM python3 (`/usr/bin/python3`),
  NOT the venv.** Run all GStreamer code under `/usr/bin/python3`. The venv
  (pygame, opencv, numpy, moderngl) has NO `gi`. Do not mix `gi` and venv
  libraries in one process. GStreamer version: **1.26.2**.

### Display (this Pi — labwc / Wayland)
- Compositor is **labwc** (`XDG_SESSION_TYPE=wayland`, `WAYLAND_DISPLAY=wayland-0`).
- **Projector = output `HDMI-A-2`** (EPL BEIKE), running **4096×2160 @
  ~24 Hz** (DCI 4K; there is no 4K@60 — 24 Hz is the cap at 4K). It is
  **pygame/SDL display index 1**.
- **Control screen = `HDMI-A-1`** (MPI7002), 1920×1080@60 at position
  1920,0; **pygame/SDL display index 0**.
- Projector is ~24 Hz, so **cap render rate to ~24–30 fps** — faster just
  pegs the GPU on frames that are never displayed (uncapped feedback
  pinned the GPU at 100%; capped + vsync at 720p sat at 60–70%).
- **`glimagesink` has no `fullscreen` property, and a Wayland client can't
  self-fullscreen on a chosen output.** Fullscreen-on-projector is a
  **labwc window rule** in `~/.config/labwc/rc.xml` matching the GStreamer
  GL window (app-id `python3`, title `OpenGL Renderer`):
  `<action name="MoveToOutput" output="HDMI-A-2"/>` THEN
  `<action name="ToggleFullscreen"/>` (move BEFORE fullscreen — labwc
  can't move an already-fullscreen window). Reload: `pkill -HUP labwc`.
  `apply_fullscreen_rule.py` installs it safely (backs up, validates XML).

### moderngl on V3D (only if a custom GL path is ever needed)
- `moderngl.create_context(require=300)` — V3D gives a GLES **3.1** context
  (version 310); moderngl otherwise demands desktop GL 3.3 (330) and bails.
- Shaders `#version 300 es`; explicit `layout(location=...)`; **RGBA8 FBOs
  only** (V3D renders zeros into RGB8). Ping-pong FBO feedback ran ~48 fps
  at 720p.

### Files from this work
- `gpu_compositor.py` — GStreamer GL player (system python3): the explicit
  4K pipeline above, plays clips (+ kept generators), live `glshader` FX
  slot, stdin commands.
- `apply_fullscreen_rule.py` / `Apply Fullscreen Rule.sh` — install the
  labwc fullscreen-on-projector rule.
- `Make 4K Test Clip.sh` — make a 4K HEVC test clip (Pi has no hw encoder;
  software x265/libx265 encode).
- `tests/spike_b_4k_decode.py` + `tests/README.md` — spike harness + the
  recorded numbers above.
- `milkdrop.py`, `tests/spike_e_feedback.py` — feedback/generation
  experiments, **abandoned**, kept only as reference.

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
