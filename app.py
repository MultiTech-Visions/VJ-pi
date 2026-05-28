"""GTK3 application + GStreamer pipeline.

Architecture (phase 4a — swappable source, stable downstream):

  source_bin (rebuilt per source change):
    CLIP:       filesrc ─ decodebin ─ videoconvert ─ videoscale ─ glupload ─[ghost src]
    GENERATOR:  videotestsrc-black ─ glupload ─ glshader[GLSL]   ─────────────[ghost src]
                                                  GL texture, 1280×720
                                                       │
                                                       ▼
  downstream_bin (lives for the whole session):
                tee ─┬─▶ queue ─ gldownload ─ videoconvert ─ gtksink (output)
                     └─▶ queue ─ gldownload ─ videoconvert ─ gtksink (HUD)

Both source-bin variants output the same caps (GL texture at canvas
resolution), so we can tear down and rebuild the source side without
disturbing the GL context that the downstream owns. That avoids both
the glshader recompile cost on each source change AND the
gtkglsink-style dual-context bug we already know about on V3D.

This is the layer the rest of phases 4–6 will build on:
  4a — source switching (clips + generators) via keyboard  ← here
  4b — overlay layer via compositor
  4c — favourites grid (tap/hold on 1-0 and Q-P)
  5  — mapping mode (groups, spaces, fit modes)
  6  — FX chain, hits, autopilot, polish

GTK3 + gtksink because Pi OS Bookworm's apt doesn't carry
gst-plugins-rs (where gtk4paintablesink lives). Single GL context
through gstreamer's gl* family. CPU presentation via gldownload
before the gtksinks — see CLAUDE.md for the V3D-dual-context
post-mortem that justifies all of this.
"""
import math
import os
import subprocess
import sys
import time as time_mod
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gst, Gdk, Gio, GLib  # noqa: E402


HERE = Path(__file__).resolve().parent
CLIPS_DIR = HERE / "assets" / "clips"
IMAGES_DIR = HERE / "assets" / "images"

# Canvas resolution. Source bins normalise to this before the
# downstream tee, so the GL texture flowing through is always
# 1280×720 regardless of clip resolution.
CANVAS_W = 1280
CANVAS_H = 720

# Downstream bin — stable for the lifetime of the app. tee forks
# the GL texture by refcount; each branch downloads to CPU just
# before its gtksink (gtksink doesn't accept GLMemory). The
# downstream's ghost sink pad accepts GL textures, so source-bin
# replacement is a clean unlink/link without touching GL state.
# Single-branch downstream: just the projector output. The HUD
# preview was using ~30% CPU just to render a tiny preview that
# the operator barely glances at — replaced with a text-only
# status panel on the HUD window. The `tee` is kept anyway so
# Phase 6 effects can fork the GL textures without restructuring
# the pipeline.
DOWNSTREAM_DESC = (
    "tee name=t allow-not-linked=true "
    "t. ! queue max-size-buffers=2 leaky=downstream ! "
    "  gldownload ! videoconvert ! "
    "  gtksink name=output_sink sync=true"
)
# sync=true on the gtksink: render each frame at its PTS instead
# of as-fast-as-the-decoder-produces. With MJPEG decode now fast
# enough on Pi 5 (used to be HEVC-bottlenecked), sync=false made
# a 15-second clip play back in ~2 seconds. Live generator
# sources also work fine with sync=true since videotestsrc emits
# at the negotiated 30 fps.
# Why a CPU videoconvert and not GPU-side glcolorconvert:
# gtksink only accepts system-memory BGRA/BGRx. The "smart"
# version (glcolorconvert → caps(GLMemory, BGRA) → gldownload
# → gtksink) sounds better — convert on GPU, download already
# in the right format. In practice on Pi 5 V3D it stalls the
# pipeline to ~1 FPS while using low CPU, because V3D's GL
# colour-space conversion + dual readback path serializes
# weirdly. The naive shape uses more CPU but actually keeps
# 30 FPS, which is what matters at showtime.


# ── GLSL fragment shaders ─────────────────────────────────────────
#
# All shaders target GLES 2.0 (#version 100 + precision qualifier)
# for V3D compatibility. gstreamer's glshader plugin provides:
#   varying vec2 v_texcoord   — UV in [0, 1]
#   uniform float time        — running time in seconds
#   uniform sampler2D tex     — input texture (we ignore it; the
#                                shader is fully generative)

PLASMA_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec2 p = v_texcoord * 8.0;
    float t = time;
    float v = (sin(p.x + t) + sin(p.y + t * 1.3)
             + sin((p.x + p.y) * 0.5 + t * 0.7)
             + sin(sqrt(p.x*p.x + p.y*p.y) + t * 1.7)) * 0.25;
    v = (v + 1.0) * 0.5;
    float hue = fract(v + t / 9.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 1.0, 1.0)), 1.0);
}
"""

TUNNEL_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 pix = (v_texcoord - 0.5) * vec2(1280.0, 720.0);
    float r = length(pix) + 1.0;
    float a = atan(pix.y, pix.x);
    float u = mod(200.0 / r + time * 2.0, 1.0);
    float v_ = (a / PI + 1.0) * 0.5;
    float chk = mod(floor(u * 8.0) + floor(v_ * 16.0), 2.0);
    float hue = fract(v_ + time / 6.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 1.0, chk)), 1.0);
}
"""

# (waves shader removed — operator audition: visually
# indistinguishable from `moire`; moire kept, waves dropped.)

# Animated quasi-voronoi cellular pattern. The sin*sin product
# creates a grid of "cells" that wobble as the input coordinates
# get phase-shifted by their neighbours over time.
CELLS_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    float t = time;
    float scale = 0.038;
    float u = pix.x * scale + sin(pix.y * scale * 0.6 + t)      * 0.4;
    float vv = pix.y * scale + cos(pix.x * scale * 0.6 + t * 1.1) * 0.4;
    float pat = abs(sin(u * PI) * sin(vv * PI));
    pat = pow(pat, 0.6);
    float hue = fract((u * 28.0 + t * 14.0) / 180.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.82, pat)), 1.0);
}
"""

# Concentric-ring moiré from two slowly orbiting sources. The
# interference between two ring patterns gives the optical-illusion
# look that's hard to look away from at the projector.
MOIRE_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    vec2 ctr = vec2(640.0, 360.0);
    float t = time;
    float ox = 1280.0 * 0.10;
    float oy =  720.0 * 0.10;
    vec2 c1 = ctr + vec2(sin(t * 0.5) * ox, cos(t * 0.4) * oy);
    vec2 c2 = ctr - vec2(sin(t * 0.5) * ox, cos(t * 0.4) * oy);
    float spacing = 14.0;
    float r1 = distance(pix, c1) / spacing;
    float r2 = distance(pix, c2) / spacing;
    float pat = (sin(r1 + t * 2.0) + sin(r2 - t * 1.5)) * 0.25 + 0.5;
    float hue = fract(pat + t * 22.0 / 180.0);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.82, pat)), 1.0);
}
"""

# (metaballs shader removed — operator audition: "meh".)

# ── Gritty / hard-edged / tactile ones (the operator asked for) ──

# Quarter-arc tiles on a grid (classic Truchet) + a "snake" that
# wanders through the field and brightens the arcs near its
# passage. Operator's idea: the snaps that re-shuffle the tile
# rotations happen MUCH less often (every ~20s instead of ~2s),
# so the maze is stable long enough that the snake's exploration
# of it is legible.
#
# The snake's path is a smooth Lissajous-style wander with two
# incommensurate frequencies per axis — never repeats exactly,
# always exploring new territory. A trail of 24 past positions
# (sampled at decreasing time offsets) gives the snake a body
# that fades from head (bright, current position) to tail (dim,
# ~2s back). The truchet arcs only show where the snake's glow
# reaches them — base brightness is low so the pattern is "dim
# pencil sketch" until the snake passes by, at which point the
# arcs nearby flare up.
#
# What it ISN'T: the snake doesn't literally walk the arc-graph
# (which would need recursive cell-walking, not fragment-shader-
# friendly). It wanders through the same screen the arcs live on
# and reveals them by proximity. The visual feel is similar.
TRUCHET_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;
// Snake positions pushed from CPU each tick. Sixteen separate
// vec2 uniforms because GstStructure can't carry bracket-named
// fields from Python — we copy them into a local array at the
// top of main() and loop over that instead. Head is snake_15
// (current); snake_0 is the tail. Coords are in normalized
// (0..1) v_texcoord space, matching the texture the maze
// itself is drawn in. The CPU walks the truchet arc graph so
// each position sits exactly on an arc (the snake never
// crosses a boundary line). If the uniforms haven't been set
// yet they default to (0,0), which puts the snake briefly at
// the top-left corner before the first CPU tick fires.
uniform vec2 snake_0,  snake_1,  snake_2,  snake_3;
uniform vec2 snake_4,  snake_5,  snake_6,  snake_7;
uniform vec2 snake_8,  snake_9,  snake_10, snake_11;
uniform vec2 snake_12, snake_13, snake_14, snake_15;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

