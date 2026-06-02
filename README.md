# pi-paint VJ

Manual VJ rig for Raspberry Pi 5 + mini wireless keyboard + projector.
The main app is the older working pygame/OpenCV compositor for clips,
FX, favourites, autopilot, and projection mapping. Procedural generators
come from a separate GStreamer/GL worker process so shader work runs on
V3D without bringing the broken VLC/Gtk window path back.

## Quick start (no terminal needed)

On the Pi, open the file manager and navigate to this `vj/` folder.
Then double-click these files in order:

1. **`setup.sh`** — choose **"Execute in Terminal"** when prompted.
   Installs SDL2/OpenGL/GStreamer system libs, creates a Python
   virtualenv, installs pygame + opencv + numpy. Takes 2-5 minutes the first time.
   Safe to re-run; subsequent runs are a no-op.

2. Drop `.mp4` files into `assets/clips/`. Drop `.jpg` / `.png` images
   into `assets/images/`; the donut generator cycles through those as
   textures each time you return to it.

   For clean high-detail playback, drop raw cinematic video files into
   `assets/4k/`, then double-click **`Process 4K Assets.sh`** once. The
   file can be 4K, 2K, or any other high-detail source; the processor
   writes Pi 5 GPU-playable HEVC clips into `assets/4k/processed/`.

   For portrait/vertical clips, drop them into `assets/portrait/`, then
   double-click **`Process Portrait Assets.sh`**. It creates 1920×1080
   landscape versions in `assets/portrait/landscape/`.

3. To launch, double-click one of these scripts and choose **"Execute"**:
   - **`Start VJ.sh`** — **dual display** (the main mode). Opens a
     control HUD on the small screen (display 0) showing live preview,
     current state, and the key cheat sheet; sends the visual output
     fullscreen to the projector (display 1).
   - **`Test (single screen).sh`** — both windows on the primary
     display, no fullscreen. Use this when no projector is connected.

4. (Optional) Double-click **`Install Desktop Shortcuts.sh`** to drop
   launcher icons on your desktop so you don't have to navigate into
   this folder every time.

### Display layout

The default `Start VJ.sh` assumes:

| Display index | Role          | What's shown                              |
|---------------|---------------|-------------------------------------------|
| `0` (primary) | Small screen  | Control HUD: preview, state, key map      |
| `1` (secondary) | Projector   | Fullscreen visual output                  |

If your displays are wired up the other way, open `Start VJ.sh` in a
text editor and swap `OUTPUT_DISPLAY` and `CONTROL_DISPLAY` at the top.

### Keyboard focus

The keyboard sends keys to whichever window has focus — click into the
**control HUD** window once when you start a set and leave it focused.
Live FX parameters are tuned with the **arrow keys** (← → for PARAM X,
↑ ↓ for PARAM Y) so you don't have to fight the trackpad/mouse cursor
mid-set. The current values show as bars in the HUD.

### Switching the output display

The HUD has an **OUTPUT DISPLAY** picker. Two ways to drive it:

- **Keyboard:** `F11` cycles the pending display, `F12` applies the pick.
  Works from either window because keyboard focus reaches the engine
  regardless of the WM's mood.
- **Mouse:** click a `Display N` button to set the pending display, then
  click `APPLY`. In fullscreen mode some window managers may yank focus
  back to the output; the keyboard shortcuts always work.

