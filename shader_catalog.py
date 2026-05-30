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
// couple of seconds (a saccade): a quick dart into place, then it
// holds rock-steady (no tremor). All from `time` — no CPU-side state.
vec2 gaze() {
    float seg = floor(time * 0.45);
    float f = fract(time * 0.45);
    vec2 prev = (hash2(vec2(seg,       1.0)) - 0.5) * 2.0;
    vec2 cur  = (hash2(vec2(seg + 1.0, 1.0)) - 0.5) * 2.0;
    float dart = smoothstep(0.0, 0.12, f);   // fast flick at segment start
    vec2 g = mix(prev, cur, dart);
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

    // --- pupil: holds steady, then snaps to a new size in a fast
    // (<0.25 s) change at a random moment in each ~2.2 s window ------
    float pd = time / 2.2;
    float pseg = floor(pd);
    float pf = fract(pd);
    float prevR = mix(0.05, 0.11, hash(vec2(pseg,       7.0)));
    float curR  = mix(0.05, 0.11, hash(vec2(pseg + 1.0, 7.0)));
    float trig  = 0.15 + 0.6 * hash(vec2(pseg, 3.0));   // random snap moment
    float rp = mix(prevR, curR, smoothstep(trig, trig + 0.09, pf));
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

SERAPHIM_SHADER = """#version 100
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

float hash(float n) { return fract(sin(n) * 43758.5453); }

// One eye in the swarm. Returns colour in .xyz and coverage in .w so
// the caller can composite it over the wings/glow behind. Each eye
// gets a unique `seed`, which drives an independent gaze (saccade +
// tremor), an independent blink, and its own iris hue — that's what
// makes the mass feel alive and chaotic rather than a clone army.
vec4 eye(vec2 p, vec2 c, float r, float seed) {
    vec2 q = (p - c) / r;
    float d = length(q);
    float cover = smoothstep(1.0, 0.94, d);
    if (cover <= 0.0) return vec4(0.0);

    float spd = 0.4 + hash(seed * 1.7) * 0.25;
    float seg = floor(time * spd + seed * 7.0);
    float f = fract(time * spd + seed * 7.0);
    vec2 gp = vec2(hash(seg + seed * 3.0) - 0.5,
                   hash(seg + seed * 3.0 + 9.0) - 0.5) * 2.0;
    vec2 gc = vec2(hash(seg + 1.0 + seed * 3.0) - 0.5,
                   hash(seg + 1.0 + seed * 3.0 + 9.0) - 0.5) * 2.0;
    vec2 gaze = mix(gp, gc, smoothstep(0.0, 0.12, f)) * 0.45;   // hold steady, no tremor

    vec2 iq = q - gaze;
    float di = length(iq);
    float ang = atan(iq.y, iq.x);

    vec3 col = vec3(0.93, 0.92, 0.86);
    col *= 0.8 + 0.2 * smoothstep(1.0, -1.0, q.y - q.x);

    float ri = 0.46;
    float hue = fract(0.07 + seed * 0.9 + time * 0.02);   // warm, gold-ish, per eye
    float fib = 0.5 + 0.5 * sin(ang * 34.0 + sin(ang * 7.0) * 2.0);
    float rad = smoothstep(0.0, ri, di);
    float val = 0.35 + 0.5 * fib * rad;
    vec3 iris = hsv2rgb(vec3(hue, 0.8, val));
    iris *= 1.0 - smoothstep(ri - 0.08, ri, di) * 0.7;     // limbal ring
    col = mix(col, iris, smoothstep(ri, ri - 0.02, di));

    // pupil holds, then snaps to a new size (<0.25 s) at a random moment,
    // independently per eye (seed-driven) — no steady gradient.
    float pd = time * 0.45 + seed * 3.0;
    float pseg = floor(pd);
    float pf = fract(pd);
    float prevR = mix(0.12, 0.26, hash(pseg + seed));
    float curR  = mix(0.12, 0.26, hash(pseg + 1.0 + seed));
    float ptrig = 0.15 + 0.6 * hash(pseg + seed * 2.0);
    float rp = mix(prevR, curR, smoothstep(ptrig, ptrig + 0.10, pf));
    col = mix(col, vec3(0.02), smoothstep(rp, rp - 0.02, di));

    vec2 hl = q - vec2(-0.18, 0.22);                        // corneal highlight
    col += exp(-dot(hl, hl) * 9.0) * 0.9;

    // Independent blink: lids of a pale-gold flesh tone sweep shut.
    float bspd = 0.13 + hash(seed * 4.1) * 0.12;
    float bp = fract(time * bspd + seed * 2.0);
    float blink = smoothstep(0.0, 0.04, bp) * smoothstep(0.16, 0.10, bp);
    float ap = mix(1.15, 0.0, blink);
    float lid = ap * (1.0 - 0.22 * q.x * q.x);
    if (abs(q.y) > lid) {
        col = vec3(0.85, 0.62, 0.40) * (0.6 + 0.4 * smoothstep(1.0, -1.0, q.y));
    }
    col = mix(col, vec3(0.4, 0.25, 0.15),
              smoothstep(0.06, 0.0, abs(abs(q.y) - lid)) * 0.5);   // lid crease

    return vec4(col, cover);
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);

    vec3 col = vec3(0.01, 0.012, 0.02);

    // Divine backglow — warm halo behind the whole apparition.
    float halo = exp(-dot(p, p) * 3.0);
    col += vec3(1.0, 0.85, 0.5) * halo * 0.5;

    // Six wings (three mirrored pairs) as glowing feathered fans,
    // opening and closing on a slow flap. abs(p.x) mirrors left/right.
    float wx = abs(p.x);
    vec2 wp = vec2(wx, p.y);
    float wrad = length(wp) + 1e-4;
    float wang = atan(wp.y, wp.x);
    float flap = sin(time * 1.6) * 0.5 + 0.5;          // 0 closed .. 1 open
    float spread = mix(0.20, 0.55, flap);              // angular half-width
    for (int w = 0; w < 3; w++) {
        float fw = float(w);
        float wc = 0.35 - fw * 0.55;                   // up / mid / down pair
        wc += sin(time * 1.6 + fw) * 0.10;             // flap sway, phase-offset
        float da = wang - wc;
        float band = exp(-da * da / (2.0 * spread * spread));
        float reach = (0.62 + 0.12 * fw) * (0.7 + 0.3 * flap);
        float along = clamp(wrad / reach, 0.0, 1.0);
        float lenfade = smoothstep(1.0, 0.2, along) * smoothstep(0.04, 0.18, wrad);
        float fcoord = (da / spread) * 5.0 + fw * 1.3;
        float feather = pow(abs(sin(fcoord * 3.14159)), 0.4);   // individual feathers
        float quill = smoothstep(0.12, 0.0, abs(fract(fcoord) - 0.5));
        float barb = 0.6 + 0.4 * sin(along * 40.0 - time + fcoord * 2.0); // barb striations
        vec3 wcol = mix(vec3(0.95, 0.80, 0.55), vec3(0.55, 0.40, 0.85), fw / 2.0);
        wcol = wcol * barb + quill * 0.3;
        col = mix(col, wcol, clamp(band * lenfade * feather * 0.9, 0.0, 1.0));
    }

    // The eye mass, in front of the wings. Golden-angle spiral so the
    // eyes pack like a sunflower head; drawn outer-first so the central
    // eyes sit on top of the pile.
    float cluster = 0.26;
    for (int i = 0; i < 14; i++) {
        float fi = float(i);
        float t = (13.0 - fi) / 14.0;                  // 1 outer .. 0 centre
        float ringr = sqrt(t) * cluster;
        float a = fi * 2.399963 + time * 0.05;         // golden angle + slow swirl
        vec2 c = vec2(cos(a), sin(a)) * ringr;
        c.y *= 0.95;
        float er = cluster * (0.20 + 0.16 * (1.0 - t)) * (0.85 + 0.3 * hash(fi + 0.5));
        vec4 e = eye(p, c, er, fi + 1.0);
        col = mix(col, e.xyz, e.w);
    }

    col *= 1.0 - smoothstep(0.5, 1.1, length(p)) * 0.5;  // vignette
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

QUASICRYSTAL_SHADER = """#version 100
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

// Sum of seven cosine plane waves at evenly spaced angles. Seven is
// odd so the lattice never repeats — that's the aperiodic shimmer.
void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    float zoom = 16.0 + sin(t * 0.13) * 5.0;     // breathing
    float ca = cos(t * 0.04), sa = sin(t * 0.04); // slow spin
    p = vec2(ca * p.x - sa * p.y, sa * p.x + ca * p.y) * zoom;

    float v = 0.0;
    for (int i = 0; i < 7; i++) {
        float ang = float(i) * 3.14159265 / 7.0;
        vec2 dir = vec2(cos(ang), sin(ang));
        v += cos(dir.x * p.x + dir.y * p.y + t * (0.5 + 0.06 * float(i)));
    }
    v /= 7.0;                            // -1..1
    float s = 0.5 + 0.5 * v;
    float bands = pow(abs(v), 0.4);      // sharpen into crystal filaments
    float hue = fract(s * 0.5 + t * 0.04);
    vec3 col = hsv2rgb(vec3(hue, 0.72, bands));
    col += vec3(smoothstep(0.9, 1.0, s)) * 0.6;  // bright interference nodes
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

KALISET_SHADER = """#version 100
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

// "Kaliset": iterate z = abs(z)/dot(z,z) - c. Folds the plane into a
// self-similar field. We read it with a SMOOTH accumulated orbit trap
// (sum of exp(-dist) over the orbit) instead of a hard min() — the min
// jumps wildly between neighbouring pixels in the chaotic fold regions
// and aliased into static. A higher inversion floor also tames the
// chaos. Result: smooth morphing galaxies, no speckle.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    vec2 z = uv * 1.3;
    float ca = cos(t * 0.05), sa = sin(t * 0.05);     // slow turn
    z = vec2(ca * z.x - sa * z.y, sa * z.x + ca * z.y);
    vec2 c = vec2(0.62 + 0.18 * sin(t * 0.13), 0.74 + 0.14 * cos(t * 0.11));
    float acc = 0.0;     // smooth glow accumulation
    float warm = 0.0;    // depth-weighted, for colour
    for (int i = 0; i < 12; i++) {
        z = abs(z) / clamp(dot(z, z), 0.03, 4.0) - c;   // higher floor = less chaos
        float w = exp(-length(z) * 4.0);                // smooth trap, no hard min
        acc += w;
        warm += w * float(i);
    }
    float glow = clamp(acc * 0.5, 0.0, 1.0);
    float depth = (acc > 0.001) ? (warm / acc) / 12.0 : 0.0;   // smooth 0..1
    float hue = fract(0.55 + depth * 0.5 + t * 0.03);
    vec3 col = hsv2rgb(vec3(hue, 0.72, glow));
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

APOLLONIAN_SHADER = """#version 100
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

// Apollonian-style packing: repeatedly fold into the unit cell and
// invert through the origin. `scale` tracks the accumulated inversion
// so we recover an approximate distance to the nested circles.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    vec2 p = uv * 1.5 + 0.12 * vec2(sin(t * 0.11), cos(t * 0.09));
    float fold = 1.05 + 0.18 * sin(t * 0.2);     // animates the gasket
    float scale = 1.0;
    float trap = 1e9;
    for (int i = 0; i < 8; i++) {
        p = -1.0 + 2.0 * fract(0.5 * p + 0.5);   // fold into [-1,1] cell
        float r2 = dot(p, p);
        float k = fold / r2;                     // circle inversion
        p *= k; scale *= k;
        trap = min(trap, abs(r2 - 0.32));
    }
    float d = length(p) / scale;                 // approx distance to structure
    float edge = smoothstep(0.06, 0.0, d);
    float hue = fract(0.1 + trap * 1.5 + t * 0.04);
    float val = edge + exp(-d * 14.0) * 0.6;
    vec3 col = hsv2rgb(vec3(hue, 0.75, clamp(val, 0.0, 1.0)));
    gl_FragColor = vec4(col, 1.0);
}
"""

HYPERBOLIC_SHADER = """#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

