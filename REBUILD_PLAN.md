# VJ-pi — GPU-first rebuild plan

> Status: PROPOSED. Written 2026-06-02 after the spike series proved 4K
> HEVC decode + zero-copy GL present + GPU mapping all work on the Pi 5.
> Nothing here is built yet. This is the plan to get sign-off on.

## 1. Why we're doing this

The current rig composites on the CPU (numpy/cv2). That caps the canvas at
~720p and means 4K clips are impossible through the FX/mapping path (a 4K
frame in the CPU = ~6 fps, measured). Tonight's spikes proved the GPU can
do the whole job:

| Gate | Proven result |
|---|---|
| Hardware HEVC 4K decode (`v4l2slh265dec`) | **162 fps** |
| Zero-copy 4K to screen (`glupload ! glcolorconvert ! glimagesink`) | **42 fps** |
| GPU mapping warp at 4K (`gltransformation`, 2-pass, unoptimised) | **28 fps** |
| One-GL-context-per-process is V3D-safe | proven (daily `--gpu-scale`) |

So the goal: **re-engineer the rig so all compositing — clips, generators,
FX, hits, mapping — happens on the GPU.** One unified pipeline where
"cinematic 4K" is just "a clip with no FX," and the trippy feedback
generators (the MilkDrop idea we started with) finally become possible.

## 2. Target architecture

Two processes, exactly like today's proven split — but the heavy lifting
moves across the boundary to the GPU:

```
┌─ COMPOSITOR process (ONE GL context — the V3D rule holds) ──────────┐
│  clip:  filesrc → v4l2slh265dec → (DMABUF) → GL texture             │
│  gen:   GLSL shader → GL texture                                    │
│  base layer (clip OR gen) → FX passes (GLSL) → hits/overlays        │
│         → mapping geometry warp → projector framebuffer (glimagesink│
│         or our own GL surface)                                      │
└────────────────────────────────────────────────────────────────────┘
            ▲ commands (stdin/JSON, like the generator worker today)
┌─ CONTROLLER process (GL-FREE — software, like main.py is now) ──────┐
│  keyboard → actions · Control HUD on the operator screen            │
└────────────────────────────────────────────────────────────────────┘
```

Why this is safe: the compositor has **one** GL context; the controller
has **none** (software HUD, exactly as the main process is GL-free today).
The dual-context V3D bug that killed past attempts cannot occur — it was
never "GPU is bad," it was always "two GL contexts in one process." This
is the *same* discipline that already works, just deepened.

## 3. Architecture — RESOLVED (Spike D passed, 2026-06-02)

Spike D confirmed on hardware: **GStreamer's `glshader` takes live,
externally-driven uniforms** (the element's `uniforms` property; verified
the picture responds live at ~30Hz from Python). So we do **not** need a
hand-rolled GL compositor for clips/FX/mapping — that whole risky path is
off the table. Stages 1–4 ride on the **proven GStreamer GL graph**:

- clip: `v4l2slh265dec ! glupload ! glcolorconvert`
- generator: `glshader` (existing `shader_catalog` shaders)
- FX: `glshader` passes with live uniforms (proven)
- compositing: `glvideomixer`
- mapping: geometry warp (`gltransformation`-style, single pass)
- output: `glimagesink` (proven 42 fps)

…all in ONE compositor process (one GL context — V3D-safe), driven by a
GL-free controller process over a pipe, exactly like the generator worker
today.

**The one genuinely-hard piece left is Stage 5** (feedback/MilkDrop
generators), because `glshader` is single-pass and can't sample its own
previous frame. That stage — and only that stage — needs custom GL FBOs,
and it gets its own spike when we reach it.

### The 4K fill-rate budget (a hard design rule, from Spikes B/C/D)

Measured: plain 4K present = 42 fps, but **every full-4K shader pass
roughly halves the framerate** (one glshader pass landed 19–28 fps).
That's V3D's fill rate over 8.3M pixels — the budget, not a bug. So:

- **Clean clip (no FX): full 4K, ~42 fps.** Cinematic stays gorgeous.
- **FX moments: run FX passes at reduced resolution** (1080p/720p), then
  upscale — exactly what the current rig's `--fx-render-scale` already
  does. 4× cheaper, and the chaotic FX moments don't need 4K detail.