float rand(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

void main() {
    float scale = 50.0;
    vec2 pix = v_texcoord * vec2(1280.0, 720.0) / scale;
    vec2 gid = floor(pix);
    vec2 gp = fract(pix);

    // Slow snaps — tile rotations only re-shuffle once every
    // ~20 seconds. The maze is stable in between, so the snake
    // gets time to actually explore it.
    float r = rand(gid + floor(time / 20.0));
    if (r > 0.5) gp.x = 1.0 - gp.x;

    float d1 = distance(gp, vec2(0.0));
    float d2 = distance(gp, vec2(1.0));
    float ring = max(smoothstep(0.06, 0.0, abs(d1 - 0.5)),
                     smoothstep(0.06, 0.0, abs(d2 - 0.5)));

    // Copy 16 individually-named uniforms into a local array so
    // the loop can use a dynamic index. (Uniform arrays with
    // dynamic indices aren't portable in GLES 2.0; local arrays
    // are.) Each entry is a snake position the CPU placed exactly
    // on a truchet arc.
    vec2 snake[16];
    snake[0]  = snake_0;   snake[1]  = snake_1;
    snake[2]  = snake_2;   snake[3]  = snake_3;
    snake[4]  = snake_4;   snake[5]  = snake_5;
    snake[6]  = snake_6;   snake[7]  = snake_7;
    snake[8]  = snake_8;   snake[9]  = snake_9;
    snake[10] = snake_10;  snake[11] = snake_11;
    snake[12] = snake_12;  snake[13] = snake_13;
    snake[14] = snake_14;  snake[15] = snake_15;

    // Trail: glow weight ramps tail→head so the body fades.
    float glow = 0.0;
    for (int i = 0; i < 16; i++) {
        float d = distance(v_texcoord, snake[i]);
        float age_weight = float(i + 1) / 16.0;
        glow += exp(-d * 80.0) * age_weight;
    }

    // Tight bright ball at the head.
    float head = exp(-distance(v_texcoord, snake[15]) * 120.0);

    // Maze: full-brightness coloured arcs at their natural hue.
    // (Earlier dim-base version was too murky.)
    float hue = fract(rand(gid) * 0.7 + time * 0.05);
    vec3 arc = hsv2rgb(vec3(hue, 0.85, ring));

    // Where the snake's body overlaps an arc, blend the arc
    // toward white. clamp(...,0,1) caps it: arcs the snake is
    // currently ON go fully white, ones recently passed turn
    // toward white proportionally, ones the snake hasn't been
    // near keep their natural colour.
    float white_amount = clamp(ring * glow * 2.0, 0.0, 1.0);
    vec3 col = mix(arc, vec3(1.0), white_amount);

    // Head ball sits on top of everything, bright white.
    col = clamp(col + vec3(head * 0.95), 0.0, 1.0);

    gl_FragColor = vec4(col, 1.0);
}
"""

# True voronoi with hard cell boundaries — different from `cells`
# which is the soft sin·sin product. This one looks like cracked
# glass or stained-glass tessellation. F1+F2 trick gives sharp
# edges between cells.
VORONOI_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec2 hash2(vec2 p) {
    return fract(sin(vec2(dot(p, vec2(127.1, 311.7)),
                          dot(p, vec2(269.5, 183.3)))) * 43758.5453);
}

void main() {
    float scale = 80.0;
    vec2 pix = v_texcoord * vec2(1280.0, 720.0) / scale;
    vec2 gid = floor(pix);
    vec2 gp = fract(pix);
    float md1 = 999.0;
    float md2 = 999.0;
    vec2 mcell;
    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            vec2 n = vec2(float(i), float(j));
            vec2 r = hash2(gid + n);
            r = 0.5 + 0.5 * sin(time * 0.3 + 6.283 * r);
            float d = distance(n + r, gp);
            if (d < md1) { md2 = md1; md1 = d; mcell = gid + n; }
            else if (d < md2) { md2 = d; }
        }
    }
    float edge = smoothstep(0.02, 0.08, md2 - md1);
    float hue = fract(hash2(mcell).x + time * 0.05);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.78, edge)), 1.0);
}
"""

# Tessellating hexagons. Each cell pulses on its own clock —
# tactile honeycomb / Tron look.
HEXGRID_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    float scale = 50.0;
    vec2 pix = v_texcoord * vec2(1280.0, 720.0) / scale;
    vec2 s = vec2(1.0, 1.7320508);
    vec2 a = mod(pix, s) - s * 0.5;
    vec2 b = mod(pix + s * 0.5, s) - s * 0.5;
    vec2 g = dot(a, a) < dot(b, b) ? a : b;
    float d = length(g);
    vec2 cell = pix - g;
    float pulse = 0.5 + 0.5 * sin(time * 1.5 + cell.x * 0.5 + cell.y * 0.3);
    float ring = smoothstep(0.5, 0.4, d) * pulse;
    float hue = fract(cell.x * 0.1 + cell.y * 0.07 + time * 0.05);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.8, ring)), 1.0);
}
"""

# Demoscene classic: B&W (well, colour-cycled) checker rotating
# and zooming around the centre. Hard edges, kinetic.
ROTOZOOM_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0, 720.0);
    float zoom = 0.5 + sin(time * 0.3) * 0.3;
    float a = time * 0.2;
    float ca = cos(a), sa = sin(a);
    vec2 q = vec2(p.x * ca - p.y * sa, p.x * sa + p.y * ca) * zoom;
    float chk = mod(floor(q.x / 40.0) + floor(q.y / 40.0), 2.0);
    float hue = fract(time * 0.1 + chk * 0.5);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.9, chk)), 1.0);
}
"""

# (glitch shader removed — operator audition: "no".)

# Straight grid lines warped through sin/cos — the geometry
# "breathes". Sharp line edges, organic motion.
WARPGRID_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec2 pix = v_texcoord * vec2(1280.0, 720.0);
    float t = time;
    pix.x += sin(pix.y * 0.02 + t) * 30.0;
    pix.y += cos(pix.x * 0.02 + t * 1.3) * 30.0;
    vec2 g = fract(pix / 60.0);
    float line = min(min(g.x, g.y), min(1.0 - g.x, 1.0 - g.y));
    float bright = smoothstep(0.04, 0.0, line);
    float hue = fract(pix.x * 0.001 + pix.y * 0.001 + t * 0.1);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.7, bright)), 1.0);
}
"""

# Fractal Brownian Motion noise tuned for marble / polished
# stone — multi-octave noise + ridge warping gives sharp veins
# through cloudy interior.
MARBLE_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float fbm(vec2 p) {
    float v = 0.0;
    float amp = 0.5;
    for (int i = 0; i < 5; i++) {
        v += amp * noise(p);
        p *= 2.0;
        amp *= 0.5;
    }
    return v;
}

void main() {
    vec2 p = v_texcoord * 4.0;
    p.x += time * 0.1;
    float n = fbm(p);
    n = fbm(p + vec2(n * 2.0));
    float v = pow(abs(sin(n * 8.0 + time * 0.2)), 0.5);
    vec3 c = mix(vec3(0.08, 0.10, 0.18), vec3(0.95, 0.97, 1.0), v);
    gl_FragColor = vec4(c, 1.0);
}
"""

# ── "Really cool" extras inspired by shader culture (Shadertoy) ──