const float PI = 3.14159265359;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

// {p,q} tiling of the Poincare disk. Fold into a wedge of angle pi/p,
// then invert through the edge geodesic (a circle orthogonal to the
// unit circle) until inside the fundamental domain. A Mobius
// translation drifts us through hyperbolic space so tiles stream in
// from the rim.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0) * 1.05;
    float t = time;
    vec2 z = uv;                       // point in the disk
    if (dot(z, z) >= 1.0) { gl_FragColor = vec4(0.0, 0.0, 0.02, 1.0); return; }

    // Mobius translation: z -> (z + b) / (1 + conj(b) z)
    vec2 b = 0.42 * vec2(sin(t * 0.15), cos(t * 0.11));
    vec2 num = z + b;
    vec2 den = vec2(1.0 + (b.x * z.x + b.y * z.y), (b.x * z.y - b.y * z.x));
    z = vec2(num.x * den.x + num.y * den.y, num.y * den.x - num.x * den.y) / dot(den, den);

    float pP = 6.0, qQ = 4.0;
    float wedge = PI / pP;
    float cx = cos(PI / pP) / sin(PI / qQ);        // edge-circle centre on x
    float cr = sqrt(max(cx * cx - 1.0, 0.0001));   // orthogonal to unit circle
    float refl = 0.0;
    for (int i = 0; i < 12; i++) {
        float a = atan(z.y, z.x);
        float r = length(z);
        a = mod(a, 2.0 * wedge);
        a = abs(a - wedge);
        z = r * vec2(cos(a), sin(a));
        vec2 dvec = z - vec2(cx, 0.0);
        float di = dot(dvec, dvec);
        if (di < cr * cr) {
            z = vec2(cx, 0.0) + dvec * (cr * cr / di);   // invert through edge
            refl += 1.0;
        } else { break; }
    }
    float a = atan(z.y, z.x);
    float tile = mod(refl, 2.0);
    float band = 0.5 + 0.5 * sin(a * pP);
    float hue = fract(refl * 0.12 + t * 0.03);
    float val = mix(0.35, 0.95, tile) * (0.6 + 0.4 * band);
    val *= smoothstep(1.0, 0.55, dot(uv, uv));     // fade toward the rim
    vec3 col = hsv2rgb(vec3(hue, 0.6, val));
    gl_FragColor = vec4(col, 1.0);
}
"""

KIFS3D_SHADER = """#version 100
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

