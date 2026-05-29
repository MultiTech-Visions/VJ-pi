"""GLSL generator catalogue used by the out-of-process GPU worker.

Generated from the current GStreamer rewrite shader catalogue, but
kept dependency-free so the worker can run under system Python.
"""

PLASMA_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nvoid main() {\n    vec2 p = v_texcoord * 8.0;\n    float t = time;\n    float v = (sin(p.x + t) + sin(p.y + t * 1.3)\n             + sin((p.x + p.y) * 0.5 + t * 0.7)\n             + sin(sqrt(p.x*p.x + p.y*p.y) + t * 1.7)) * 0.25;\n    v = (v + 1.0) * 0.5;\n    float hue = fract(v + t / 9.0);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 1.0, 1.0)), 1.0);\n}\n'

TUNNEL_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nconst float PI = 3.14159265358979;\n\nvoid main() {\n    vec2 pix = (v_texcoord - 0.5) * vec2(1280.0, 720.0);\n    float r = length(pix) + 1.0;\n    float a = atan(pix.y, pix.x);\n    float u = mod(200.0 / r + time * 2.0, 1.0);\n    float v_ = (a / PI + 1.0) * 0.5;\n    float chk = mod(floor(u * 8.0) + floor(v_ * 16.0), 2.0);\n    float hue = fract(v_ + time / 6.0);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 1.0, chk)), 1.0);\n}\n'

CELLS_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nconst float PI = 3.14159265358979;\n\nvoid main() {\n    vec2 pix = v_texcoord * vec2(1280.0, 720.0);\n    float t = time;\n    float scale = 0.038;\n    float u = pix.x * scale + sin(pix.y * scale * 0.6 + t)      * 0.4;\n    float vv = pix.y * scale + cos(pix.x * scale * 0.6 + t * 1.1) * 0.4;\n    float pat = abs(sin(u * PI) * sin(vv * PI));\n    pat = pow(pat, 0.6);\n    float hue = fract((u * 28.0 + t * 14.0) / 180.0);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.82, pat)), 1.0);\n}\n'

MOIRE_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nvoid main() {\n    vec2 pix = v_texcoord * vec2(1280.0, 720.0);\n    vec2 ctr = vec2(640.0, 360.0);\n    float t = time;\n    float ox = 1280.0 * 0.10;\n    float oy =  720.0 * 0.10;\n    vec2 c1 = ctr + vec2(sin(t * 0.5) * ox, cos(t * 0.4) * oy);\n    vec2 c2 = ctr - vec2(sin(t * 0.5) * ox, cos(t * 0.4) * oy);\n    float spacing = 14.0;\n    float r1 = distance(pix, c1) / spacing;\n    float r2 = distance(pix, c2) / spacing;\n    float pat = (sin(r1 + t * 2.0) + sin(r2 - t * 1.5)) * 0.25 + 0.5;\n    float hue = fract(pat + t * 22.0 / 180.0);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.82, pat)), 1.0);\n}\n'