# Real underwater pool-floor caustics — bright animated tendrils
# of light focused through rippling water onto the floor below.
# Classic Shadertoy-style fold-iteration formula (Dave Hoskins,
# "Water Caustic"). Each iteration distorts the uv with a
# trigonometric kick; reciprocal-length accumulation gives the
# bright filament look at light-focus points.
CAUSTICS_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

#define TAU 6.28318530718

void main() {
    float t = time * 0.5 + 23.0;
    vec2 uv = v_texcoord;
    vec2 p = mod(uv * TAU * 2.0, TAU) - 250.0;
    vec2 i = p;
    float c = 1.0;
    float inten = 0.005;
    for (int n = 0; n < 5; n++) {
        float tt = t * (1.0 - (3.5 / float(n + 1)));
        i = p + vec2(cos(tt - i.x) + sin(tt + i.y),
                     sin(tt - i.y) + cos(tt + i.x));
        c += 1.0 / length(vec2(p.x / (sin(i.x + tt) / inten),
                                p.y / (cos(i.y + tt) / inten)));
    }
    c /= 5.0;
    c = 1.17 - pow(c, 1.4);
    vec3 col = vec3(pow(abs(c), 8.0));
    // Tint toward pool-blue rather than pure white — the floor
    // we're seeing the caustics ON has its own colour.
    col = clamp(col + vec3(0.0, 0.35, 0.5), 0.0, 1.0);
    gl_FragColor = vec4(col, 1.0);
}
"""

# (Mandelbrot fractal zoom removed — too expensive on Pi 5 V3D
# at 256 iterations × 1280×720 = ~235M ops/frame. Ran at ~1 fps.
# If we ever come back to it: needs a much lower iteration cap
# OR a multi-pass approach (low-res preview, full-res only for
# foreground), neither of which is worth the complexity right
# now.)

# Galactic spiral arms. Polar log-spiral pattern with hue
# cycling along the radial angle. Slow, deep, hypnotic.
SPIRAL_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

const float PI = 3.14159265358979;

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0, 720.0);
    float r = length(p);
    float a = atan(p.y, p.x);
    float arms = 5.0;
    float v = sin(a * arms + log(r + 1.0) * 2.0 - time * 1.5);
    v = pow(abs(v), 1.5);
    v *= smoothstep(0.0, 100.0, r) * smoothstep(800.0, 400.0, r);
    float hue = fract(a / (2.0 * PI) + time * 0.05);
    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.85, v)), 1.0);
}
"""

# (kaleido shader removed — operator audition: deferred to the
# FX-chain phase as a frame transform rather than a standalone
# generator. Kaleido is more useful applied to other generators.)

# (Procedural ray-marched donut removed — operator's call:
# keep only the image-textured variant below. The texturing is
# what makes the donut interesting; the procedural hue gradient
# version was redundant once the image-fed one existed.)

# Image-textured donut: ray-marched torus + image as surface
# material. The shader samples its `tex` uniform (whatever the
# upstream source bin feeds in) and wraps it around the torus
# via UV unwrap. The image scrolls slowly around the major
# circumference so it animates.
DONUT_SHADER = """\
#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;
uniform sampler2D tex;

const float PI = 3.14159265359;

float sdTorus(vec3 p, vec2 t) {
    vec2 q = vec2(length(p.xz) - t.x, p.y);
    return length(q) - t.y;
}

vec3 rotY(vec3 p, float a) {
    float c = cos(a), s = sin(a);
    return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z);
}
vec3 rotX(vec3 p, float a) {
    float c = cos(a), s = sin(a);
    return vec3(p.x, c * p.y - s * p.z, s * p.y + c * p.z);
}

float map(vec3 p) {
    p = rotY(p, time * 0.5);
    p = rotX(p, time * 0.3);
    return sdTorus(p, vec2(1.0, 0.4));
}

// Map a 3D point on the torus surface to a (u, v) coordinate
// suitable for sampling the input texture. Inverts the rotation
// we applied in `map` so the texture sticks to the torus rather
// than spinning past it.
vec2 torusUV(vec3 p_world) {
    vec3 p = rotX(p_world, -time * 0.3);
    p = rotY(p, -time * 0.5);
    float u = atan(p.z, p.x);            // -PI..PI around major
    vec2 q = vec2(length(p.xz), p.y);
    vec2 dq = q - vec2(1.0, 0.0);        // R = 1.0
    float v = atan(dq.y, dq.x);          // -PI..PI around minor
    return vec2((u + PI) / (2.0 * PI),
                (v + PI) / (2.0 * PI));
}

void main() {
    vec2 uv_scr = v_texcoord - 0.5;
    uv_scr.x *= 1280.0 / 720.0;
    vec3 ro = vec3(0.0, 0.0, -3.0);
    vec3 rd = normalize(vec3(uv_scr, 1.0));
    float t = 0.0;
    bool hit = false;
    for (int i = 0; i < 64; i++) {
        vec3 pos = ro + rd * t;
        float d = map(pos);
        if (d < 0.001) { hit = true; break; }
        t += d;
        if (t > 10.0) break;
    }
    if (!hit) {
        gl_FragColor = vec4(0.0, 0.0, 0.05, 1.0);
        return;
    }
    vec3 hit_pos = ro + rd * t;
    vec2 tex_uv = torusUV(hit_pos);
    // Slow scroll around the major circumference — the image
    // wraps around the donut like a label on a tin can.
    tex_uv.x = fract(tex_uv.x + time * 0.05);
    vec3 col = texture2D(tex, tex_uv).rgb;
    // Simple depth darken so the back of the donut isn't full
    // bright — gives it a hint of 3D form.
    float depth = t / 10.0;
    col *= 1.0 - depth * 0.4;
    gl_FragColor = vec4(col, 1.0);
}
"""

# The full catalogue. Order here is also the cycle order for
# `[` / `]`. New ones go at the end so muscle memory holds.
GENERATORS = {
    "plasma":     PLASMA_SHADER,
    "tunnel":     TUNNEL_SHADER,
    "cells":      CELLS_SHADER,
    "moire":      MOIRE_SHADER,
    "truchet":    TRUCHET_SHADER,
    "voronoi":    VORONOI_SHADER,
    "hexgrid":    HEXGRID_SHADER,
    "rotozoom":   ROTOZOOM_SHADER,
    "warpgrid":   WARPGRID_SHADER,
    "marble":     MARBLE_SHADER,
    "caustics":   CAUSTICS_SHADER,
    "spiral":     SPIRAL_SHADER,
    "donut":      DONUT_SHADER,  # image-fed
}

# Generators whose source bin needs an image instead of the
# default videotestsrc-black. Keyed off the name so the rest of
# the dispatch logic doesn't have to special-case anything.
IMAGE_FED_GENERATORS = {"donut"}

# Cycle order for `[` / `]`. Same as dict order but explicit
# (dict iteration order is technically insertion-order in
# Python 3.7+; this list makes the contract obvious).
GENERATOR_ORDER = list(GENERATORS.keys())


# ── Truchet snake-game CPU simulation ────────────────────────────────
#
# The truchet generator's "snake" is a literal Snake-game-style
# entity that walks the arc graph of the visible maze. The CPU
# tracks its state (current cell, which side it entered from,
# progress along the current arc) and ticks it forward at ~30Hz,
# pushing its head + 15 trail positions to the shader as uniforms.
# The shader only RENDERS — it doesn't compute where the snake is.
#
# Why on the CPU: a fragment shader can't easily simulate a
# sequence of cell-to-cell transitions (each cell's outgoing
# direction depends on the snake's incoming direction + the tile's
# random rotation, which is iterative). Doing it on the CPU and
# pushing the result as uniforms is the clean separation.

# Direction conventions used here:
#   N = "north"  → cell_y += 1  → towards higher v_texcoord.y
#   S = "south"  → cell_y -= 1
#   E = "east"   → cell_x += 1
#   W = "west"   → cell_x -= 1
# These labels are arbitrary — what matters is that Python's
# notion of which neighbour cell shares the exit edge matches
# the shader's geometry, which it does because both use the
# same `gp = fract(pix)` cell decomposition.
SNAKE_DIR_DELTA = {
    "W": (-1, 0), "N": (0, 1), "E": (1, 0), "S": (0, -1),
}
SNAKE_OPPOSITE = {"W": "E", "N": "S", "E": "W", "S": "N"}