// Sierpinski-tetrahedron KIFS distance estimator. The fold offset
// breathes with time so the fractal morphs while the camera orbits.
float DE(vec3 z) {
    float scale = 2.0;
    vec3 off = vec3(1.0) + 0.18 * vec3(sin(time * 0.2), 0.0, cos(time * 0.17));
    for (int i = 0; i < 9; i++) {
        if (z.x + z.y < 0.0) { float tmp = -z.y; z.y = -z.x; z.x = tmp; }
        if (z.x + z.z < 0.0) { float tmp = -z.z; z.z = -z.x; z.x = tmp; }
        if (z.y + z.z < 0.0) { float tmp = -z.z; z.z = -z.y; z.y = tmp; }
        z = z * scale - off * (scale - 1.0);
    }
    return length(z) * pow(scale, -9.0);
}

void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time * 0.2;
    vec3 ro = vec3(sin(t) * 2.6, sin(t * 0.5) * 0.8, cos(t) * 2.6);
    vec3 ww = normalize(-ro);
    vec3 uu = normalize(cross(vec3(0.0, 1.0, 0.0), ww));
    vec3 vv = cross(ww, uu);
    vec3 rd = normalize(uv.x * uu + uv.y * vv + 1.6 * ww);
    float dist = 0.0; float steps = 0.0; bool hit = false;
    for (int i = 0; i < 56; i++) {
        vec3 pos = ro + rd * dist;
        float d = DE(pos);
        if (d < 0.0016) { hit = true; break; }
        dist += d; steps += 1.0;
        if (dist > 8.0) break;
    }
    vec3 col = vec3(0.02, 0.02, 0.05);
    if (hit) {
        float ao = 1.0 - steps / 56.0;             // cheap glow/AO from step count
        float hue = fract(0.6 + dist * 0.12 + time * 0.02);
        col = hsv2rgb(vec3(hue, 0.65, 0.3 + 0.7 * ao));
    } else {
        col += hsv2rgb(vec3(fract(0.6 + time * 0.02), 0.4, 0.1)) * (steps / 56.0);
    }
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