TRUCHET_SHADER = "#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n// Snake positions pushed from CPU each tick. Sixteen separate\n// vec2 uniforms because GstStructure can't carry bracket-named\n// fields from Python — we copy them into a local array at the\n// top of main() and loop over that instead. Head is snake_15\n// (current); snake_0 is the tail. Coords are in normalized\n// (0..1) v_texcoord space, matching the texture the maze\n// itself is drawn in. The CPU walks the truchet arc graph so\n// each position sits exactly on an arc (the snake never\n// crosses a boundary line). If the uniforms haven't been set\n// yet they default to (0,0), which puts the snake briefly at\n// the top-left corner before the first CPU tick fires.\nuniform vec2 snake_0,  snake_1,  snake_2,  snake_3;\nuniform vec2 snake_4,  snake_5,  snake_6,  snake_7;\nuniform vec2 snake_8,  snake_9,  snake_10, snake_11;\nuniform vec2 snake_12, snake_13, snake_14, snake_15;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nfloat rand(vec2 p) {\n    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);\n}\n\nvoid main() {\n    float scale = 50.0;\n    vec2 pix = v_texcoord * vec2(1280.0, 720.0) / scale;\n    vec2 gid = floor(pix);\n    vec2 gp = fract(pix);\n\n    // Slow snaps — tile rotations only re-shuffle once every\n    // ~20 seconds. The maze is stable in between, so the snake\n    // gets time to actually explore it.\n    float r = rand(gid + floor(time / 20.0));\n    if (r > 0.5) gp.x = 1.0 - gp.x;\n\n    float d1 = distance(gp, vec2(0.0));\n    float d2 = distance(gp, vec2(1.0));\n    float ring = max(smoothstep(0.06, 0.0, abs(d1 - 0.5)),\n                     smoothstep(0.06, 0.0, abs(d2 - 0.5)));\n\n    // Copy 16 individually-named uniforms into a local array so\n    // the loop can use a dynamic index. (Uniform arrays with\n    // dynamic indices aren't portable in GLES 2.0; local arrays\n    // are.) Each entry is a snake position the CPU placed exactly\n    // on a truchet arc.\n    vec2 snake[16];\n    snake[0]  = snake_0;   snake[1]  = snake_1;\n    snake[2]  = snake_2;   snake[3]  = snake_3;\n    snake[4]  = snake_4;   snake[5]  = snake_5;\n    snake[6]  = snake_6;   snake[7]  = snake_7;\n    snake[8]  = snake_8;   snake[9]  = snake_9;\n    snake[10] = snake_10;  snake[11] = snake_11;\n    snake[12] = snake_12;  snake[13] = snake_13;\n    snake[14] = snake_14;  snake[15] = snake_15;\n\n    // Trail: glow weight ramps tail→head so the body fades.\n    float glow = 0.0;\n    for (int i = 0; i < 16; i++) {\n        float d = distance(v_texcoord, snake[i]);\n        float age_weight = float(i + 1) / 16.0;\n        glow += exp(-d * 80.0) * age_weight;\n    }\n\n    // Tight bright ball at the head.\n    float head = exp(-distance(v_texcoord, snake[15]) * 120.0);\n\n    // Maze: full-brightness coloured arcs at their natural hue.\n    // (Earlier dim-base version was too murky.)\n    float hue = fract(rand(gid) * 0.7 + time * 0.05);\n    vec3 arc = hsv2rgb(vec3(hue, 0.85, ring));\n\n    // Where the snake's body overlaps an arc, blend the arc\n    // toward white. clamp(...,0,1) caps it: arcs the snake is\n    // currently ON go fully white, ones recently passed turn\n    // toward white proportionally, ones the snake hasn't been\n    // near keep their natural colour.\n    float white_amount = clamp(ring * glow * 2.0, 0.0, 1.0);\n    vec3 col = mix(arc, vec3(1.0), white_amount);\n\n    // Head ball sits on top of everything, bright white.\n    col = clamp(col + vec3(head * 0.95), 0.0, 1.0);\n\n    gl_FragColor = vec4(col, 1.0);\n}\n"

VORONOI_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nvec2 hash2(vec2 p) {\n    return fract(sin(vec2(dot(p, vec2(127.1, 311.7)),\n                          dot(p, vec2(269.5, 183.3)))) * 43758.5453);\n}\n\nvoid main() {\n    float scale = 80.0;\n    vec2 pix = v_texcoord * vec2(1280.0, 720.0) / scale;\n    vec2 gid = floor(pix);\n    vec2 gp = fract(pix);\n    float md1 = 999.0;\n    float md2 = 999.0;\n    vec2 mcell;\n    for (int j = -1; j <= 1; j++) {\n        for (int i = -1; i <= 1; i++) {\n            vec2 n = vec2(float(i), float(j));\n            vec2 r = hash2(gid + n);\n            r = 0.5 + 0.5 * sin(time * 0.3 + 6.283 * r);\n            float d = distance(n + r, gp);\n            if (d < md1) { md2 = md1; md1 = d; mcell = gid + n; }\n            else if (d < md2) { md2 = d; }\n        }\n    }\n    float edge = smoothstep(0.02, 0.08, md2 - md1);\n    float hue = fract(hash2(mcell).x + time * 0.05);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.78, edge)), 1.0);\n}\n'