# Truchet tile arc topology — which exit corresponds to which
# entry, for each of the two tile rotation states.
#   Type 1 (not flipped, hash <= 0.5): arcs at SW + NE corners
#     → W↔S (via SW arc), N↔E (via NE arc)
#   Type 2 (flipped, hash > 0.5): arcs at SE + NW corners
#     → E↔S (via SE arc), W↔N (via NW arc)
SNAKE_TYPE1_FLOW = {"W": "S", "S": "W", "N": "E", "E": "N"}
SNAKE_TYPE2_FLOW = {"E": "S", "S": "E", "W": "N", "N": "W"}

# Edge midpoint coordinates in local cell space (0..1).
SNAKE_EDGE_MIDS = {
    "N": (0.5, 1.0), "S": (0.5, 0.0),
    "E": (1.0, 0.5), "W": (0.0, 0.5),
}

# Match the shader's `scale` constant and snap period exactly.
TRUCHET_SCALE = 50.0
TRUCHET_SNAP_PERIOD = 20.0
SNAKE_TRAIL_LEN = 16


def _truchet_tile_flipped(cx, cy, t):
    """Mirror the shader's `rand(gid + floor(time / 20.0)) > 0.5`
    exactly. Returns whether the tile at (cx, cy) is in its
    flipped orientation at time `t`."""
    snap = math.floor(t / TRUCHET_SNAP_PERIOD)
    # Shader: rand(gid + floor(time/20)). gid is the vec2 cell
    # coord; the float `snap` broadcasts across both axes. Then
    # rand(p) = fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453).
    arg = (cx + snap) * 127.1 + (cy + snap) * 311.7
    v = math.sin(arg) * 43758.5453
    return (v - math.floor(v)) > 0.5


def _snake_arc_xy(start_dir, end_dir, progress):
    """Local position (0..1) on the quarter-arc joining the
    start-edge midpoint to the end-edge midpoint. `progress` 0
    is at the start edge, 1 at the end edge."""
    sx, sy = SNAKE_EDGE_MIDS[start_dir]
    ex, ey = SNAKE_EDGE_MIDS[end_dir]
    # Arc center = the corner adjacent to both edges. W or E
    # picks x = 0 or 1; S or N picks y = 0 or 1.
    cx = 0.0 if "W" in (start_dir, end_dir) else 1.0
    cy = 0.0 if "S" in (start_dir, end_dir) else 1.0
    sa = math.atan2(sy - cy, sx - cx)
    ea = math.atan2(ey - cy, ex - cx)
    diff = ea - sa
    # Always take the short way round (the quarter arc).
    while diff > math.pi:
        diff -= 2.0 * math.pi
    while diff < -math.pi:
        diff += 2.0 * math.pi
    angle = sa + diff * progress
    return (cx + 0.5 * math.cos(angle), cy + 0.5 * math.sin(angle))