DOMAINCOLOR_SHADER = """#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

const float PI = 3.14159265359;

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
vec2 cmul(vec2 a, vec2 b) { return vec2(a.x * b.x - a.y * b.y, a.x * b.y + a.y * b.x); }
vec2 cdiv(vec2 a, vec2 b) { float d = dot(b, b) + 1e-9; return vec2(a.x * b.x + a.y * b.y, a.y * b.x - a.x * b.y) / d; }

// Domain colouring of an animated complex rational function.
// hue = phase of f(z), brightness banded by log|f| -> contour rings
// that swirl around orbiting zeros and poles.
void main() {
    vec2 z = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0) * 2.4;
    float t = time;
    vec2 z1 = 0.9 * vec2(cos(t * 0.3), sin(t * 0.3));
    vec2 z2 = 0.7 * vec2(cos(t * 0.5 + 2.0), sin(t * 0.5 + 2.0));
    vec2 p1 = 1.1 * vec2(cos(t * 0.23 + 1.0), sin(t * 0.23 + 1.0));
    vec2 num = cmul(z - z1, z + z1);
    num = cmul(num, z - z2);
    vec2 f = cdiv(num, z - p1);
    float mag = length(f);
    float phase = atan(f.y, f.x);
    float hue = fract(phase / (2.0 * PI) + 0.5 + t * 0.02);
    float rings = fract(log(mag + 1e-3) * 1.2 - t * 0.1);   // |f| contours
    float spokes = 0.5 + 0.5 * sin(phase * 6.0);            // phase wedges
    float val = 0.35 + 0.5 * rings + 0.25 * spokes;
    vec3 col = hsv2rgb(vec3(hue, 0.85, clamp(val, 0.0, 1.0)));
    gl_FragColor = vec4(col, 1.0);
}
"""