Switching is **live** in both windowed test mode and fullscreen mode.
Under the hood "fullscreen" is implemented as a borderless window
(`NOFRAME`) sized to fill the chosen display — this sidesteps the
[long-standing SDL2 bug](https://github.com/libsdl-org/SDL/issues/3192)
where `SDL_WINDOW_FULLSCREEN` can't be reliably retargeted between
monitors. The output covers the full display the same way a true
fullscreen window would.

The applied choice is **persisted to `vj_state.json`** and used by every
subsequent launch. The launcher script's `OUTPUT_DISPLAY` only seeds
the very first run; the HUD picker wins after that.

### If "Execute" doesn't appear

Raspberry Pi OS file manager → **Edit → Preferences → General** →
turn ON **"Don't ask options on launch executable file"** (then a
single double-click runs them).

If that's not what you want, the dialog that pops up on double-click
has the right option — pick **"Execute"** for the Run scripts and
**"Execute in Terminal"** for setup so you can see install progress.

## Terminal install / launch (if you prefer)

```bash
cd vj
./setup.sh                                # same as the double-click

# Dual display — control HUD on display 0, fullscreen output on display 1
./venv/bin/python main.py --fullscreen --output-display 1 \
                          --control --control-display 0

# Single window, no HUD
./venv/bin/python main.py

# Lighter fallback if 1080p FX load is too high
./venv/bin/python main.py --width 1280 --height 720 --fullscreen --output-display 1
```

## Keyboard map

The layout matches a standard QWERTY keyboard. Designed for the Tosuny /
Rii mini wireless keyboards (~70 keys + trackpad).

| Keys                | Action                                              |
|---------------------|-----------------------------------------------------|
| `−` / `=`           | Cycle CLIP library (prev / next, auto-repeat on hold) |
| `[` / `]`           | Cycle GPU generator catalogue (prev / next, auto-repeat on hold) |
| `1`–`0`             | Clip favourite slots (10). Tap → play. **Hold ≥ ½ s → assign the currently-playing clip to that slot.** |
| `A S D F G H J K L ;` | Generator favourite slots. Tap → play. **Hold ≥ ½ s → assign the currently-playing generator to that slot.** |
| `Z`                 | HIT: strobe flash                                  |
| `X`                 | HIT: black flash                                   |
| `C`                 | HIT: invert flash                                  |
| `V`                 | HIT: zoom punch                                    |
| `B`                 | HIT: RGB smash                                     |
| `F1`                | FX: kaleidoscope (segments = PARAM X)              |
| `F2`                | FX: horizontal mirror                              |
| `F3`                | FX: feedback / trails (PARAM X = zoom, Y = rotate) |
| `F4`                | FX: invert (persistent)                            |
| `F5`                | FX: posterize (levels = PARAM Y)                   |
| `F6`                | FX: edge detect                                    |
| `F7`                | FX: RGB split / chromatic aberration (offset = PARAM X) |
| `F8`                | FX: melt — warp the base layer with a generator's colour field (kaliset by default; PARAM X = shimmer → full liquefy). Live mode only. |
| `← →`               | Adjust PARAM X (active-FX horizontal control)      |
| `↑ ↓`               | Adjust PARAM Y (active-FX vertical control)        |
| `Enter Enter`       | Engage **AUTOPILOT** (double-tap within 600 ms). Any other key takes over again and executes immediately. While engaged, `↑/↓` tune the clip-change rate and `←/→` tune the FX-change rate. |
| `F11`               | Cycle the pending output display                   |
| `F12`               | Apply the pending output display (and persist it)  |
| `Space`             | Blackout toggle (panic button)                     |
| `Backspace`         | Freeze frame toggle                                |
| `M`                 | Toggle **MAPPING mode** (see section below)        |
| `N`                 | Toggle **4K CINEMATIC mode**. While active, `−` / `=` move through the 4K playlist; `Esc` exits cinematic mode. |
| `Esc`               | Panic: clear FX, hits, blackout/freeze. **Keeps the current clip _or_ generator playing** so you never drop to black unexpectedly. |
| `Shift+Esc`         | Quit                                               |

## Projection mapping mode (press `M`)

Mapping mode lets you carve the output into named **spaces** (quadrilaterals
on the projected frame), tie multiple spaces together into a **group** so a
single set of controls drives them all symmetrically, and stack multiple
groups so each one runs its own content + autopilot loop. Setup persists in
`vj_state.json` between sessions.

There are two sub-modes:

- **EDIT** — mouse-first. Drag rectangles in the HUD preview to create
  spaces, click to select, drag to move, drag corners to reshape, bind
  spaces together. No content gets assigned in this sub-mode (content / FX
  / favourite keys are swallowed) so you can focus on layout.
- **PERFORM** — keyboard-first. The keys you already know
  (`1-0`, `Q-P`, `A-L`, `F1-F7`, `←→↑↓`, `−/=`, `[/]`) target the currently
  selected group. `Tab` cycles between groups.

Press `M` to enter / leave mapping mode entirely. Press `E` to switch
between EDIT and PERFORM inside it. The first time you enter mapping mode
the rig drops you straight into EDIT with a blank canvas; later launches
resume in PERFORM with your saved layout.

### Edit sub-mode (mouse-first)

Edit gestures work in **both** the HUD preview AND on the projector
itself — pointing at physical features through the projection is the
natural way to do projection mapping.

| Gesture                | Action                                          |
|------------------------|-------------------------------------------------|
| Click + drag empty area | Rubber-band a rectangle → becomes a new space in a new group |
| Click a space's body    | Pick that space (its group becomes the active group; corner handles + hover toolbar appear) |
| Click + drag a space's body | Move the whole space                        |
| Click + drag a corner handle | Reshape that corner of the picked space    |
| Hover any space         | A small toolbar sprouts above its bounding box  |

The hover toolbar replaces the old keyboard-only editing commands. Each
button is a 4 %-of-canvas chip drawn just above the space:

| Button | Appears on              | Action                                  |
|--------|-------------------------|-----------------------------------------|
| **×**  | Every space             | Delete this space (group goes too if it was the last) |
| **+**  | Hovered space that's in a different group from the picked one | Bind this space into the picked space's group (source group is deleted if it becomes empty); selection stays on the originator so you can chain binds |
| **⊘**  | Picked space whose group has ≥ 2 spaces | Unbind this space into its own new group |
| **G·n** | Every space            | Group-membership chip — also tappable to pick this space |

### Edit sub-mode (keyboard fallbacks)

| Key                 | Action                                            |
|---------------------|---------------------------------------------------|
| `E`                 | Leave EDIT sub-mode (back to PERFORM)             |
| `Esc`               | Cancel any in-flight drag / deselect              |
| `Tab` / `Shift+Tab` | Cycle the active group (same as PERFORM)          |
| `M`                 | Leave MAPPING entirely                            |
| `Shift+click` other space | Bind into the picked space's group (same as toolbar **+**) |
| `B`                 | Arm bind — next plain click binds (alternative to Shift) |
| `U`                 | Unbind picked space (same as toolbar **⊘**)       |
| `Delete`            | Delete the picked space (same as toolbar **×**)   |

### Per-group frame controls (FRAME panel in the HUD)

A group's content plays **once, across the whole canvas**, and the group's
spaces are holes through which it shows. Two side-by-side spaces in one
group reveal the left and right portions of the same playing video —
not two copies of it. That's how you build "many windows, one underlying
video" compositions and use spaces as physical layers.

The video keeps its natural aspect — no warp distortion. Click the FRAME
panel buttons in the selected group's status panel to compose how the
video sits on the canvas:

| Control     | Action                                                    |
|-------------|-----------------------------------------------------------|
| Mode pill   | Click to cycle: `window` → `fit` → `fill` → `stretch`. `window`, `fit`, `fill` all use the canvas as the playback surface and reveal it through the group's spaces. `stretch` is per-space — each quad warps its own copy of the video (the old billboard look, opt in when you want the angled-perspective distortion on purpose). |
| `−` / `+`   | Zoom the video ±15 % per click (window mode)              |
| `RESET`     | Zoom 1.0, pan 0,0                                          |
| ◀ ▲ ▼ ▶     | Pan the video ± 10 % of the canvas half-size per click — all spaces in the group shift in sync because they're windows onto the same plane |

Frame settings are saved per group in `vj_state.json` so the composition
you set up for a show is there again the next time you launch.

### Perform sub-mode + global mapping ops

| Key                 | Action                                              |
|---------------------|-----------------------------------------------------|
| `Tab` / `Shift+Tab` | Next / previous group (the active group's spaces get a coloured border on the projector) |
| `1-0` `Q-P` `A-L` `F1-F7` `←→↑↓` `−/=` `[/]` | Apply to the SELECTED group only — each group keeps its own state |
| `Ctrl+N`            | New group                                          |
| `Ctrl+Backspace`    | Delete the current group                           |
| `Ctrl+=` / `Ctrl+-` | Add / remove a space in the current group          |
| `Ctrl+G`            | Cycle pre-baked grid layouts (1·2x1·1x2·2x2·3x2·3x3·4x2·4x3) for the current group |
| `Ctrl+A`            | Toggle **autopilot** on the current group          |
| `Ctrl+K`            | Cycle autopilot kind: cycle/random clips, cycle/random generatives |
| `Ctrl+,` / `Ctrl+.` | Autopilot interval ± 1 second (default 8 s)        |
| `Ctrl+B`            | Toggle the on-projector selection border on/off    |
| `Ctrl+C`            | Cycle border colour (light gray / cyan / amber / violet / mint / red-pink — never white) |
| `Ctrl+[` / `Ctrl+]` | Border intensity ± 10 %                            |
| `Ctrl+;` / `Ctrl+'` | Border thickness ± 1 px                            |

Pitfalls worth knowing about:

- Border colour defaults to a **light gray** (180,180,180) — not white —
  with intensity at 100 %. Drop it via `Ctrl+[` / `Ctrl+]` if even light
  gray is too bright on your surface.
- Spaces are stored in **normalized 0..1 coordinates** so the mapping
  survives resolution changes, display switches, and Pi-OS-fontsize-
  induced surprises.
- Each group has its own random **time offset** so two groups running the
  same generative don't visually lock-step.
- `cv2.warpPerspective` is the bottleneck; we crop to the quad bounding
  box and apply a convex-poly mask so unmapped pixels stay pure black (no
  spill onto the wall). If you run >12 groups with different clips, the
  per-pool LRU may thrash — the live show should stay well below that.

### Browsing big libraries + favourites

Drop hundreds of clips into `assets/clips/`. Then:

1. **Cycle** through the library with `−` / `=` (clips) or `[` / `]`
   (overlays). Hold a key to auto-scrub at ~12/sec.
2. When you land on something you like, **long-press** any number key
   (`1`–`0`) for ~½ second. That clip gets assigned to that favourite
   slot. Do the same with `Q`–`P` for overlays.
3. **Tap** a number key (or `Q`–`P`) any time after that to instantly
   recall the assigned clip/overlay.
4. Favourites persist in `vj_state.json` between sessions — saved by
   filename stem, so re-ordering or processing files won't break them
   (only renaming does).
5. To clear a slot: cycle to a clip-free state first (or long-press `0`
   to clear that slot's binding, then cycle past), then long-press the
   slot you want to wipe. `Esc` keeps the clip playing on purpose, so
   it won't clear slots by itself.

The HUD shows two rows of 10 chips with the assigned stems, with the
slot whose clip is currently playing highlighted blue.

Position shows in the HUD as `name  [47/152]`. Open VideoCapture
handles are LRU-evicted (12 at a time) so a huge library doesn't
bloat memory.

Pick a clip then a generative (`A`-`L`) — generative wins until you
pick another clip.

### Autopilot

Double-tap **Enter** (within 600 ms) to engage autopilot. The engine
then drives itself:

- Picks a random clip (sometimes a generative instead) every
  ~`auto_clip_interval` seconds, with jitter.
- Toggles a random FX every ~`auto_fx_interval` seconds (caps active
  FX at 3).
- Drifts PARAM X/Y toward fresh random targets every couple of seconds.
- Occasionally swaps the overlay (or clears it).

Autopilot **never fires the punch-in hits** (strobe / black flash /
invert flash / zoom punch / RGB smash). Those are seizure / migraine
risks and only the operator should trigger them — `Z X C V B` are
always your call.

While autopilot is engaged, the HUD shows a green **AUTOPILOT** badge
with the current rates. The arrow keys retune the rates instead of
PARAM X/Y: `↑/↓` make clip changes faster/slower, `←/→` make FX
changes faster/slower.

**Any other key press** (Z for a strobe, a `5` to recall favourite 5,
F3 to switch on feedback, …) immediately disengages autopilot and
performs the action — perfect for grabbing back control when the
random output happens to land on something you want to embellish.

## Asset sources

Free libraries (download once, no internet needed at the party):

- **Base clips:** [Beeple VJ Loops](https://www.beeple-crap.com/vjloops),
  [Mantissa CC0 4K](https://mantissa.xyz/vj.html),
  [Videezy VJ Loops](https://www.videezy.com/free-video/vj-loop)
- **Overlays (fire, sparks, lasers, lens flares):**
  [Videezy Spark Overlays](https://www.videezy.com/free-video/spark-overlay),
  [Vecteezy Sparks](https://www.vecteezy.com/free-videos/sparks-overlay)

Overlay code still exists in the engine, but the controls are shelved
for now because the rig does not currently need overlay clips.

Recommended pre-processing (one-time, on a desktop):

```bash
# Downsample to projector resolution + re-encode to H.264 for hardware decode
ffmpeg -i input.mp4 -vf scale=854:480 -c:v libx264 -preset slow -crf 22 -an output.mp4
```

…or just drop the raw files into `assets/clips/` and double-click
**`Process Assets.sh`**. It shows a GUI progress window when available,
prints progress when run from a terminal, and writes the full log to
`vj_last_process.log`. Originals are preserved in `assets/clips/_originals/`.
Clips are scaled + centre-cropped to fill the frame.

### 4K cinematic mode

This is part of the main app: start **`Start VJ.sh`**, then press `N`.
The normal VJ canvas goes black and a separate GStreamer/GL video window
plays the `assets/4k/processed/` playlist with the fastest Pi 5 path:
HEVC hardware decode → GL upload/convert → `glimagesink`. No FX, no
mapping, no CPU frame copy.

On the Pi/labwc projector setup, run **`Apply Fullscreen Rule.sh`** once
if the cinematic video window does not jump to the projector fullscreen.

Controls while cinematic mode is active:

| Keys      | Action                         |
|-----------|--------------------------------|
| `N`       | Leave cinematic mode           |
| `Esc`     | Leave cinematic mode           |
| `−` / `=` | Previous / next 4K clip        |

Drop raw large files into `assets/4k/` and run **`Process 4K Assets.sh`**
before a set. It also works if launched from inside the `assets/` folder.
The processor writes HEVC/H.265 MP4 files capped at
3840×2160 and 30 fps into `assets/4k/processed/`, leaving the raw source
files in place. If no processed files exist, cinematic mode will try the
top-level `assets/4k/` files directly, but unsupported codecs will fail
or skip; processing ahead of time is the reliable path.

### Portrait to landscape conversion

Drop vertical videos into `assets/portrait/`, then run
**`Process Portrait Assets.sh`**. The output lands in
`assets/portrait/landscape/` as 1920×1080 H.264 MP4.

The conversion preserves the full portrait frame in the center and fills
the landscape side space with a blurred/dimmed copy of the same video, so
you get a proper 16:9 clip without cutting off the subject. Move the
finished files into `assets/clips/` for the regular VJ app, or into
`assets/4k/` and run **`Process 4K Assets.sh`** if you want them in the
cinematic playlist.

## Architecture

```
main.py        argparse + pygame init + main loop wiring; opens the
               output window via pygame.display + a second SDL2
               Window+Renderer for the control HUD
engine.py      Engine class: state, render pipeline, public actions.
               Splits compose_frame() (numpy) from blit_to_output()
               (pygame) so the same frame can feed both windows.
control.py     ControlWindow: preview, state badges, cached key
               cheat sheet. Renders to an offscreen Surface and
               uploads as a Texture each frame.
effects.py     Transformative numpy/OpenCV effects + CPU generator fallbacks
shader_catalog.py / gpu_generator_worker.py
               GStreamer/GL shader generators in a separate process
clips.py       ClipPool: lazy MP4 loader keyed by slot index
keymap.py      Pygame key → engine action dispatch table
config.py      Config dataclass
cinematic4k.py System-Python GStreamer/GL player for N-key 4K cinematic mode
```

Render pipeline each frame:

```
base layer   →  active clip OR active generative OR black
   ↓
FX chain     →  kaleido, mirror, rgb_split, posterize, edges, invert, feedback, melt
   ↓
hits         →  transient strobe/flash/punch (5 frames)
   ↓
blit to pygame screen
```

`prev_frame` is captured before the next render so feedback works.

## Performance notes

- Default render resolution is **1920×1080**. The output surface scales
  to the display via `pygame.transform.smoothscale` (bilinear) or the
  optional GPU scaler, so the same canvas can still present cleanly on
  the 4K projector. Clip downsampling uses `cv2.INTER_AREA` for clean
  anti-aliased shrinking and `cv2.INTER_LINEAR` for upscaling.
- **`Process Assets.sh` bakes every clip to the render
  resolution** so the per-frame `cv2.resize` is a no-op at playback —
  the only per-frame cost is the unavoidable H.264 decode and a BGR→RGB
  shuffle. The processor reads its target size from `config.py`, so
  the two stay in sync. If you change the render resolution (via
  `--width / --height` or by editing `config.py`), **re-run the
  processor** so your library is at the new size; otherwise the engine
  will live-resize every frame and print a one-time warning per file.
- If 30 fps starts dropping under heavy FX load, fall back to
  `--width 1280 --height 720` and re-run **`Process Assets.sh`**.
- **Generatives render at `--gen-render-scale × canvas` (default 0.5).**
  They're smooth procedural patterns — pixel-perfect rendering at canvas
  resolution is wasted CPU. At 0.5, a 4-group mapping setup with mixed
  FX cuts the generative pixel count by 4×, with no visual difference
  for plasma / waves / cells /
  moire / metaballs. Try `--gen-render-scale 0.33` if you need more
  headroom (almost-3× again); back off to 1.0 if you're driving a tunnel-
  style sharp checker pattern as the base layer and want pixel-perfect
  edges. Clips and overlays are unaffected — they keep their detail.
- `kaleidoscope` is the heaviest effect (per-pixel remap). Stack 2-3
  effects max for headroom.
- MP4 decode uses OpenCV's `VideoCapture` — relies on libavcodec; on
  Pi 5 it does software H.264 decode but stays well under a frame budget
  for 854×480.
- In **mapping mode** the per-group mask is cached by space-corner
  signature — a running set pays the `cv2.fillConvexPoly` cost once,
  not every frame. Edits invalidate the cache automatically. Dead
  groups get garbage-collected every ~5 s.
- Clip frames are read **once per render**, so don't try to play more
  than one clip slot simultaneously — only the most recently selected
  base and overlay are advanced.

## Not yet built (Phase 2/3)

- **Auto mode:** aubio beat detector listening on a USB mic, scenes
  swap on downbeats, hits fire on kick onsets, FX intensity scales
  with smoothed RMS energy.
- **Hailo person matte:** silhouette the dancer/DJ from the webcam,
  composite over the base — fire and lasers "shoot from hands."
- **Pre-baked Shadertoy MP4s:** bake favorite shaders offline to
  854×480 H.264 loops so the rig can play them back without live GLSL.
