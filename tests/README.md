# GPU spikes & diagnostics

Two kinds of thing live here:

1. **The current spikes** (`spike_a_*`, `spike_b_*`) — small, decisive
   experiments on the **production stack** (SDL2 `_sdl2` Renderer +
   GStreamer), built to answer the two open questions blocking a
   GPU-first / 4K-cinematic direction. **Start here.**
2. **The legacy moderngl diagnostics** (`test_01_*` … `test_07_*`) —
   recovered from the abandoned single-process moderngl attempt
   (reverted in commit `a7c2e48`). Kept for reference and for the V3D
   lessons baked into them. They need `pip install moderngl`, which is
   **not** part of the current rig.

---

## The two spikes that matter

### Why these exist (the short version)

The project's whole GPU history died on one bug: **two GL contexts in one
process corrupt each other on V3D** (a surface goes solid black). The fix
has always been *one GL context per process*. The app's `--gpu-scale`
mode already applies that to **output**: the projector becomes a single
SDL2 GPU renderer (hardware-scales the canvas to 2K/4K), the HUD stays
software. So output is, in principle, already solved.

What's still unproven — and what these spikes test:

| Spike | Question it answers |
|---|---|
| **A — dual-screen survival** | Does the `--gpu-scale` window layout (GPU renderer on the projector + software HUD on the operator screen) actually hold on *this* Pi, with nothing else running, no surprise blackout? |
| **B — 4K HEVC throughput** | Can the Pi **hardware-decode** 4K H.265 and get those frames onto the projector at 30 fps? This is THE gate on real 4K detail — `--gpu-scale` only upscales; the decoded frame itself has to be 4K, and HEVC is the Pi 5's only hardware-decoded codec. |

If A passes and B holds ~30 fps with a **hardware** decoder, the
4K-cinematic-with-mapping direction is real and we plan the build with
confidence. If B only works with a software decoder or drops frames, we
fall back to a separate, more modest cinematic mode.

### Spike A — dual-screen survival

```bash
# On the Pi, two displays (projector = display 1, operator screen = 0):
./venv/bin/python tests/spike_a_dualscreen.py \
    --output-display 1 --control-display 0 --fullscreen --seconds 60

# Quick single-screen smoke test:
./venv/bin/python tests/spike_a_dualscreen.py
```

**Report back:** did either window ever go black/freeze? The final
`[spike-a]` OUTPUT/CONTROL fps lines. Any error lines.

(The full-app equivalent is just running `Start VJ.sh` with `--gpu-scale
--control` — the spike is the isolated version so a failure can't hide
behind the engine.)

### Spike B — 4K HEVC decode throughput

First make a 4K HEVC test clip (one-time, software encode, ~minutes):

```bash
./tests/make_4k_test_clip.sh                  # 20s synthetic 4K HEVC
# or transcode something you already have:
./tests/make_4k_test_clip.sh path/to/clip.mp4
```

Then measure:

```bash
./venv/bin/python tests/spike_b_4k_decode.py \
    --clip tests/4k_hevc_test.mp4 --output-display 1 --fullscreen

# Force SOFTWARE decode for an A/B comparison:
./venv/bin/python tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 \
    --decoder avdec_h265

# Decode-only (no window, pure decoder throughput):
./venv/bin/python tests/spike_b_4k_decode.py --clip tests/4k_hevc_test.mp4 \
    --mode decode
```

It runs three measurements — **decode-only**, **decode+upload**,
**decode+present** — so we can see exactly where any cost is, and it
prints **which decoder GStreamer plugged**.

**Report back:** the `[spike-b] decoder plugged:` line (HARDWARE vs
SOFTWARE), the fps RESULT line for each mode, the decoded frame size
(should be 3840x2160), and any error lines.

---

## Legacy moderngl diagnostics (reference only)

Each `test_0N_*.py` exercises ONE thing in a single-process moderngl GL
context, in order, so the first FAIL points at the broken subsystem.
They need `pip install moderngl` and are the scaffolding from the
*reverted* GPU attempt — useful to read for the V3D notes (RGBA8-only
FBOs, explicit attribute locations, RGBA-only readback) but not part of
the current architecture.

```bash
./tests/run_all.sh        # runs test_01 … test_07, logs to tests/output/
```

| Test | Checks | If it fails… |
|---|---|---|
| `01_context` | GL context creation, version queries | no usable GL context at all |
| `02_clear_screen` | clear to default framebuffer | basic GL output broken |
| `03_clear_fbo` | clear to an offscreen RGBA8 FBO | FBO path broken on V3D |
| `04_noattrib_quad` | fullscreen quad via `gl_VertexID` | rasterisation broken |
| `05_attrib_quad` | same quad via vertex attribute | **04 ok + 05 fail = V3D attribute bug** |
| `06_fbo_shader` | procedural shader → FBO | the generative path |
| `07_texture_sample` | upload texture, sample in shader | the clip-frame path |

> Note: these rely on the real V3D driver. On a dev box running software
> Mesa (LLVMpipe) over Xvfb, `fbo.read()` returns mostly zeros even for a
> plain clear — that's an Xvfb/LLVMpipe quirk, not a real failure. Run on
> the Pi.