CURLFLOW_SHADER = """#version 100
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
float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    float a = hash(i), b = hash(i + vec2(1.0, 0.0)), c = hash(i + vec2(0.0, 1.0)), d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 3; i++) { v += a * noise(p); p *= 2.0; a *= 0.5; }
    return v;
}
float pot(vec2 p) { return fbm(p + vec2(0.0, time * 0.15)); }
vec2 curl(vec2 p) {                          // divergence-free field
    float e = 0.06;
    float a = pot(p + vec2(0.0, e)), b = pot(p - vec2(0.0, e));
    float c = pot(p + vec2(e, 0.0)), d = pot(p - vec2(e, 0.0));
    return vec2(a - b, d - c) / (2.0 * e);
}

// Advect each pixel through the curl field and accumulate density
// along the streamline -> ink-in-water billowing.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    vec2 start = uv * 2.5;
    vec2 q = start;
    float dens = 0.0;
    for (int i = 0; i < 12; i++) {
        q += curl(q) * 0.09;
        dens += fbm(q * 1.5);
    }
    dens /= 12.0;
    float hue = fract(0.55 + length(q - start) * 0.15 + time * 0.02);
    float val = smoothstep(0.2, 0.8, dens);
    vec3 col = hsv2rgb(vec3(hue, 0.7, val));
    col += val * val * vec3(0.2, 0.25, 0.4);
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

PHYLLOTAXIS_SHADER = """#version 100
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

