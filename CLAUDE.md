# CLAUDE.md — handoff context for the VJ-pi rewrite

You're picking up a project mid-rewrite. Read this whole file before
acting. The conversation that led here was long, made expensive
mistakes, and the operator has a low tolerance for repeats.

## What this project is

A manual VJ rig for a Raspberry Pi 5 + projector + tiny wireless
keyboard. The operator triggers clips, generators, FX, and
projection-mapping warps from the keyboard while watching a control
HUD on a second screen.

Currently being rebuilt from scratch on **GStreamer + GTK3** after
the previous pygame / cv2 / numpy / moderngl architecture hit
fundamental limits on Pi 5's V3D driver. Active branch:
`claude/gstreamer-rewrite`.

## Hard hardware / platform facts (non-negotiable)

These are Pi 5 / V3D / Bookworm realities. Design with them; don't
try to work around them.

1. **Pi 5 has no H.264 hardware decode.** The block was deliberately
   removed by the Pi Foundation. H.264 decodes in software via
   `avdec_h264`, which eats CPU at 2K+ resolutions. **HEVC has
   hardware decode** via `v4l2slh265dec`. Practical implication: clips
   should be transcoded to HEVC 720p before going in `assets/clips/`.
   The asset processor (phase 2.5) does this in bulk.

2. **V3D leaks GL state between contexts in the same process.** Any
   two GL/EGL contexts coexisting (e.g. SDL2 hardware Renderer +
   moderngl standalone; two GTK GL widgets) corrupt each other —
   symptom: one window goes solid black while the other still
   renders. This killed the previous architecture twice. The rewrite
   uses **ONE GL context**, owned by GStreamer's `gl*` element family.

3. **Pi OS Bookworm apt doesn't carry `gst-plugins-rs`.** That's where
   `gtk4paintablesink` lives. We use GTK3 + `gtksink` (apt package
   `gstreamer1.0-gtk3`) instead. When Pi OS adopts Trixie, GTK4 +
   gtk4paintablesink becomes apt-installable; the swap is contained.

4. **`gtksink` is a CPU sink** — it accepts `video/x-raw` (system
   memory), not GL memory. GL pipeline branches feeding `gtksink` need
   `gldownload ! videoconvert` between the GL `tee` and the sink.

5. **`gtkglsink` is NOT a substitute** even though it sounds like one
   — each gtkglsink widget creates its own GL context, which on V3D
   means two contexts in one process = HUD goes black. Stay on
   `gtksink` even though it requires the CPU bridge.

## Architecture (rewrite, current state)

```
filesrc ─ decodebin ─[dynamic pad]─▶ downstream bin:

videoconvert ─ videoscale ─ glupload ─ tee ─┬─▶ gldownload ─ videoconvert ─ gtksink (output)
                                            └─▶ gldownload ─ videoconvert ─ gtksink (HUD)
```

One pipeline. One GL context. Two windows. The tee forks GL textures
by refcount (zero-copy). Each branch downloads to CPU only at the
sink stage. Both windows display the same composited frame; the
HUD's preview IS the projector output, just packed in a smaller
widget.

## Repo layout

- `main.py` — thin entry. Instantiates `VJApp`.
- `app.py` — GTK3 `Gtk.Application` + GStreamer pipeline. ~300 lines.
- `assets/clips/` — operator's MP4/MOV library. **Gitignored** (see
  workflow rules below; this rule is recent, and was added after the
  library was destroyed once).
- `assets/overlays/` — overlay MP4s. Also gitignored.
- `Start VJ.sh` — launcher with log-tee + zenity error dialog.
- `Test (single screen).sh` — single-monitor variant.
- `setup.sh` — apt deps (PyGObject + GStreamer + GTK3 + ffmpeg).
  No pip, no venv — PyGObject is system-managed.
- `Update.sh` — git pull from main, warns if setup.sh changed.
- `requirements.txt` — empty stub (no pip deps).
- `vj_last_run.log` — last launcher run, overwritten each launch.
- `vj_last_update.log` — last Update.sh run, overwritten each run.

## Rewrite plan (phases)

| Phase | Status   | What it adds                                                |
|-------|----------|-------------------------------------------------------------|
| 1     | DONE     | Dual-window GTK3 + GStreamer scaffold via `videotestsrc`   |
| 2     | DONE     | Real clip decode (`filesrc + decodebin → glupload`)         |
| 2.5   | DONE     | Asset processor (`Process Assets.sh`): bulk HEVC 720p       |
| 3     | DONE     | Generators (tunnel, plasma) as GLSL shaders via `glshader`  |
| 4a    | DONE     | Swappable source-bin + keyboard source switching (-/=/A/S)  |
| 4b    | NEXT     | Overlay layer via `compositor` (Q-P-style screen blend)     |
| 4c    | TODO     | Favourites grid (1-0 / Q-P: tap to play, hold to assign)    |
| 5     | TODO     | Mapping mode (groups, spaces, fit modes, edit-mode mouse)   |
| 6     | TODO     | FX chain, hits, autopilot, HUD status + FPS + polish        |

Each phase must end in something runnable on real Pi 5 hardware,
not just "syntax checked locally."

## Verified state on Pi 5

- **Phase 1:** SMPTE bars visible in both windows. No GL state leak,
  no dual-context conflict. Architecture proven.
- **Phase 2:** a clip loads via decodebin and plays in both windows.
  **Performance is poor (~5–15 fps, CPU pegged) with 2K H.264
  sources** — expected per constraint #1. The fix is the asset
  processor (phase 2.5), not architecture changes.

