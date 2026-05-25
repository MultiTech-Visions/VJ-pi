# GPU pipeline diagnostic tests

Each test exercises ONE thing. Run them in order; the first FAIL tells
us exactly which subsystem is broken so we can fix the right thing
instead of guessing.

Every test:
* Opens its own pygame window with the same GL attributes Start VJ.sh
  uses (GLES 3.0 via SDL).
* Does its one thing.
* Reads the rendered pixels back from the GPU.
* Saves a PNG to `tests/output/` so you can SEE what was actually
  drawn, not just trust a number.
* Prints `[PASS]` or `[FAIL]` plus diagnostics, then exits with
  status 0 or 1.

## Run everything in one shot

```bash
./tests/run_all.sh
```

That runs every test in sequence and writes the combined log to
`tests/output/run.log`. Send me that file plus the PNGs in
`tests/output/` and we'll have an actual map of where the pipeline
breaks.

## Run a single test

```bash
./venv/bin/python tests/test_01_context.py
```

Each test is fully standalone; you can run any of them in isolation.

## What each test checks

| Test | Checks | If this fails, the broken thing is… |
|---|---|---|
| `01_context` | moderngl can create a GL context, version queries return sensible data. | SDL / EGL / driver isn't giving us a usable GL context at all. |
| `02_clear_screen` | `ctx.clear(magenta)` on the default framebuffer produces magenta pixels. | Basic GL output to the screen is broken. (You'd see this in main.py too — strobe wouldn't work.) |
| `03_clear_fbo` | `ctx.clear(magenta)` on an offscreen FBO produces magenta pixels (readback). | FBO creation / clearing is broken on V3D — the main pipeline uses FBOs for everything. |
| `04_noattrib_quad` | Renders a fullscreen quad using `gl_VertexID` (no vertex attributes). | Shader rasterisation itself is broken. |
| `05_attrib_quad` | Renders the same quad using `layout(location=0) in vec2 in_pos`. | Vertex attribute binding is broken. **If 04 passes and 05 fails, that's our smoking gun.** |
| `06_fbo_shader` | Renders a procedural shader (plasma) into an FBO, reads back. | The exact path the main pipeline uses for generatives. |
| `07_texture_sample` | Uploads a known-good BGR texture, samples it in a shader, renders. | The exact path the main pipeline uses for clip frames. |

Test 04 vs 05 is the key comparison: if `gl_VertexID` based rendering
works but attribute-based rendering doesn't, V3D's GLES attribute
binding is the smoking gun and the fix is `glBindAttribLocation`
before link (or sticking with `gl_VertexID` in the main pipeline).

## Reading the PNGs

If a PNG is solid black when it should be magenta / plasma /
whatever, that step's draw never produced any fragments. If it's
the right colour but garbled (stripes, wrong region), the draw
happened but with wrong vertex data / wrong sampling. Either way
you can SEE the failure mode instead of just reading a number.

## Note on running locally on Xvfb / software Mesa

These tests rely on the real GPU driver responding correctly to
moderngl. On a headless dev machine running LLVMpipe (software
Mesa) over Xvfb, `fbo.read()` returns mostly zeros even for a
plain `ctx.clear` — a known Xvfb/LLVMpipe/GLES interaction that
has nothing to do with V3D. The tests are written to run on the
actual Pi 5 hardware; treat any local LLVMpipe results as "the
test compiled and ran", not as a reference for what `[PASS]`
should look like.
