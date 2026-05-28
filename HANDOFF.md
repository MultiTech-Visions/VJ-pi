# Handoff — VJ-pi on Pi 5

## Why you're here

The previous agent (Claude Code) burned a lot of operator time on this codebase and didn't ship a smoothly-working clip-playback path. Operator has lost patience and wants a fresh pair of eyes. **The repo is at commit `977b6df`** — generators work, clip playback via VLC needs validation + likely window-management fixes.

**Operator is on a Raspberry Pi 5, Pi OS Bookworm/Trixie, Wayland (labwc), 2-monitor setup (operator screen + projector). Tolerance for nuance bullshit: zero. Ship working code, don't speculate.**

## What this app is

Manual VJ rig. Clips loop, generators run procedural GLSL shaders, all triggered from a tiny wireless keyboard while the operator watches a HUD on the secondary screen. `CLAUDE.md` at the repo root has the long-form context.

## Architecture today

- **Clip playback** → VLC via `python-vlc` (in-process libvlc). One window, VLC manages it.
- **Generators** → GStreamer pipeline (`videotestsrc` → `glupload` → `glshader` → `tee` → `gldownload` → `videoconvert` → `gtksink`). 13 GLSL shaders + a snake-game-on-truchet that pushes uniforms from CPU.
- **HUD** → GTK3 window on the operator screen, text-only.
- Source switching toggles which output window is visible. mpv attribute names are still `self._player` (renamed from `_mpv` after the engine swap).

## Known-good state

- Generators (plasma, tunnel, truchet w/ snake, donut with image texture, etc.) render correctly at acceptable CPU when using the GStreamer pipeline → gtksink → output_window.
- Process Assets.sh transcodes clips to **MJPEG `.mov` 1280×720** (originally HEVC; switched because the Pi 5 HEVC HW decoder's DMABuf path doesn't negotiate with `glupload`, and we ended up using VLC anyway so codec choice is now flexible — `.mov` MJPEG is what's in `assets/clips/` right now).
- VLC bare CPU playing 720p HEVC: ~7%. App total CPU with VLC + GTK + GStreamer scaffolding: ~25%.
- Snake game on the `truchet` generator works (CPU sim of arc-graph walking, positions pushed to shader as 16 individual `snake_N` vec2 uniforms via `GstStructure.from_string` syntax).

## What's BROKEN / needs your attention

1. **Window management on the projector** is the live bug. VLC's `set_fullscreen(True)` does NOT honour a target output the way mpv's `--fs-screen-name` did — it fullscreens onto whichever monitor its window is on, which on the operator's rig was the small operator screen with no way to escape. Last incident required a hard reset of the Pi.

   - **Current mitigation**: auto-fullscreen on launch is DISABLED. Operator presses `F` in the HUD to toggle. VLC instance is configured with `--key-quit=q,Ctrl+q,Esc` so panic-exit always works.
   - **What needs to happen**: position VLC's window on the projector output BEFORE going fullscreen. On Wayland this is hard — `wlr-output-management` or compositor hints. Could also explore using GStreamer-`waylandsink`-based output for clips, embedded in our own GTK window (more control over which output it lands on).

2. **No verification on actual hardware screen**. Previous agent had no way to see what was actually rendered. You'll need the operator to run + report visible behavior, or set up screenshots (`grim` is installed for Wayland; the app's main display is `HDMI-A-2` per `wlr-randr`).

3. **`HANDOFF.md` and `/tmp/codex_install.sh`** are scratch — feel free to delete them when you've absorbed the context.

## Things NOT to relitigate (already explored, please don't loop)

These are documented in CLAUDE.md and `~/.claude/projects/-home-multitech-Desktop-VJ/memory/`:

- **Pi 5 has no H.264 hardware decode.** HEVC has hw decode via `v4l2slh265dec` but the DMABuf output doesn't negotiate cleanly with `glupload` on current Pi OS. Don't try to chase this — VLC sidesteps it.
- **V3D dual GL-context bug**: two GL contexts in one process corrupt each other. Don't use `gtkglsink` (creates a per-widget GL context). `gtksink` is the CPU-bridge sink we use.
- **mpv default uses libplacebo/Vulkan**, which on Pi 5 V3D fails `VK_ERROR_OUT_OF_HOST_MEMORY` allocating swapchain. We tried mpv before VLC and hit this — abandoned.
- **`Gst.parse_bin_from_description` can't parse `(memory:GLMemory)` caps features** — parens collide with bin-grouping. Build those bins programmatically.
- **`GstStructure` field names can include `[]` but Python can't put a vec2 value into a bracket-named field via `set_value`**. That's why the truchet snake uses 16 individual `snake_N` uniforms.
- **The previous agent did multiple tries-then-reverts on the GStreamer-for-clips path.** The orphan-mpv commits in the reflog (`07c52d7`, `b5807b7`, `6f6c7f2`, `ae031e7`) used to work. The current state (commit `977b6df`) is on the VLC path.

## Recent commits (last ~10)

```
977b6df Safety: stop auto-fullscreen, ensure player has panic-quit keys
380f762 Swap mpv for VLC (python-vlc) — the dev-blessed Pi 5 path
86fa30a mpv: force OpenGL backend (Pi 5's Vulkan swapchain alloc fails)
cb97e45 mpv: pick projector output by physical size, gate fullscreen on n_monitors≥2
3bf11bf mpv hybrid fixes: hide output window in clip mode + fullscreen mpv
d9b279c Hybrid: mpv for clips (CPU but bulletproof), GStreamer for generators
4575d8d Downstream: revert to CPU videoconvert (GL path stalls V3D to 1 FPS)
ce2a04d Easy button: clips → MJPEG, downstream → GL-side BGRA
1e0227c Revert "Clip playback: decodebin3 + DMABuf zero-copy → glupload (CPU 98% → ~10%)"
11ae6da Revert "Clip source bin: GstStream-based pad-added filter (decodebin3 fix)"
```

## Operator's first ask of you

**Get clip playback working AND ensure operator can always exit, without losing the generator stack.** Then move on to features (favourites grid, FX chain, mapping mode — see `CLAUDE.md` phase table). Don't iterate forever on CPU optimization.

## Test plan

1. `python3 main.py --single-screen` — should boot, show the HUD on the operator screen, and a small VLC window with the first clip looping.
2. `python3 main.py` (no flag) — should boot for the dual-screen setup. **Confirm with operator visually** that the VLC window opens on the projector before assuming it does.
3. Press `[` / `]` — cycle generators. Should swap from VLC to GStreamer output_window and back.
4. Press `F` — toggle player fullscreen. **First check with operator that the player is on the right screen** before suggesting they hit this.
5. Press `Esc` (HUD) or `q` (VLC window) — panic-exit must always work.

## Reference

- Adafruit Pi Video Looper 2 — github.com/adafruit/pi_video_looper2 — proven Pi 5 video looping pattern in ~80 lines of python-vlc.
- `CLAUDE.md` at repo root — long-form project context, V3D gotchas, deferred features.
- Memory at `~/.claude/projects/-home-multitech-Desktop-VJ/memory/` — accumulated lessons. `search-first-on-problems.md` and `no-wasting-time.md` are especially load-bearing.

Good luck. Operator deserves you not wasting their time.