class TruchetSnake:
    """A literal Snake-game-style entity walking the truchet arc
    graph. Constrained to the visible arcs — never crosses a
    boundary line because each cell transition uses the tile's
    actual arc topology.

    Wraps modulo the cell grid so the snake can wander forever
    without falling off the canvas. When the maze's 20-second
    "snap" re-shuffles tile rotations, the snake's outgoing
    direction is re-derived from whatever tile is now under it —
    so it continues along whichever arc the new tile actually
    has (no dead-arc trapping)."""

    SPEED = 1.5  # arcs traversed per second

    def __init__(self, canvas_w, canvas_h):
        self.cells_x = max(1, int(canvas_w // TRUCHET_SCALE))
        self.cells_y = max(1, int(canvas_h // TRUCHET_SCALE))
        self.cell_x = self.cells_x // 2
        self.cell_y = self.cells_y // 2
        self.incoming = "W"
        self.progress = 0.0
        self.trail = []  # list of (norm_x, norm_y), oldest first
        self._last_t = None

    def reset(self):
        """Drop the snake somewhere mid-canvas with a fresh trail.
        Called when truchet is (re-)activated."""
        self.cell_x = self.cells_x // 2
        self.cell_y = self.cells_y // 2
        self.incoming = "W"
        self.progress = 0.0
        self.trail = []
        self._last_t = None

    def _outgoing(self, t):
        flipped = _truchet_tile_flipped(self.cell_x, self.cell_y, t)
        flow = SNAKE_TYPE2_FLOW if flipped else SNAKE_TYPE1_FLOW
        return flow[self.incoming]

    def update(self, t):
        """Advance the snake to time `t` (seconds, monotonic)."""
        if self._last_t is None:
            self._last_t = t
        dt = max(0.0, min(0.1, t - self._last_t))  # cap big jumps
        self._last_t = t
        self.progress += dt * self.SPEED
        # Transition through cells if progress crossed 1.0. Guard
        # rail of 8 in case a huge dt would push us through many
        # cells — should never happen in practice but if it does,
        # don't burn the timer.
        for _ in range(8):
            if self.progress < 1.0:
                break
            self.progress -= 1.0
            outgoing = self._outgoing(t)
            dx, dy = SNAKE_DIR_DELTA[outgoing]
            self.cell_x = (self.cell_x + dx) % self.cells_x
            self.cell_y = (self.cell_y + dy) % self.cells_y
            self.incoming = SNAKE_OPPOSITE[outgoing]
        if self.progress >= 1.0:
            self.progress = 0.999
        # Compute current world-norm position.
        outgoing = self._outgoing(t)
        local_x, local_y = _snake_arc_xy(
            self.incoming, outgoing, self.progress)
        # Use TRUE scale (TRUCHET_SCALE / canvas_dim) so the
        # snake's norm coords agree with the shader's v_texcoord
        # mapping exactly — partial right/top columns left
        # unused but visually faithful.
        norm_x = (self.cell_x + local_x) * TRUCHET_SCALE / (
            self.cells_x * TRUCHET_SCALE)
        norm_y = (self.cell_y + local_y) * TRUCHET_SCALE / (
            self.cells_y * TRUCHET_SCALE)
        # Simplifies to (cx + local) / cells, but written this
        # way makes the intent obvious.
        self.trail.append((norm_x, norm_y))
        if len(self.trail) > SNAKE_TRAIL_LEN:
            self.trail = self.trail[-SNAKE_TRAIL_LEN:]

    def positions(self):
        """Padded list of SNAKE_TRAIL_LEN positions. Head is the
        last element. Unfilled slots (on cold start) repeat the
        oldest known position so all 16 uniforms are valid."""
        out = list(self.trail)
        while len(out) < SNAKE_TRAIL_LEN:
            out.insert(0, out[0] if out else (0.5, 0.5))
        return out


def _find_donut_image():
    """Pick an image at random from assets/images/ to texture the
    donut. Returns a Path or None.

    Random pick (not alphabetical) — each time the operator
    activates the donut generator they get a different image,
    so cycling away and back is a way to shuffle textures.
    Falls back to a generated checker pattern only if the
    folder is genuinely empty.
    """
    import random
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    candidates = (list(IMAGES_DIR.glob("*.png"))
                  + list(IMAGES_DIR.glob("*.jpg"))
                  + list(IMAGES_DIR.glob("*.jpeg"))
                  + list(IMAGES_DIR.glob("*.PNG"))
                  + list(IMAGES_DIR.glob("*.JPG")))
    if candidates:
        return random.choice(candidates)
    return _ensure_placeholder_image()


def _ensure_placeholder_image():
    """Write a colourful checker placeholder if assets/images/ is
    empty, so the donut generator has something to texture with on first
    launch. Operator should drop their own image to replace.

    Uses cairo (already in the GTK stack — no new deps). Returns
    the placeholder path or None if cairo isn't available.
    """
    default = IMAGES_DIR / "_default_checker.png"
    if default.exists():
        return default
    try:
        import cairo
    except ImportError:
        print("[vj] cairo not available; cannot generate placeholder image")
        return None
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1024, 512)
    ctx = cairo.Context(surface)
    # Wide aspect (2:1) matches the torus UV unwrap better than
    # a square. Vibrant 6-colour cycling checker — visible on
    # the donut even at small UV scales.
    colours = [
        (1.0, 0.20, 0.30),
        (1.0, 0.60, 0.00),
        (1.0, 1.00, 0.20),
        (0.2, 1.00, 0.50),
        (0.2, 0.60, 1.00),
        (0.7, 0.30, 1.00),
    ]
    cell = 64
    for y in range(8):
        for x in range(16):
            idx = (x + y) % 6
            r, g, b = colours[idx]
            ctx.set_source_rgb(r, g, b)
            ctx.rectangle(x * cell, y * cell, cell, cell)
            ctx.fill()
    surface.write_to_png(str(default))
    print(f"[vj] wrote placeholder donut texture → {default}")
    return default


def _list_clips():
    """Sorted list of .mp4 / .mov / .MP4 / .MOV files in assets/clips/.

    Empty list if the folder doesn't exist or is empty. Phase 4
    cycles through this list with -/= keys.
    """
    if not CLIPS_DIR.exists():
        return []
    return (sorted(CLIPS_DIR.glob("*.mp4"))
            + sorted(CLIPS_DIR.glob("*.mov"))
            + sorted(CLIPS_DIR.glob("*.MP4"))
            + sorted(CLIPS_DIR.glob("*.MOV")))


def _detect_projector_output():
    """Best-guess the Wayland output name of the projector.

    Heuristic: the projector is the physically LARGEST enabled
    output. Pi VJ rigs typically pair a small operator screen
    (a 7" Pi display, ~410×260 mm) with a much bigger projector
    (1100+×600+ mm reported physical size). Both expose their
    physical size via wlr-randr; picking by area is reliable.

    Returns None if wlr-randr isn't available, only one output
    is enabled, or sizes aren't parseable. Caller falls back to
    the env var / hard-coded default.
    """
    try:
        out = subprocess.run(
            ["wlr-randr"], capture_output=True, text=True, timeout=2.0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    outputs = []  # list of (name, area_mm2, enabled)
    current_name = None
    current_area = 0
    current_enabled = False
    def flush():
        if current_name is not None:
            outputs.append((current_name, current_area, current_enabled))
    for line in out.stdout.splitlines():
        if line and not line.startswith(" ") and not line.startswith("\t"):
            flush()
            current_name = line.split()[0]
            current_area = 0
            current_enabled = False
        elif "Enabled: yes" in line:
            current_enabled = True
        elif "Physical size:" in line:
            # "  Physical size: 1150x650 mm"
            try:
                spec = line.split(":", 1)[1].strip().split()[0]
                w_str, h_str = spec.split("x")
                current_area = int(w_str) * int(h_str)
            except (ValueError, IndexError):
                pass
    flush()
    enabled = [(n, a) for (n, a, e) in outputs if e]
    if not enabled:
        return None
    # Largest physical area wins. Ties broken by name order.
    enabled.sort(key=lambda x: (-x[1], x[0]))
    return enabled[0][0]


# ── VLC backend (clip playback) ──────────────────────────────────────
#
# Why VLC and not mpv or our own GStreamer pipeline:
# Adafruit's Pi Video Looper 2 (the dev-blessed Pi 5 video looping
# reference implementation, tested on Pi 4 + 5) is ~80 lines of
# python-vlc. We do the same here. VLC picks the right backend
# automatically on Pi 5 OpenGL — no Vulkan swapchain failures,
# no DMABuf negotiation, no per-frame CPU videoconvert. Measured
# bare-VLC playing 720p HEVC on this Pi: ~7% CPU.
#
# Used here for clip playback only — generators still go through
# the GStreamer pipeline below because that's where the GLSL
# shaders + snake-game uniforms live cleanly.
#
# Architecture:
#   - mpv runs as a subprocess from app launch to app quit, with
#     a Unix-socket IPC server.
#   - When the operator picks a clip, we send `loadfile <path>`
#     over IPC and unpause. mpv handles decode, loop, sync.
#   - When the operator picks a generator, we send `set pause yes`
#     to mpv and bring the GStreamer output GTK window to the
#     front. mpv's window stays underneath, paused (~0% CPU).
#
# Window stacking is the awkward bit on Wayland — set_keep_above
# is hint-only, but Pi OS's compositor (labwc) honours it.

class VlcBackend:
    """Thin wrapper around python-vlc — same surface as the
    previous MpvBackend (spawn/loadfile/pause/fullscreen/
    shutdown) so the rest of the app doesn't care which engine
    is under it.

    VLC runs in-process via libvlc and creates its own native
    window. We don't try to embed it in our GTK output window
    — embedding via set_xwindow() needs an X11 XID which on
    Wayland means XWayland-mode windows, and even Adafruit's
    looper sidesteps it (they use pygame just to grab a window
    ID). VLC's own window stacks underneath our generator
    output window, hidden in clip mode by the existing
    show/hide logic in _install_clip / _install_generator."""

    def __init__(self, projector_output):
        self.projector_output = projector_output
        self.instance = None
        self.player = None

    def spawn(self):
        """Create the VLC instance + media player. No subprocess
        — libvlc lives in-process. Returns True on success."""
        try:
            import vlc
        except ImportError:
            print("[vj] python3-vlc not installed — run setup.sh")
            return False
        self._vlc = vlc  # cache module reference for event types
        self.instance = vlc.Instance(
            "--no-xlib",
            "--quiet",
            "--no-video-title-show",
            "--no-osd",
        )
        if self.instance is None:
            print("[vj] failed to construct VLC instance")
            return False
        self.player = self.instance.media_player_new()
        # Loop the current media by re-playing on
        # MediaPlayerEndReached. Instance-level --input-repeat
        # doesn't apply cleanly to a single media_player; this
        # handler is the reliable loop path.
        events = self.player.event_manager()
        events.event_attach(
            vlc.EventType.MediaPlayerEndReached,
            lambda _ev: self._on_end_reached(),
        )
        print(f"[vj] VLC ready (libvlc {vlc.libvlc_get_version().decode()}); "
              f"target output={self.projector_output}")
        return True

    def _on_end_reached(self):
        """MediaPlayerEndReached fires on the VLC event thread.
        Can't call player.play() from there (libvlc is not
        reentrant on the event thread); schedule on GLib idle."""
        def _restart():
            try:
                self.player.stop()
                self.player.play()
            except Exception as exc:
                print(f"[vj] loop restart failed: {exc!r}")
            return False
        GLib.idle_add(_restart)

    def loadfile(self, path):
        if self.player is None or self.instance is None:
            return
        media = self.instance.media_new(str(path))
        self.player.set_media(media)
        self.player.play()

    def pause(self, paused=True):
        if self.player is None:
            return
        # set_pause(1) pauses, set_pause(0) unpauses.
        self.player.set_pause(1 if paused else 0)

    def fullscreen(self, on=True):
        if self.player is None:
            return
        self.player.set_fullscreen(bool(on))

    def shutdown(self):
        if self.player is not None:
            try:
                self.player.stop()
                self.player.release()
            except Exception:
                pass
            self.player = None
        if self.instance is not None:
            try:
                self.instance.release()
            except Exception:
                pass
            self.instance = None


class VJApp(Gtk.Application):
    """GTK3 + GStreamer application owning the pipeline and the two
    top-level windows.

    Pipeline structure: one stable downstream bin (tee + 2 sinks) +
    one swappable source bin (clip OR generator). Source switches
    happen via keyboard; the downstream stays alive.
    """

    def __init__(self, source_kind="clip", single_screen=False):
        super().__init__(
            application_id="com.multitech.vjpi",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        # Initial source from CLI flag — phase 4 keys flip it at
        # runtime; the flag is just the starting state.
        self.initial_source_kind = source_kind
        self.single_screen = single_screen

        self.pipeline = None
        self.output_window = None
        self.hud_window = None
        self._downstream_bin = None
        self._source_bin = None
        self._status_label = None

        # Clip playback engine — VLC via python-vlc. Spawned in
        # do_activate so it's ready before the first _install_clip.
        # The projector output name is auto-detected from
        # wlr-randr (largest physical size = projector). Operator
        # can override via VJ_PROJECTOR_OUTPUT env var.
        projector_output = (os.environ.get("VJ_PROJECTOR_OUTPUT")
                            or _detect_projector_output()
                            or "HDMI-A-1")
        self._player = VlcBackend(projector_output)

        # Clip pool state — list of paths + index of the last clip
        # that was active. We remember the index even when the
        # current source is a generator so `-/=` can return to the
        # previous-or-next clip naturally.
        self._clips = _list_clips()
        self._current_clip_idx = 0
        # Tracks what the current source bin is rendering, for
        # status display and key routing. ("clip", path) or
        # ("generator", name).
        self._current_source = None
        # Generator-cycle pointer. `[` / `]` walks this through
        # GENERATOR_ORDER. Survives clip mode too, so going clip
        # → [ goes back to whichever generator was last active.
        self._current_generator_idx = 0
        # Big "what's playing" label, set from _refresh_status.
        self._big_status_label = None
        # CPU-side state for the truchet snake-game. The snake's
        # full state lives here; the shader only renders glow at
        # the positions we push as uniforms each tick. See the
        # TruchetSnake class above for the simulation details.
        # _snake_start_t is set the first time the truchet
        # generator is activated so the snake's notion of time
        # aligns with the shader's `time` uniform for the snap
        # cadence.
        self._truchet_snake = TruchetSnake(CANVAS_W, CANVAS_H)
        self._snake_start_t = None
        self._snake_uniforms_err_logged = False

    # ── GTK Application lifecycle ──────────────────────────────────

    def do_command_line(self, cmdline):
        self.activate()
        return 0

    def do_activate(self):
        Gst.init(None)
        print(f"[vj] initial source: {self.initial_source_kind}")

        # Spawn mpv up-front so the IPC socket exists by the time
        # _install_clip needs it. Failure to launch is non-fatal —
        # we still have generators via GStreamer; only clip mode
        # breaks. Operator gets a console warning.
        if not self._player.spawn():
            print("[vj] WARNING: mpv didn't start — clips won't play. "
                  "Generators still work.")

        # Build the stable downstream bin once. The source bin gets
        # constructed below and added to the same pipeline.
        self.pipeline = Gst.Pipeline.new("vj-pipeline")
        try:
            self._downstream_bin = Gst.parse_bin_from_description(
                DOWNSTREAM_DESC, True
            )
        except GLib.Error as exc:
            self._fail(
                "Could not build the downstream GStreamer chain.\n"
                f"  Error: {exc.message}\n\n"
                "Hint: is `gstreamer1.0-gtk3` installed? Re-run setup.sh."
            )
            return
        self._downstream_bin.set_name("downstream")
        self.pipeline.add(self._downstream_bin)

        # Pick the initial source. If the user asked for clip mode
        # but there are no clips, fall back to plasma so they get
        # something on-screen rather than a silent failure.
        if self.initial_source_kind in GENERATORS:
            self._install_generator(self.initial_source_kind, start_state=Gst.State.NULL)
        elif self._clips:
            self._install_clip(0, start_state=Gst.State.NULL)
        else:
            print("[vj] no clips found; falling back to plasma generator")
            self._install_generator("plasma", start_state=Gst.State.NULL)

        if not self._bind_sinks_to_windows():
            return

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # Only show the GStreamer output window if a generator is
        # the active source. In clip mode it stays hidden so mpv's
        # window is what the projector shows — otherwise both
        # windows compete and the operator gets a stray third
        # window stacked on the desktop.
        in_clip_mode = (self._current_source
                        and self._current_source[0] == "clip")
        if not in_clip_mode:
            self.output_window.show_all()
        self.hud_window.show_all()
        if self.single_screen:
            if not in_clip_mode:
                self._move_window_to_monitor(self.output_window, 0, fullscreen=False)
            self._move_window_to_monitor(self.hud_window, 0, fullscreen=False)
        else:
            if not in_clip_mode:
                self._move_window_to_monitor(self.output_window, 1, fullscreen=True)
            self._move_window_to_monitor(self.hud_window, 0, fullscreen=False)

        self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[vj] pipeline up: {Gst.version_string()}")

        # Fullscreen mpv on the projector — only if a 2nd monitor
        # actually exists AND we weren't asked for single-screen
        # mode. On a one-display Pi (or remote/headless), keep mpv
        # windowed so the HUD stays visible alongside.
        display = Gdk.Display.get_default()
        n_monitors = display.get_n_monitors() if display else 1
        print(f"[vj] {n_monitors} monitor(s) detected; mpv "
              f"target={self._player.projector_output}")
        if n_monitors >= 2 and not self.single_screen:
            GLib.timeout_add(400, self._player_fullscreen_once)
        else:
            print(f"[vj] not fullscreening mpv "
                  f"(single screen or --single-screen flag)")

        # 30Hz snake-sim tick. Runs unconditionally; the callback
        # is a no-op when truchet isn't the active generator, so
        # the cost when idle is just one Python function call per
        # frame (negligible). Keeping it always-on avoids juggling
        # start/stop on every generator swap.
        GLib.timeout_add(33, self._on_truchet_snake_tick)

    def do_shutdown(self):
        if self._player is not None:
            self._player.shutdown()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        Gtk.Application.do_shutdown(self)

    # ── Source-bin builders ────────────────────────────────────────

    def _build_clip_source_bin(self, clip_path):
        """Build a Gst.Bin: filesrc → decodebin → videoconvert →
        videoscale → capsfilter → glupload. The bin has a ghost src
        pad outputting GL textures at CANVAS_W × CANVAS_H.

        decodebin's pad-added is hooked up to link into the post-
        decoder normalisation chain at runtime — same pattern as
        phase 2, just localised to this bin.
        """
        bin_name = f"clip-source-{id(clip_path) & 0xffff:04x}"
        outer = Gst.Bin.new(bin_name)

        filesrc = Gst.ElementFactory.make("filesrc", None)
        filesrc.set_property("location", str(clip_path))

        decoder = Gst.ElementFactory.make("decodebin", None)

        # Post-decoder normalisation: bring whatever decodebin
        # produces up/down to canvas resolution + raw video, then
        # upload to GL. parse_bin_from_description gives us ghost
        # pads on the unconnected ends (videoconvert sink + glupload
        # src), which we use below.
        norm = Gst.parse_bin_from_description(
            f"videoconvert ! videoscale ! "
            f"video/x-raw,width={CANVAS_W},height={CANVAS_H} ! "
            f"glupload",
            True,
        )

        outer.add(filesrc)
        outer.add(decoder)
        outer.add(norm)
        if not filesrc.link(decoder):
            raise RuntimeError("failed to link filesrc → decodebin")

        # decodebin → norm gets linked once decodebin figures out the
        # stream type. Bind the handler to this bin's `norm` so the
        # closure doesn't reach into self.
        norm_sink = norm.get_static_pad("sink")
        def on_pad_added(_decoder, new_pad):
            caps = new_pad.get_current_caps()
            if caps is None:
                return
            if not caps.get_structure(0).get_name().startswith("video/"):
                return  # ignore audio
            if norm_sink.is_linked():
                return
            new_pad.link(norm_sink)
        decoder.connect("pad-added", on_pad_added)

        # Ghost the GL texture out of the bin so the outer pipeline
        # can link directly to downstream.
        norm_src = norm.get_static_pad("src")
        outer.add_pad(Gst.GhostPad.new("src", norm_src))
        return outer

    def _build_generator_source_bin(self, name):
        """Build a Gst.Bin: videotestsrc-black → glupload → glshader.
        Output: GL textures at canvas resolution. parse_bin_from_
        description ghosts the unconnected glshader src pad as the
        bin's "src" pad — perfect for linking to downstream.
        """
        if name not in GENERATORS:
            raise ValueError(f"unknown generator {name!r}")
        outer = Gst.parse_bin_from_description(
            f"videotestsrc is-live=true pattern=black ! "
            f"video/x-raw,width={CANVAS_W},height={CANVAS_H},framerate=30/1 ! "
            f"glupload ! "
            f"glshader name=shader",
            True,
        )
        outer.set_name(f"generator-source-{name}")
        shader = outer.get_by_name("shader")
        shader.set_property("fragment", GENERATORS[name])
        return outer

    def _build_image_source_bin(self, name, image_path):
        """Build a Gst.Bin: filesrc → decodebin → videoconvert →
        imagefreeze → videoscale → glupload → glshader[name]. The
        image is fed as the shader's input texture (the standard
        `tex` uniform glshader binds to its upstream).

        Same outer shape as the regular generator bin (single GL
        ghost src pad at canvas resolution), so the downstream
        link path doesn't need to know which kind of source this
        is.
        """
        if name not in GENERATORS:
            raise ValueError(f"unknown generator {name!r}")
        outer = Gst.Bin.new(f"image-generator-source-{name}")
        filesrc = Gst.ElementFactory.make("filesrc", None)
        filesrc.set_property("location", str(image_path))
        decoder = Gst.ElementFactory.make("decodebin", None)
        # imagefreeze turns a one-frame decode into a continuous
        # stream at the requested framerate — the shader then
        # always has a current input buffer to sample as `tex`.
        norm = Gst.parse_bin_from_description(
            f"videoconvert ! imagefreeze ! videoscale ! "
            f"video/x-raw,width={CANVAS_W},height={CANVAS_H},"
            f"framerate=30/1 ! "
            f"glupload ! glshader name=shader",
            True,
        )
        outer.add(filesrc)
        outer.add(decoder)
        outer.add(norm)
        if not filesrc.link(decoder):
            raise RuntimeError("filesrc → decodebin link failed")

        norm_sink = norm.get_static_pad("sink")
        def on_pad_added(_dec, new_pad):
            caps = new_pad.get_current_caps()
            if caps is None:
                return
            struct_name = caps.get_structure(0).get_name()
            if not (struct_name.startswith("video/")
                    or struct_name.startswith("image/")):
                return
            if norm_sink and not norm_sink.is_linked():
                new_pad.link(norm_sink)
        decoder.connect("pad-added", on_pad_added)

        outer.add_pad(Gst.GhostPad.new(
            "src", norm.get_static_pad("src")))
        norm.get_by_name("shader").set_property(
            "fragment", GENERATORS[name])
        return outer

    # ── Source-bin install / replace ───────────────────────────────

    def _install_clip(self, idx, start_state=Gst.State.PLAYING):
        """Make assets/clips/<sorted>[idx] the active source via mpv.

        Sends `loadfile` over IPC + unpauses mpv. The GStreamer
        pipeline's source is set to a black plasma so the
        downstream/output_window stays alive in case the operator
        cycles back to a generator — but the output_window is
        hidden so mpv's window shows through on the projector.

        start_state is kept for signature compatibility with the
        generator install method; on the mpv path it just controls
        whether we leave mpv paused (NULL) or running (PLAYING).
        """
        if not self._clips:
            print("[vj] no clips loaded — ignoring clip switch")
            return
        idx = idx % len(self._clips)
        clip = self._clips[idx]
        self._current_clip_idx = idx
        self._current_source = ("clip", clip)
        # Tell mpv to play the clip. mpv handles decode/loop/sync.
        self._player.loadfile(clip)
        self._player.pause(False)
        # Hide the GStreamer output window so mpv's window is what
        # the projector shows. The GStreamer pipeline keeps a
        # source loaded (a cheap plasma) so swap-back-to-generator
        # is instant, but its rendering goes to nothing visible.
        if self.output_window is not None:
            self.output_window.hide()
        # Ensure a generator source-bin exists so the pipeline
        # isn't dangling — re-install plasma at NULL state so it's
        # idle. (Skip on first-launch when output_window doesn't
        # exist yet; the initial source is set by do_activate.)
        if (self.output_window is not None
                and (self._source_bin is None
                     or not (self._current_source
                             and self._current_source[0] == "generator"))):
            try:
                bin_ = self._build_generator_source_bin("plasma")
                self._swap_source_bin(bin_, Gst.State.NULL)
            except Exception as exc:
                print(f"[vj] couldn't park GStreamer source on plasma: {exc!r}")
        print(f"[vj] → clip {idx + 1}/{len(self._clips)}: {clip.name}")
        self._refresh_status()

    def _install_generator(self, name, start_state=Gst.State.PLAYING):
        """Make a glshader-driven generator the active source.
        Also pins the cycle pointer (_current_generator_idx) on this
        name so the `[` / `]` cycle picks up from here.

        Image-fed generators (those in IMAGE_FED_GENERATORS) get
        a different source-bin shape — image file → imagefreeze →
        shader — so their `tex` uniform sees the image instead of
        videotestsrc black.
        """
        try:
            if name in IMAGE_FED_GENERATORS:
                image_path = _find_donut_image()
                if image_path is None:
                    print(f"[vj] {name}: no image in assets/images/ and "
                          "couldn't generate placeholder; falling back to "
                          "plasma")
                    name = "plasma"
                    new_bin = self._build_generator_source_bin(name)
                else:
                    new_bin = self._build_image_source_bin(name, image_path)
                    print(f"[vj] {name}: using image {image_path.name}")
            else:
                new_bin = self._build_generator_source_bin(name)
        except Exception as exc:
            print(f"[vj] failed to build generator source ({name}): {exc!r}")
            return
        if name in GENERATOR_ORDER:
            self._current_generator_idx = GENERATOR_ORDER.index(name)
        self._current_source = ("generator", name)
        # Reset the snake whenever truchet is (re-)activated so it
        # always starts mid-screen with a clean trail. Also anchor
        # the snake's local clock here — its sense of time runs
        # from when truchet became active, matching the shader's
        # snap cadence which uses gstreamer time from that point.
        if name == "truchet":
            self._truchet_snake.reset()
            self._snake_start_t = time_mod.monotonic()
        self._swap_source_bin(new_bin, start_state)
        # Pause mpv (clip mode stops being on top of the projector)
        # and bring the GStreamer output window to the front. mpv
        # stays alive — paused at ~0% CPU — so the next clip-mode
        # switch is instant.
        self._player.pause(True)
        if self.output_window is not None:
            self.output_window.show_all()
            self.output_window.present()
            self.output_window.set_keep_above(True)
        print(f"[vj] → generator: {name}")
        self._refresh_status()

    def _player_fullscreen_once(self):
        """One-shot GLib callback: tell the clip player to go
        fullscreen on the projector output."""
        self._player.fullscreen(True)
        return False  # don't repeat

    def _on_truchet_snake_tick(self):
        """30Hz GLib timer callback. Advances the snake-game
        simulation and pushes its current head + trail positions
        to the truchet shader as uniforms. No-op when truchet
        isn't the active source.

        Returns True so GLib keeps the timer alive."""
        active = (self._current_source
                  and self._current_source[0] == "generator"
                  and self._current_source[1] == "truchet")
        if not active or self._source_bin is None:
            return True
        if self._snake_start_t is None:
            self._snake_start_t = time_mod.monotonic()
        t = time_mod.monotonic() - self._snake_start_t
        self._truchet_snake.update(t)
        shader = self._source_bin.get_by_name("shader")
        if shader is None:
            return True
        # Build the GstStructure of vec2 uniforms via the only
        # syntax that actually carries vec2 values through Python:
        # `from_string` with the typed-array form
        # `name=< (double)x, (double)y >`. Bracket-named fields
        # (e.g. snake[0]) can't get a value attached from Python,
        # which is why the GLSL declares 16 individual uniforms.
        positions = self._truchet_snake.positions()
        fields = ", ".join(
            f"snake_{i}=< (double){x:.5f}, (double){y:.5f} >"
            for i, (x, y) in enumerate(positions)
        )
        struct_str = f"uniforms, {fields};"
        result = Gst.Structure.from_string(struct_str)
        if result is None or result[0] is None:
            if not self._snake_uniforms_err_logged:
                print(f"[vj] truchet snake: failed to build uniforms "
                      f"structure (parse returned None)")
                self._snake_uniforms_err_logged = True
            return True
        try:
            shader.set_property("uniforms", result[0])
        except Exception as exc:
            if not self._snake_uniforms_err_logged:
                print(f"[vj] truchet snake: set uniforms failed: {exc!r}")
                self._snake_uniforms_err_logged = True
        return True

    def _swap_source_bin(self, new_bin, start_state):
        """Tear down the existing source bin (if any), add the new
        one, link to downstream. Returns the pipeline to PLAYING if
        start_state == PLAYING; on the initial install the caller
        promotes state once everything is wired up.
        """
        if self._source_bin is not None:
            # PAUSED first so the GL elements don't hold buffers
            # across the unlink; then NULL to release resources.
            old = self._source_bin
            self._source_bin = None
            self.pipeline.set_state(Gst.State.READY)
            old.unlink(self._downstream_bin)
            old.set_state(Gst.State.NULL)
            self.pipeline.remove(old)

        self.pipeline.add(new_bin)
        if not new_bin.link(self._downstream_bin):
            print("[vj] source → downstream link failed")
        self._source_bin = new_bin

        if start_state == Gst.State.PLAYING:
            self.pipeline.set_state(Gst.State.PLAYING)

    # ── Sinks → windows binding ────────────────────────────────────

    def _bind_sinks_to_windows(self):
        """Pull the output gtksink widget out of the downstream bin
        and pack it into the projector window. HUD window is text-
        only (no video preview) — saves ~30% CPU vs running a
        second rendering branch."""
        output_sink = self.pipeline.get_by_name("output_sink")
        if output_sink is None:
            self._fail("output gtksink missing from the pipeline")
            return False
        output_widget = output_sink.get_property("widget")
        # GTK3 quirk: gtksink's widget defaults to hidden.
        output_widget.show()
        self.output_window = self._build_output_window(output_widget)
        self.hud_window = self._build_hud_window()
        self.add_window(self.output_window)
        self.add_window(self.hud_window)
        return True

    # ── Window construction ────────────────────────────────────────

    def _build_output_window(self, video_widget):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("pi-paint VJ — Output")
        win.set_default_size(CANVAS_W, CANVAS_H)
        win.add(video_widget)
        win.connect("destroy", lambda *_: self.quit())
        win.connect("key-press-event", self._on_key_press)
        return win

    def _build_hud_window(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("VJ Control")
        win.set_default_size(680, 480)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        # No video preview here — see _bind_sinks_to_windows for
        # why. The big status label below is what the operator
        # uses to track what's playing.

        # Big, unmissable "what's playing" label. The operator
        # uses this to identify which generator they're currently
        # cycling on. Bigger than the small status line below so
        # they can glance at it from across the room.
        self._big_status_label = Gtk.Label()
        self._big_status_label.set_xalign(0.0)
        box.pack_start(self._big_status_label, False, False, 0)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        self._status_label.set_line_wrap(True)
        box.pack_start(self._status_label, False, False, 0)
        self._refresh_status()

        # Key cheat sheet. Phase 6 will replace with a richer
        # panel + favourites grid; for now the operator can read
        # the active bindings off the HUD.
        keymap = Gtk.Label()
        keymap.set_markup(
            "<small>"
            "<b>Keys</b>:\n"
            "  <tt>- / =</tt>  prev / next clip\n"
            "  <tt>[ / ]</tt>  prev / next generator (cycle all)\n"
            "  <tt>A</tt>      plasma\n"
            "  <tt>S</tt>      tunnel\n"
            "  <tt>H</tt>      cells\n"
            "  <tt>K</tt>      moiré\n"
            "  <tt>`</tt>      toggle nerd stats\n"
            "  <tt>Esc</tt>    quit"
            "</small>"
        )
        keymap.set_xalign(0.0)
        box.pack_start(keymap, False, False, 0)

        win.add(box)
        win.connect("destroy", lambda *_: self.quit())
        win.connect("key-press-event", self._on_key_press)
        return win

    def _refresh_status(self):
        if self._status_label is None:
            return
        kind, what = self._current_source if self._current_source else (None, None)

        # Big "what's playing" line — name only, large + bold so
        # the operator can identify it at a glance while cycling.
        big = ""
        if kind == "clip":
            big = "▶ clip"
        elif kind == "generator":
            idx = self._current_generator_idx
            big = (f"⚡ {what}  "
                   f"<span size='small' fgcolor='#888'>"
                   f"({idx + 1}/{len(GENERATOR_ORDER)})</span>")
        if self._big_status_label is not None:
            self._big_status_label.set_markup(
                f"<span size='xx-large' weight='bold'>{big}</span>"
            )

        # Small status line — file name / phase tag.
        if kind == "clip":
            text = (f"clip {self._current_clip_idx + 1}"
                    f"/{len(self._clips)}: {what.name}")
        elif kind == "generator":
            text = f"generator: {what}"
        else:
            text = "no source"
        self._status_label.set_markup(
            f"<b>VJ Control HUD</b>\n"
            f"<small>{GLib.markup_escape_text(text)}</small>"
        )

    # ── Monitor placement ──────────────────────────────────────────

    def _move_window_to_monitor(self, window, monitor_idx, fullscreen):
        """Pin a Gtk.Window to a specific physical monitor.

        Pi setups typically have the projector on monitor 1 and the
        small operator screen on monitor 0.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return
        n = display.get_n_monitors()
        if n == 0:
            return
        idx = min(max(0, monitor_idx), n - 1)
        monitor = display.get_monitor(idx)
        geo = monitor.get_geometry()
        window.move(geo.x, geo.y)
        if fullscreen:
            window.fullscreen_on_monitor(display.get_default_screen(), idx)

    # ── Input ──────────────────────────────────────────────────────

    def _on_key_press(self, _widget, event):
        key = event.keyval
        mod = event.state

        # Quit
        if key == Gdk.KEY_Escape:
            self.quit()
            return True

        # Clip cycling — `-` and `=` (plus their shifted twins for
        # operators who Shift-mash). Always step ±1 from the
        # remembered clip index, regardless of whether the current
        # source is a clip or a generator. Generators are "off to
        # the side" — they don't move the cycle pointer, so
        # generator → `=` lands on clip[idx+1], where idx is
        # whichever clip was active before the operator picked the
        # generator. Index wraps via modulo in _install_clip.
        if key in (Gdk.KEY_minus, Gdk.KEY_underscore):
            self._install_clip(self._current_clip_idx - 1)
            return True
        if key in (Gdk.KEY_equal, Gdk.KEY_plus):
            self._install_clip(self._current_clip_idx + 1)
            return True

        # Generator cycle — `[` / `]` walks GENERATOR_ORDER. Lets
        # the operator audition the whole catalogue without
        # remembering every individual hotkey, then pick the keepers.
        if key in (Gdk.KEY_bracketleft, Gdk.KEY_braceleft):
            n = len(GENERATOR_ORDER)
            self._current_generator_idx = (self._current_generator_idx - 1) % n
            self._install_generator(GENERATOR_ORDER[self._current_generator_idx])
            return True
        if key in (Gdk.KEY_bracketright, Gdk.KEY_braceright):
            n = len(GENERATOR_ORDER)
            self._current_generator_idx = (self._current_generator_idx + 1) % n
            self._install_generator(GENERATOR_ORDER[self._current_generator_idx])
            return True

        # Direct generator hotkeys. After the operator's audition
        # pass: keep A/S/H/K bindings for the originals they
        # explicitly approved; everything else is reachable via
        # `[` / `]` until the favourites system goes in (the
        # operator wants generator favs to work the same way clip
        # favs will: tap to recall, hold to assign current).
        gen_keys = {
            Gdk.KEY_a: "plasma",
            Gdk.KEY_s: "tunnel",
            Gdk.KEY_h: "cells",
            Gdk.KEY_k: "moire",
        }
        if key in gen_keys and not (mod & Gdk.ModifierType.CONTROL_MASK):
            self._install_generator(gen_keys[key])
            return True

        return False

    # ── GStreamer bus ──────────────────────────────────────────────

    def _on_bus_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[vj.gst] ERROR: {err.message}")
            if dbg:
                print(f"[vj.gst] debug: {dbg}")
            self.quit()
        elif t == Gst.MessageType.WARNING:
            err, _ = msg.parse_warning()
            print(f"[vj.gst] warn: {err.message}")
        elif t == Gst.MessageType.EOS:
            # Only clip mode produces EOS — videotestsrc is is-live
            # so generators never end. seek_simple with FLUSH causes
            # a small frame stutter at the loop point; phase 4c/4d
            # is where we refine to a segment-seek loop.
            if self._current_source and self._current_source[0] == "clip":
                print("[vj.gst] EOS — looping current clip")
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0,
                )

    # ── Failure surface ────────────────────────────────────────────

    def _fail(self, message):
        print(f"[vj] FATAL: {message}", file=sys.stderr)
        try:
            dlg = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="pi-paint VJ failed to start",
            )
            dlg.format_secondary_text(message)
            dlg.run()
            dlg.destroy()
        except Exception:
            pass
        self.quit()