// Phyllotaxis (sunflower) seed lattice. Each seed sits at angle
// i*golden and radius sqrt(i). Drifting the divergence angle a hair
// off 137.5 deg makes the spiral arms dramatically reorganise.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    float golden = 2.39996323 + 0.025 * sin(t * 0.1);   // parastichy morph
    float spread = 0.055;                                // seed spacing
    float glow = 0.0; float hueAcc = 0.0; float wsum = 0.0;
    for (int i = 1; i <= 96; i++) {
        float fi = float(i);
        float ang = fi * golden + t * 0.15;
        float rad = spread * sqrt(fi);
        vec2 c = vec2(cos(ang), sin(ang)) * rad;
        float dotr = 0.018 + 0.012 * sin(t * 1.5 + fi * 0.3);   // pulsing dots
        vec2 dd = uv - c;
        float g = exp(-dot(dd, dd) / (dotr * dotr));
        glow += g;
        hueAcc += g * fract(fi * 0.013 + t * 0.03);
        wsum += g;
    }
    float hue = (wsum > 0.0) ? hueAcc / wsum : 0.0;
    float val = smoothstep(0.0, 1.0, glow);
    vec3 col = hsv2rgb(vec3(fract(hue), 0.65, val));
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

SUNPLASMA_SHADER = """#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform float time;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i), b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0)), d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++) { v += a * noise(p); p *= 2.0; a *= 0.5; }
    return v;
}

// Hot fire ramp: black -> deep red -> orange -> yellow -> white.
vec3 firePal(float x) {
    x = clamp(x, 0.0, 1.0);
    vec3 c = vec3(1.0, 0.18, 0.02) * smoothstep(0.05, 0.45, x);
    c += vec3(0.9, 0.55, 0.0) * smoothstep(0.35, 0.72, x);
    c += vec3(0.5, 0.7, 0.2)  * smoothstep(0.6, 0.92, x);    // toward yellow
    c += vec3(0.6, 0.6, 0.8)  * smoothstep(0.85, 1.0, x);    // white-hot core
    return c;
}

// A wandering active region. Position drifts over the disk; brightness
// pulses through a birth/death cycle so spots fade in and out, which
// reads as new hot spots spawning.
vec2 spotPos(float i, float t) {
    float ph = i * 2.399;
    float rr = 0.16 + 0.16 * sin(t * 0.23 + ph);
    float aa = t * (0.10 + 0.018 * i) + ph;
    vec2 orbit = vec2(cos(aa), sin(aa)) * rr;
    orbit += 0.10 * vec2(sin(t * 0.5 + ph * 1.7), cos(t * 0.37 + ph));
    return orbit;
}
float spotLife(float i, float t) {
    return clamp(0.55 + 0.75 * sin(t * 0.3 + i * 2.3), 0.0, 1.0);
}

void main() {
    vec2 p = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;

    // Churning photosphere: domain-warped fbm so the surface roils.
    vec2 warp = vec2(fbm(p * 3.0 + vec2(t * 0.05, -t * 0.04)),
                     fbm(p * 3.0 + vec2(-t * 0.045, t * 0.05)));
    float gran = fbm(p * 4.0 + warp * 1.6 + t * 0.03);
    float heat = 0.18 + gran * 0.5;
    heat += exp(-dot(p, p) * 2.2) * 0.35;           // central solar glow

    // Hot spots + the coronal arcs that connect them.
    float arc = 0.0;
    for (int i = 0; i < 6; i++) {
        float fi = float(i);
        vec2 s0 = spotPos(fi, t);
        float l0 = spotLife(fi, t);
        float d = length(p - s0);
        heat += exp(-d * d * 130.0) * l0 * 1.3;       // the spot itself

        float nj = fi + 1.0; if (nj > 5.5) nj = 0.0;  // next spot in the ring
        vec2 s1 = spotPos(nj, t);
        float l1 = spotLife(nj, t);
        vec2 dir = s1 - s0;
        float len = length(dir) + 1e-4;
        vec2 nrm = vec2(-dir.y, dir.x) / len;
        float bulge = 0.10 + 0.08 * sin(t * 0.4 + fi);
        float best = 1e9;
        for (int sgi = 0; sgi <= 6; sgi++) {          // sample the bulging loop
            float u = float(sgi) / 6.0;
            vec2 cp = s0 + dir * u + nrm * sin(u * 3.14159) * bulge;
            best = min(best, length(p - cp));
        }
        arc += exp(-best * best * 700.0) * min(l0, l1);
    }
    heat += arc * 1.1;

    vec3 col = firePal(heat);
    col *= 1.0 - smoothstep(0.55, 1.15, length(p)) * 0.6;   // float it in space
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

MANDALA_SHADER = """#version 100
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

