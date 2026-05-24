# pi-paint VJ

Manual VJ rig for Raspberry Pi 5 + mini wireless keyboard + projector. A
standalone pygame app — it does not import from the rest of pi-paint, so
it can be lifted into its own repo at any time.

This is **Phase 1**: manual mode only, no audio. Phase 2 (aubio beat
detection + auto mode) and Phase 3 (Hailo person-matte) are planned but
not built yet.

## Quick start (no terminal needed)

On the Pi, open the file manager and navigate to this `vj/` folder.
Then double-click these files in order:

1. **`setup.sh`** — choose **"Execute in Terminal"** when prompted.
   Installs SDL2/OpenGL system libs, creates a Python virtualenv,
   installs pygame + opencv + numpy. Takes 2-5 minutes the first time.
   Safe to re-run; subsequent runs are a no-op.

2. Drop `.mp4` files into `assets/clips/` and `assets/overlays/` using
   the file manager (drag and drop works). See the READMEs in those
   folders for what to put there.

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
  click `APPLY`. (In fullscreen mode some window managers yank focus
  back to the projector when you click the HUD — use the keyboard
  shortcuts instead if that happens.)

In **windowed test mode** the picker moves the output window
immediately. In **fullscreen mode** the picker can't reliably move a
running fullscreen window (pygame/SDL pins it to its original monitor
on most setups, and aggressive workarounds tend to hang or close the
window). Instead, the apply just **persists the choice to
`vj_state.json`** and shows a `⚠ Shift+Esc and re-launch` hint in the
HUD — the next launch starts fullscreen on the new monitor. The
launcher script's `OUTPUT_DISPLAY` only seeds the very first run; the
HUD picker wins after that.

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

# Bump rendered resolution
./venv/bin/python main.py --width 1280 --height 720 --fullscreen --output-display 1
```

## Keyboard map

The layout matches a standard QWERTY keyboard. Designed for the Tosuny /
Rii mini wireless keyboards (~70 keys + trackpad).

| Keys                | Action                                              |
|---------------------|-----------------------------------------------------|
| `−` / `=`           | Cycle CLIP library (prev / next, auto-repeat on hold) |
| `[` / `]`           | Cycle OVERLAY library (prev / next, auto-repeat on hold) |
| `1`–`0`             | Clip favourite slots (10). Tap → play. **Hold ≥ ½ s → assign the currently-playing clip to that slot.** |
| `Q`–`P`             | Overlay favourite slots (10). Same tap/hold pattern. |
| `A S D F G H`       | Generative base: plasma / tunnel / starfield / warp / waves / cells (toggle) |
| `J K L`             | Generative base: lissajous / moiré / metaballs (toggle) |
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
| `← →`               | Adjust PARAM X (active-FX horizontal control)      |
| `↑ ↓`               | Adjust PARAM Y (active-FX vertical control)        |
| `F11`               | Cycle the pending output display                   |
| `F12`               | Apply the pending output display (and persist it)  |
| `Space`             | Blackout toggle (panic button)                     |
| `Backspace`         | Freeze frame toggle                                |
| `Esc`               | Panic: clear FX, overlays, hits, blackout/freeze. **Keeps the current clip playing** so you never drop to black unexpectedly. |
| `Shift+Esc`         | Quit                                               |

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

## Asset sources

Free libraries (download once, no internet needed at the party):

- **Base clips:** [Beeple VJ Loops](https://www.beeple-crap.com/vjloops),
  [Mantissa CC0 4K](https://mantissa.xyz/vj.html),
  [Videezy VJ Loops](https://www.videezy.com/free-video/vj-loop)
- **Overlays (fire, sparks, lasers, lens flares):**
  [Videezy Spark Overlays](https://www.videezy.com/free-video/spark-overlay),
  [Vecteezy Sparks](https://www.vecteezy.com/free-videos/sparks-overlay)

For overlays: pick footage that's already pre-keyed against a **black**
background. The compositor uses screen-blend, so anything bright pops
through and the black drops out. Don't bother with true alpha-channel
video on Pi — the hardware decoder doesn't like it.

Recommended pre-processing (one-time, on a desktop):

```bash
# Downsample to projector resolution + re-encode to H.264 for hardware decode
ffmpeg -i input.mp4 -vf scale=854:480 -c:v libx264 -preset slow -crf 22 -an output.mp4
```

…or just drop the raw files into `assets/clips/` and `assets/overlays/`
and double-click **`assets/Process Assets.sh`** — it scans both folders,
normalises anything that isn't already H.264 / target resolution /
audio-free, stashes the originals in a `_originals/` sub-folder, and
skips files that are already good (so you can re-run it any time).
Clips are scaled + centre-cropped to fill the frame; overlays are
scaled and pad-letterboxed with black (which the screen-blend
compositor treats as transparent).

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
effects.py     Generative + transformative numpy/OpenCV effects
clips.py       ClipPool: lazy MP4 loader keyed by slot index
keymap.py      Pygame key → engine action dispatch table
config.py      Config dataclass
```

Render pipeline each frame:

```
base layer   →  active clip OR active generative OR black
   ↓
FX chain     →  kaleido, mirror, rgb_split, posterize, edges, invert, feedback
   ↓
overlay      →  screen-blend the active overlay clip
   ↓
hits         →  transient strobe/flash/punch (5 frames)
   ↓
blit to pygame screen
```

`prev_frame` is captured before the next render so feedback works.

## Performance notes

- Targets 30 fps at 854×480 on Pi 5. Generatives are vectorized numpy.
- `kaleidoscope` is the heaviest effect (per-pixel remap). Stack 2-3
  effects max for headroom.
- MP4 decode uses OpenCV's `VideoCapture` — relies on libavcodec; on
  Pi 5 it does software H.264 decode but stays well under a frame budget
  for 854×480.
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