- **Fuse effects into as few passes as possible** rather than stacking
  many full-screen passes.

Reserve full-4K passes for the clean present; everything else runs lean.

## 4. Incremental migration — the rig never breaks

Each stage ships and is usable. The **current CPU rig stays the default**
(its own launcher) until the GPU rig reaches parity and survives a real
event. No big-bang rewrite.

- **Stage 0 — Spike D.** Settle the clip→our-GL-texture question (§3).
- **Stage 1 — GPU compositor skeleton.** New compositor process: base
  layer = clip (4K GPU) OR generator (existing `shader_catalog` shaders),
  output to projector, driven by the existing `keymap`/controller over a
  pipe. Mapping = `gltransformation` placeholder. **Delivers: 4K clips +
  GPU generators + basic mapping, live.** Launch via a new flag; old rig
  untouched.
- **Stage 2 — FX chain to GLSL, one effect at a time.** Port `effects.py`
  effects to GL shader passes with real uniforms (intensity, params).
  Each ported FX ships independently; un-ported FX still run on the CPU
  path. This is the big lift — done incrementally so you're never stuck.
- **Stage 3 — Hits, overlays, favourites, autopilot.** Mostly control
  logic; wire into the compositor. Hits/overlays become GL passes.
- **Stage 4 — Real mapping.** Replace the `gltransformation` placeholder
  with proper corner-pin / mesh warp as a **single-pass geometry** warp
  (the lesson from Spike C: geometry warp, not per-pixel). `mapping.py`'s
  spaces/groups/state logic ports; only the warp execution changes.
- **Stage 5 — The payoff: feedback generators.** With a GL compositor and
  FBOs in hand, add a ping-pong feedback buffer and write MilkDrop-style
  generators that warp their own previous frame. This is the trippy stuff
  you opened with — it falls out almost for free once the compositor
  exists.
- **Stage 6 — Retire the CPU path** once the GPU rig is at parity and
  proven live.

## 5. The honest hard parts

- **Porting `effects.py` → GLSL is the real work** (weeks, not days). Many
  effects are numpy/cv2 idioms that need re-expressing as shaders.
  Mitigated by doing it one effect at a time with the CPU path as
  fallback.
- **Clip→our-GL-texture zero-copy** is the riskiest unknown (Spike D). If
  it fails, the fallback architecture is less elegant but still works.
- **Live pipeline control** on the GPU (switching clips, toggling FX) must
  stay glitch-free — easier in a custom GL compositor than a GStreamer
  graph, which is part of why we lean that way.
- **moderngl note:** moderngl "failed" before only because it ran a second
  GL context alongside pygame's in ONE process. In its own process with
  ONE context (as the old `gpu.py` standalone-EGL did), it's fine. We are
  not resurrecting the thing that broke.

## 6. How we avoid repeating history

- Spike the riskiest unknown (D) before committing — proven approach.
- Keep the working CPU rig as the default until GPU is at parity.
- No destructive git ops; clips/images stay gitignored.
- Ship every stage; test on the real Pi each step (edit → push → pull →
  run → observe).

## 7. Immediate next step — Stage 1 begins

Spikes are done; architecture is locked (§3). Stage 1 first milestone:

1. **`gpu_compositor.py`** — a new compositor process (system python3 /
   gi, like `gpu_generator_worker.py`): builds the GStreamer GL graph
   `clip-or-generator → glshader(FX, passthrough at first) →
   glimagesink(projector)`, reads JSON commands on stdin (set source,
   next/prev clip, set generator, set FX uniform), rebuilds the source
   branch on switch (acceptable latency, same as the generator worker).
2. **A minimal launcher** to run it fullscreen on the projector and cycle
   the real 4K clip library from the keyboard — proving the unified GL
   pipeline plays the actual library at 4K with live switching.
3. Then layer in: generators as a source, the FX uniform wired to a key,
   the `gltransformation` mapping pass.

The current CPU rig stays the default (its own launcher) the entire time.
Build → push → pull → run on the Pi → observe, one slice at a time.