## Immediate next thing to do (if just teleported in)

Build the asset processor. ~20 lines of shell. It should:

1. Walk `assets/clips/` and `assets/overlays/`.
2. For each MP4/MOV that isn't already HEVC at canvas resolution, run:
   ```
   ffmpeg -i <input> -vf scale=1280:720 -c:v libx265 -preset fast \
          -crf 23 -an <input>_h265_720.mp4
   ```
3. Move the original to a `.original/` subfolder (don't delete).
4. Idempotent: skip files already processed.

Then point the operator at it and tell them to run it on their clip
library. Phase 3 (generators on GPU) can start while they wait.

## Workflow rules — DO NOT VIOLATE

1. **NEVER `git stash push -u` without confirming what's untracked
   first.** This destroyed the operator's clip library once. The mp4
   files were untracked, `-u` grabs untracked, the stash later got
   dropped or got mixed with other state, and the library was lost.
   Now `assets/clips/*` and `assets/overlays/*` are in `.gitignore`
   to prevent recurrence, but the principle is general: any
   destructive git operation needs `git status` + a sanity check
   first.

2. **NEVER run `git clean`, `git reset --hard`, `rm -rf`, or any
   destructive operation on the operator's files** without explicit
   permission AND showing them what will be affected first.

3. **NEVER trust that `git stash` is safe.** Inspect with
   `git stash show stash@{N} --stat` before dropping.

4. **Do web research before guessing about hardware behaviour.** The
   previous architecture wasted days because I guessed at V3D quirks
   instead of looking them up. Use WebSearch / WebFetch tools when
   the platform is doing something unexpected; don't speculate in a
   tight loop with the operator.

5. **Plan before coding for anything bigger than a one-line tweak.**
   Real-hardware deploy loops are expensive — operator has to pull,
   run, observe, report. Don't ship code that's likely to be wrong.
   For bigger changes, write the plan in chat first and get sign-off.

6. **Don't break what works.** Phase 1 and phase 2 are verified.
   Don't refactor them "while you're in there." If a refactor is
   needed, propose it explicitly and get a yes.

## Compressed post-mortem of the failed attempts

- **Original pygame/cv2 era** (pre-rewrite): worked but bottlenecked
  on CPU. Generators, compositing, mapping warp all in numpy/cv2.
  Steady 50% CPU at idle, dropping into single-digit FPS in mapping
  mode with autopilot.
- **First GPU attempt** (`claude/rpi5-gpu-acceleration-I85mA`, since
  reverted): ported the whole pipeline to moderngl. Hit a wall of
  V3D bugs — silent black FBOs (RGB8 not color-renderable on V3D),
  `glReadPixels(GL_RGB)` returning zeros, attribute introspection
  returning wrong locations. Never got pixels on the projector.
  Reverted in one commit.
- **Second GPU attempt** (`claude/gpu-offload-strategy-CKXXm`, also
  reverted): more surgical — Tier 1 moved output to SDL2 Renderer +
  streaming texture, Tier 3 added one generator (`tunnel`) via a
  standalone moderngl EGL context. Tier 1 worked. Tier 3 triggered
  the V3D dual-context state-leak — HUD went solid black the moment
  the moderngl context initialised. Tried HUD on software renderer,
  HUD streaming texture, VJ_NO_GPU kill switch. None fixed it. The
  root cause was structural, not a workaround target. Reverted.
- **Current rewrite** (`claude/gstreamer-rewrite`): structural fix.
  Single Gst.Pipeline, single GL context. Dual-context bug can't
  recur. Performance is now bottlenecked on CPU H.264 decode (Pi 5
  hardware limit, not architecture) — solved by the asset processor.

## Operator context

- Pi 5 + Pi OS Bookworm + projector + small operator screen.
- ~100+ MP4 loops, mostly from mantissa.xyz (2K H.264, ~5s loops).
  **The library was lost once during this work** because of the git
  stash mistake. The operator is rebuilding it. Treat their data as
  sacred — assume losing a single file is a serious incident.
- Frustration tolerance is low. Be honest about uncertainty. When
  you don't know, say so. When you make a mistake, own it plainly
  without grovelling. Move forward.
- The operator runs the launchers (`Start VJ.sh` etc.) on the Pi
  via the GUI file manager — "Execute" not "Execute in Terminal" —
  so stdout/stderr go to nowhere unless captured. The log-tee +
  zenity dialog patterns in the launchers exist for exactly this
  reason. Don't break them.

## How to test changes

1. Make the edit.
2. Commit + push to `claude/gstreamer-rewrite`.
3. Tell the operator to `git pull origin claude/gstreamer-rewrite`
   on the Pi and run the relevant launcher.
4. Ask for `vj_last_run.log` output (or the visible behaviour
   they report).
5. Iterate.

If the agent and the operator end up on the same Pi via
`claude --teleport`, this loop collapses to: edit → run → observe —
which is much faster and the preferred mode.

## Branch state at handoff

Top of `claude/gstreamer-rewrite`:
- Phase 1: GTK3 + GStreamer scaffold (videotestsrc proof).
- Phase 1 fix: GL → CPU bridge for `gtksink` link.
- Phase 2: clip playback via `filesrc + decodebin`.
- `.gitignore` fix: `assets/clips/*` and `assets/overlays/*`
  excluded so `git stash -u` can never touch user media again.

Latest verified test on Pi: phase 2 plays a 2K H.264 clip at low
FPS, both windows in sync, no errors. Asset processor is the next
deliverable to make playback usable.