HEXGRID_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nvoid main() {\n    float scale = 50.0;\n    vec2 pix = v_texcoord * vec2(1280.0, 720.0) / scale;\n    vec2 s = vec2(1.0, 1.7320508);\n    vec2 a = mod(pix, s) - s * 0.5;\n    vec2 b = mod(pix + s * 0.5, s) - s * 0.5;\n    vec2 g = dot(a, a) < dot(b, b) ? a : b;\n    float d = length(g);\n    vec2 cell = pix - g;\n    float pulse = 0.5 + 0.5 * sin(time * 1.5 + cell.x * 0.5 + cell.y * 0.3);\n    float ring = smoothstep(0.5, 0.4, d) * pulse;\n    float hue = fract(cell.x * 0.1 + cell.y * 0.07 + time * 0.05);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.8, ring)), 1.0);\n}\n'

ROTOZOOM_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nvoid main() {\n    vec2 p = (v_texcoord - 0.5) * vec2(1280.0, 720.0);\n    float zoom = 0.5 + sin(time * 0.3) * 0.3;\n    float a = time * 0.2;\n    float ca = cos(a), sa = sin(a);\n    vec2 q = vec2(p.x * ca - p.y * sa, p.x * sa + p.y * ca) * zoom;\n    float chk = mod(floor(q.x / 40.0) + floor(q.y / 40.0), 2.0);\n    float hue = fract(time * 0.1 + chk * 0.5);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.9, chk)), 1.0);\n}\n'

WARPGRID_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nvoid main() {\n    vec2 pix = v_texcoord * vec2(1280.0, 720.0);\n    float t = time;\n    pix.x += sin(pix.y * 0.02 + t) * 30.0;\n    pix.y += cos(pix.x * 0.02 + t * 1.3) * 30.0;\n    vec2 g = fract(pix / 60.0);\n    float line = min(min(g.x, g.y), min(1.0 - g.x, 1.0 - g.y));\n    float bright = smoothstep(0.04, 0.0, line);\n    float hue = fract(pix.x * 0.001 + pix.y * 0.001 + t * 0.1);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.7, bright)), 1.0);\n}\n'

MARBLE_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nfloat hash(vec2 p) {\n    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);\n}\n\nfloat noise(vec2 p) {\n    vec2 i = floor(p);\n    vec2 f = fract(p);\n    f = f * f * (3.0 - 2.0 * f);\n    float a = hash(i);\n    float b = hash(i + vec2(1.0, 0.0));\n    float c = hash(i + vec2(0.0, 1.0));\n    float d = hash(i + vec2(1.0, 1.0));\n    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);\n}\n\nfloat fbm(vec2 p) {\n    float v = 0.0;\n    float amp = 0.5;\n    for (int i = 0; i < 5; i++) {\n        v += amp * noise(p);\n        p *= 2.0;\n        amp *= 0.5;\n    }\n    return v;\n}\n\nvoid main() {\n    vec2 p = v_texcoord * 4.0;\n    p.x += time * 0.1;\n    float n = fbm(p);\n    n = fbm(p + vec2(n * 2.0));\n    float v = pow(abs(sin(n * 8.0 + time * 0.2)), 0.5);\n    vec3 c = mix(vec3(0.08, 0.10, 0.18), vec3(0.95, 0.97, 1.0), v);\n    gl_FragColor = vec4(c, 1.0);\n}\n'