// Concentric petal rings, more petals further out, each ring blooming
// (its thickness pulses) and rotating — alternating directions — so it
// reads as a breathing, turning mandala. Symmetric by construction, so
// there is no chaotic noise.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    float r = length(uv);
    float a = atan(uv.y, uv.x) + t * 0.05;

    vec3 col = vec3(0.0);
    for (int k = 0; k < 6; k++) {
        float fk = float(k);
        float ringR = 0.12 + fk * 0.13;
        float petals = 6.0 + fk * 2.0;
        float bloom = 0.5 + 0.5 * sin(t * 0.5 - fk * 0.8);
        float spin = (mod(fk, 2.0) * 2.0 - 1.0) * (0.2 + fk * 0.05);
        float petal = pow(0.5 + 0.5 * cos(a * petals + t * spin), 3.0);
        float ring = smoothstep(0.055 * bloom + 0.006, 0.0, abs(r - ringR));
        float hue = fract(0.6 + fk * 0.12 + t * 0.04);
        col += hsv2rgb(vec3(hue, 0.7, 1.0)) * ring * petal;
    }
    col += hsv2rgb(vec3(fract(t * 0.1), 0.5, 1.0)) * smoothstep(0.08, 0.0, r);  // centre jewel
    col *= 1.0 - smoothstep(0.85, 1.15, r) * 0.5;
    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

DROSTESCOPE_SHADER = """#version 100
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

// Kaleidoscope fold + log-polar (Droste) coordinate, so the mirrored
// pattern self-repeats every log-radial period and zooms inward
// forever. A twist term shears it into an endless spiral tunnel.
void main() {
    vec2 uv = (v_texcoord - 0.5) * vec2(1280.0/720.0, 1.0);
    float t = time;
    float r = length(uv) + 1e-4;
    float a = atan(uv.y, uv.x);

    float N = 6.0;
    float seg = 6.2831853 / N;
    a = mod(a, seg);
    a = abs(a - seg * 0.5);                  // dihedral mirror

    float lr = log(r);
    float v = fract((lr - t * 0.25) / 0.9 + 0.3 * (a / seg));   // Droste repeat + twist

    float ring = pow(0.5 + 0.5 * cos(v * 6.2831853), 2.0);
    float spokes = 0.5 + 0.5 * cos(a * N * 2.0 + lr * 3.0);
    float motif = ring * (0.5 + 0.5 * spokes);

    float hue = fract(0.55 + v + a * 0.1 + t * 0.04);
    vec3 col = hsv2rgb(vec3(hue, 0.78, motif));
    col += hsv2rgb(vec3(fract(t * 0.1), 0.4, 1.0)) * smoothstep(0.3, 0.0, r) * 0.5;  // tunnel core
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
    'seraphim': SERAPHIM_SHADER,
    'quasicrystal': QUASICRYSTAL_SHADER,
    'kaliset': KALISET_SHADER,
    'mandala': MANDALA_SHADER,
    'drostescope': DROSTESCOPE_SHADER,
    'apollonian': APOLLONIAN_SHADER,
    'hyperbolic': HYPERBOLIC_SHADER,
    'kifs3d': KIFS3D_SHADER,
    'domaincolor': DOMAINCOLOR_SHADER,
    'curlflow': CURLFLOW_SHADER,
    'phyllotaxis': PHYLLOTAXIS_SHADER,
    'sunplasma': SUNPLASMA_SHADER,
    'donut': DONUT_SHADER,
}

GPU_GENERATOR_ORDER = list(GPU_GENERATORS.keys())
