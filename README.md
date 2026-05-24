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

### Switching the output display on the fly

The HUD has an **OUTPUT DISPLAY** picker — click one of the `Display N`
buttons to choose a target, then click **APPLY** to move the fullscreen
output. Handy if your projector ends up on a different HDMI port than
expected (no need to restart or edit `Start VJ.sh`).

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
| `1 2 3 4 5 6 7 8 9 0` | Pick base clip from `assets/clips/` (slot 1-10)   |
| `Q W E R T Y U I O P` | Toggle overlay from `assets/overlays/` (slot 1-10) |
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
| `Space`             | Blackout toggle (panic button)                     |
| `Backspace`         | Freeze frame toggle                                |
| `Esc`               | Kill all FX, overlays, hits — back to clean base   |
| `Shift+Esc`         | Quit                                               |

Press a clip key (1-0) and a generative key (A/S/D) — last one wins.
Press an overlay key once to turn it on, again to turn it off.

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