CAUSTICS_SHADER = "#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\n#define TAU 6.28318530718\n\nvoid main() {\n    float t = time * 0.5 + 23.0;\n    vec2 uv = v_texcoord;\n    vec2 p = mod(uv * TAU * 2.0, TAU) - 250.0;\n    vec2 i = p;\n    float c = 1.0;\n    float inten = 0.005;\n    for (int n = 0; n < 5; n++) {\n        float tt = t * (1.0 - (3.5 / float(n + 1)));\n        i = p + vec2(cos(tt - i.x) + sin(tt + i.y),\n                     sin(tt - i.y) + cos(tt + i.x));\n        c += 1.0 / length(vec2(p.x / (sin(i.x + tt) / inten),\n                                p.y / (cos(i.y + tt) / inten)));\n    }\n    c /= 5.0;\n    c = 1.17 - pow(c, 1.4);\n    vec3 col = vec3(pow(abs(c), 8.0));\n    // Tint toward pool-blue rather than pure white — the floor\n    // we're seeing the caustics ON has its own colour.\n    col = clamp(col + vec3(0.0, 0.35, 0.5), 0.0, 1.0);\n    gl_FragColor = vec4(col, 1.0);\n}\n"

SPIRAL_SHADER = '#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\n\nvec3 hsv2rgb(vec3 c) {\n    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);\n    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);\n    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);\n}\n\nconst float PI = 3.14159265358979;\n\nvoid main() {\n    vec2 p = (v_texcoord - 0.5) * vec2(1280.0, 720.0);\n    float r = length(p);\n    float a = atan(p.y, p.x);\n    float arms = 5.0;\n    float v = sin(a * arms + log(r + 1.0) * 2.0 - time * 1.5);\n    v = pow(abs(v), 1.5);\n    v *= smoothstep(0.0, 100.0, r) * smoothstep(800.0, 400.0, r);\n    float hue = fract(a / (2.0 * PI) + time * 0.05);\n    gl_FragColor = vec4(hsv2rgb(vec3(hue, 0.85, v)), 1.0);\n}\n'

LINEA_SHADER = """#version 100
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

float lineField(vec2 p, float a, float freq, float phase) {
    vec2 d = vec2(cos(a), sin(a));
    float v = sin(dot(p, d) * freq + phase);
    return smoothstep(0.915, 1.0, abs(v));
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    float l = 0.0;
    l += lineField(p + vec2(0.05 * sin(t * 0.3), 0.03 * cos(t * 0.2)),
                   0.2 + sin(t * 0.13) * 0.4, 34.0, t * 1.7);
    l += lineField(p, 1.6 + cos(t * 0.17) * 0.5, 42.0, -t * 1.2);
    l += lineField(p + vec2(sin(t * 0.2), cos(t * 0.25)) * 0.08,
                   2.5, 25.0, t * 0.9);
    l += lineField(p + vec2(cos(t * 0.16), sin(t * 0.11)) * 0.05,
                   -0.7 + sin(t * 0.19) * 0.3, 55.0, t * 0.55);
    float grid = clamp(l, 0.0, 1.0);
    float glow = min(l * 0.45, 1.0);
    float hue = fract(0.56 + t * 0.035 + p.x * 0.08 + p.y * 0.05);
    vec3 col = hsv2rgb(vec3(hue, 0.82, grid));
    col += hsv2rgb(vec3(hue + 0.08, 0.70, glow)) * 0.35;
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

GRAYSCOTT_SHADER = """#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

vec3 palette(float t) {
    vec3 a = vec3(0.05, 0.07, 0.10);
    vec3 b = vec3(0.45, 0.50, 0.55);
    vec3 c = vec3(1.00, 0.78, 0.48);
    vec3 d = vec3(0.05, 0.28, 0.42);
    return a + b * cos(6.28318 * (c * t + d));
}

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
        p *= 2.03;
        amp *= 0.5;
    }
    return v;
}

