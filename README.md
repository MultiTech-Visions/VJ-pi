# pi-paint VJ

A manual VJ rig for Raspberry Pi 5 + projector. **Currently being
rewritten** on top of GStreamer + GTK3 after the pygame/cv2 + moderngl
architecture hit a wall on V3D's dual-context state-leak. The rewrite
is happening in phases on the `claude/gstreamer-rewrite` branch; this
README will fill in as features land.

## Why the rewrite

The previous architecture had at least three independent rendering
systems coexisting in one process:

- Output via `pygame.display.set_mode` (legacy SDL surface).
- HUD via `pygame._sdl2.video.Window + Renderer` (newer SDL2 multi-window).
- Optional moderngl EGL context for shader-based generators.

On Pi 5's V3D driver, any two GL contexts in one process leak state
between each other — symptom: HUD turns solid black the moment the
shader pipeline initialises. The post-mortem of that effort sits in
the previous branch (`claude/gpu-offload-strategy-CKXXm`).

The fix isn't another workaround. It's one pipeline: GStreamer end
to end. Decode, composite, effects, and display all flow through a
single GL/EGL context owned by GStreamer's `gl*` element family.
Both windows (operator HUD and projector output) receive the same
GL texture via `tee` into two `gtksink` widgets. No dual-renderer
fight, no readback to RAM mid-frame.

## Status: phase 1

Two GTK3 windows, one GStreamer pipeline, one source
(`videotestsrc`), one tee, two `gtksink` previews. No VJ logic yet.
Phase 1 is the architectural proof: dual-window single-GL-context
on real V3D hardware.

Subsequent phases:

| Phase | Adds                                                          |
|-------|---------------------------------------------------------------|
| 2     | Real clip decode (`filesrc` → `decodebin` → `glupload`)       |
| 3     | Generators as GLSL fragment shaders via `glshader`            |
| 4     | Compositor + clip selection + keymap + favourites             |
| 5     | Mapping mode (groups, spaces, fit modes, edit-mode mouse)     |
| 6     | FX chain, hits, autopilot, HUD status panel + FPS + polish    |

Each phase ends in something runnable on the Pi.

## Install / launch

```bash
./setup.sh           # apt-installs GTK3 + GStreamer + ffmpeg
./Start\ VJ.sh       # dual display
./Test\ \(single\ screen\).sh   # both windows on the primary display
```

No virtualenv — everything goes through system Python and apt-managed
PyGObject. `requirements.txt` is intentionally empty; if you find
yourself wanting to `pip install` something into the project, double-
check there isn't an apt counterpart first (mixing pip and system
PyGObject is a fast path to broken introspection).

## Logs

Every launch writes `vj_last_run.log` (overwritten each run). Every
update writes `vj_last_update.log`. If a launch fails before the
window appears, the launcher pops a GUI dialog with the tail of the
log so you don't have to dig through a terminal.

## Hardware notes

- **Pi 5 has no H.264 hardware decode block** — it was deliberately
  removed by the Foundation. H.264 clips decode via software
  (`avdec_h264`). The Pi 5 CPU can handle ~4K30 H.264 in software
  but it competes with everything else for cycles.
- **HEVC has hardware decode** on Pi 5 via the V3D V4L2 stateful
  decoder. Re-encoding clips to HEVC is the highest-leverage perf
  move (planned for the asset processor in phase 2).
- The V3D driver is GLES 3.1 only — no desktop GL. GStreamer's
  `gl*` plugins use the right profile automatically; no shader code
  needs to know.

## Asset folders

```
assets/clips/       # MP4/MOV loops, slots 1-0 on the keyboard
assets/overlays/    # MP4/MOV overlays, slots Q-P
```

Drop files in via the file manager. Sorted alphabetically.

## What's gone (vs the pygame era)

Deleted in this rewrite: `engine.py`, `clips.py`, `control.py`,
`effects.py`, `mapping.py`, `display_helpers.py`, `keymap.py`,
`config.py`, `state.py`. Their functionality is being rebuilt on
top of GStreamer one phase at a time. The mapping data model
(groups, spaces, fit_modes) will return in phase 5 as plain Python
state driving GStreamer pad geometry and a custom homography
shader.