float chemical(vec2 p) {
    float t = time * 0.055;
    p += vec2(fbm(p * 0.75 + t), fbm(p * 0.82 - t)) * 1.4;
    float a = fbm(p * 2.6 + vec2(t * 1.2, -t * 0.7));
    float b = fbm(p * 5.4 - vec2(t * 0.8, t * 1.1));
    float c = fbm(p * 10.8 + vec2(a * 2.0, b * 2.0));
    return a * 0.58 + b * 0.31 + c * 0.22;
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float v = chemical(p * 3.2);
    float bands = sin((v + length(p) * 0.08 - time * 0.018) * 34.0);
    float membrane = smoothstep(0.30, 0.0, abs(bands));
    float fill = smoothstep(0.25, 0.70, v);
    float pits = smoothstep(0.72, 0.92, chemical(p * 6.4 + 4.0));
    float ridges = max(membrane, smoothstep(0.16, 0.0, abs(fract(v * 7.0) - 0.5)));
    vec3 col = mix(vec3(0.030, 0.055, 0.075), palette(v * 0.7 + time * 0.012), fill);
    col = mix(col, vec3(0.00, 0.78, 0.98), ridges * 0.82);
    col = mix(col, vec3(1.00, 0.94, 0.42), membrane * 0.62);
    col *= 0.88 + pits * 0.34;
    col += membrane * 0.10;
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

LENIA_SHADER = """#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(41.0, 289.0))) * 45758.5453);
}

float orb(vec2 p, vec2 c, float r, float wobble) {
    vec2 q = p - c;
    float a = atan(q.y, q.x);
    float d = length(q);
    float edge = r * (1.0 + 0.12 * sin(a * 5.0 + wobble) + 0.07 * sin(a * 9.0 - wobble * 0.7));
    return exp(-pow(d / edge, 2.5));
}

float creature(vec2 p, vec2 c, float seed) {
    float t = time * (0.16 + seed * 0.035);
    vec2 drift = vec2(sin(t + seed * 5.1), cos(t * 0.83 + seed * 3.7)) * 0.11;
    c += drift;
    float body = 0.0;
    body += orb(p, c, 0.145, time * 0.9 + seed);
    body += orb(p, c + vec2(cos(t * 1.7), sin(t * 1.4)) * 0.105, 0.082, -time + seed);
    body += orb(p, c + vec2(cos(t * 1.1 + 2.1), sin(t * 1.5 + 1.4)) * 0.125, 0.070, time * 1.3);
    body += orb(p, c + vec2(cos(t * 1.3 + 4.2), sin(t * 1.2 + 2.7)) * 0.110, 0.056, -time * 1.1);
    return clamp(body, 0.0, 1.0);
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float field = 0.0;
    for (int y = -1; y <= 1; y++) {
        for (int x = -2; x <= 2; x++) {
            vec2 cell = vec2(float(x), float(y));
            float h = hash(cell);
            vec2 c = cell * vec2(0.42, 0.36) + vec2(hash(cell + 3.1), hash(cell + 8.7)) * 0.18;
            field += creature(p, c, h) * (0.55 + h * 0.45);
        }
    }
    field = clamp(field, 0.0, 1.0);
    float skin = smoothstep(0.18, 0.72, field);
    float rim = smoothstep(0.08, 0.0, abs(field - 0.38));
    float core = smoothstep(0.62, 0.96, field);
    vec3 bg = vec3(0.015, 0.018, 0.026);
    vec3 flesh = mix(vec3(0.10, 0.50, 0.80), vec3(0.92, 0.35, 0.72), 0.5 + 0.5 * sin(time * 0.18));
    vec3 col = mix(bg, flesh, skin);
    col += rim * vec3(0.75, 0.95, 1.00);
    col += core * vec3(0.95, 0.85, 0.35) * 0.45;
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

DONUT_SHADER = "#version 100\n#ifdef GL_ES\nprecision highp float;\n#endif\nvarying vec2 v_texcoord;\nuniform float time;\nuniform sampler2D tex;\n\nconst float PI = 3.14159265359;\n\nfloat sdTorus(vec3 p, vec2 t) {\n    vec2 q = vec2(length(p.xz) - t.x, p.y);\n    return length(q) - t.y;\n}\n\nvec3 rotY(vec3 p, float a) {\n    float c = cos(a), s = sin(a);\n    return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z);\n}\nvec3 rotX(vec3 p, float a) {\n    float c = cos(a), s = sin(a);\n    return vec3(p.x, c * p.y - s * p.z, s * p.y + c * p.z);\n}\n\nfloat map(vec3 p) {\n    p = rotY(p, time * 0.5);\n    p = rotX(p, time * 0.3);\n    return sdTorus(p, vec2(1.0, 0.4));\n}\n\n// Map a 3D point on the torus surface to a (u, v) coordinate\n// suitable for sampling the input texture. Inverts the rotation\n// we applied in `map` so the texture sticks to the torus rather\n// than spinning past it.\nvec2 torusUV(vec3 p_world) {\n    vec3 p = rotX(p_world, -time * 0.3);\n    p = rotY(p, -time * 0.5);\n    float u = atan(p.z, p.x);            // -PI..PI around major\n    vec2 q = vec2(length(p.xz), p.y);\n    vec2 dq = q - vec2(1.0, 0.0);        // R = 1.0\n    float v = atan(dq.y, dq.x);          // -PI..PI around minor\n    return vec2((u + PI) / (2.0 * PI),\n                (v + PI) / (2.0 * PI));\n}\n\nvoid main() {\n    vec2 uv_scr = v_texcoord - 0.5;\n    uv_scr.x *= 1280.0 / 720.0;\n    vec3 ro = vec3(0.0, 0.0, -3.0);\n    vec3 rd = normalize(vec3(uv_scr, 1.0));\n    float t = 0.0;\n    bool hit = false;\n    for (int i = 0; i < 64; i++) {\n        vec3 pos = ro + rd * t;\n        float d = map(pos);\n        if (d < 0.001) { hit = true; break; }\n        t += d;\n        if (t > 10.0) break;\n    }\n    if (!hit) {\n        gl_FragColor = vec4(0.0, 0.0, 0.05, 1.0);\n        return;\n    }\n    vec3 hit_pos = ro + rd * t;\n    vec2 tex_uv = torusUV(hit_pos);\n    // Slow scroll around the major circumference — the image\n    // wraps around the donut like a label on a tin can.\n    tex_uv.x = fract(tex_uv.x + time * 0.05);\n    vec3 col = texture2D(tex, tex_uv).rgb;\n    // Simple depth darken so the back of the donut isn't full\n    // bright — gives it a hint of 3D form.\n    float depth = t / 10.0;\n    col *= 1.0 - depth * 0.4;\n    gl_FragColor = vec4(col, 1.0);\n}\n"

EYEBALL_SHADER = """#version 100
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

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

vec2 hash2(vec2 p) {
    return fract(sin(vec2(dot(p, vec2(127.1, 311.7)),
                          dot(p, vec2(269.5, 183.3)))) * 43758.5453);
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
    for (int i = 0; i < 4; i++) {
        v += amp * noise(p);
        p *= 2.0;
        amp *= 0.5;
    }
    return v;
}

// Where is the eye looking? It snaps to a fresh random target every
// couple of seconds (a saccade): a quick dart into place, then a
// hold, plus a tiny tremor so it never sits perfectly still. All
// derived from `time` — no CPU-side state needed.
vec2 gaze() {
    float seg = floor(time * 0.45);
    float f = fract(time * 0.45);
    vec2 prev = (hash2(vec2(seg,       1.0)) - 0.5) * 2.0;
    vec2 cur  = (hash2(vec2(seg + 1.0, 1.0)) - 0.5) * 2.0;
    float dart = smoothstep(0.0, 0.12, f);   // fast flick at segment start
    vec2 g = mix(prev, cur, dart);
    g += 0.03 * vec2(sin(time * 13.0), cos(time * 11.0));  // tremor
    return g * 0.12;                          // keep iris inside the sclera
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);

    vec3 bg = vec3(0.015, 0.02, 0.03);
    vec3 col = bg;

    float R = 0.42;                 // eyeball radius
    float d = length(p);
    float eyeMask = smoothstep(R, R - 0.01, d);

    // --- sclera (white of the eye) with faint red veins ---------
    vec3 sclera = vec3(0.95, 0.95, 0.93);
    float veins = fbm(p * 9.0 + 5.0);
    veins = smoothstep(0.55, 0.75, veins);
    veins *= smoothstep(0.10, R, d);        // sparse near the iris, more at the rim
    sclera = mix(sclera, vec3(0.85, 0.25, 0.22), veins * 0.45);
    sclera *= 0.82 + 0.18 * smoothstep(R, -R, p.y - p.x);  // soft top-left lighting

    vec3 eye = sclera;

    // --- iris ---------------------------------------------------
    vec2 g = gaze();
    vec2 q = p - g;
    float di = length(q);
    float ang = atan(q.y, q.x);
    float ri = 0.17;                // iris radius

    float irisHue = fract(0.58 + time * 0.02);     // slow cool-tone drift
    float fib  = 0.5 + 0.5 * sin(ang * 38.0 + sin(ang * 7.0) * 2.0);
    float fib2 = 0.5 + 0.5 * sin(ang * 90.0 - di * 30.0);
    float fiber = mix(fib, fib2, 0.4);             // radial fibres
    float radial = smoothstep(0.0, ri, di);        // darker centre → bright edge
    float irisVal = 0.35 + 0.5 * fiber * radial;
    vec3 iris = hsv2rgb(vec3(irisHue + fiber * 0.04, 0.75, irisVal));
    iris *= 1.0 - smoothstep(ri - 0.03, ri, di) * 0.7;   // dark limbal ring
    float irisMask = smoothstep(ri, ri - 0.008, di);
    eye = mix(eye, iris, irisMask);

    // --- pupil (dilates a touch for some life) ------------------
    float rp = 0.062 + 0.016 * sin(time * 0.6);
    float pupilMask = smoothstep(rp, rp - 0.006, di);
    eye = mix(eye, vec3(0.02), pupilMask);

    // --- corneal highlights (fixed, like real reflections) ------
    vec2 hl = p - vec2(-0.07, 0.09);
    eye += exp(-dot(hl, hl) * 240.0) * 0.9;
    vec2 hl2 = p - vec2(0.05, -0.04);
    eye += exp(-dot(hl2, hl2) * 900.0) * 0.3;

    col = mix(bg, eye, eyeMask);

    // --- blink: lids sweep shut briefly every ~6s ---------------
    float bp = fract(time * 0.16);
    float blink = smoothstep(0.0, 0.03, bp) * smoothstep(0.11, 0.07, bp);
    float ap = mix(R + 0.05, 0.0, blink);          // vertical aperture
    float lidCurve = ap * (1.0 - 0.25 * (p.x / R) * (p.x / R));
    float lidMask = (abs(p.y) > lidCurve) ? eyeMask : 0.0;
    vec3 lid = vec3(0.45, 0.30, 0.26);
    lid *= 0.7 + 0.3 * smoothstep(R, -R, p.y);
    float crease = smoothstep(0.02, 0.0, abs(abs(p.y) - lidCurve)) * eyeMask;
    col = mix(col, lid, lidMask);
    col = mix(col, lid * 0.5, crease * 0.6);

    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

GPU_GENERATORS = {
    'plasma': PLASMA_SHADER,
    'tunnel': TUNNEL_SHADER,
    'cells': CELLS_SHADER,
    'moire': MOIRE_SHADER,
    'truchet': TRUCHET_SHADER,
    'voronoi': VORONOI_SHADER,
    'hexgrid': HEXGRID_SHADER,
    'rotozoom': ROTOZOOM_SHADER,
    'warpgrid': WARPGRID_SHADER,
    'marble': MARBLE_SHADER,
    'caustics': CAUSTICS_SHADER,
    'spiral': SPIRAL_SHADER,
    'linea': LINEA_SHADER,
    'lenia': LENIA_SHADER,
    'grayscott': GRAYSCOTT_SHADER,
    'eyeball': EYEBALL_SHADER,
    'donut': DONUT_SHADER,
}

GPU_GENERATOR_ORDER = list(GPU_GENERATORS.keys())
