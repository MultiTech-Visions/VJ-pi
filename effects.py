"""Per-frame effect context shared by the GPU renderer.

All the heavy lifting (generatives, FX, compositing) now lives in
`gpu.py` as shader programs. This file is left as the canonical home of
`EffectContext` — a tiny struct that bundles render size, wall-time,
and the operator's PARAM X/Y values — so engine code, mapping-group
composition, and any future per-pass shader uniforms all pull from one
place.
"""


class EffectContext:
    """Per-frame state passed into the GPU pipeline.

    `px`, `py` are user-tuned parameters in 0..1 (driven by the arrow
    keys — see Engine.update_params_from_keys). `t` is wall-clock
    seconds since the engine started, offset per-group in mapping mode
    so identical generatives don't lock-step.
    """

    __slots__ = ("w", "h", "t", "px", "py")

    def __init__(self, w, h, t, params):
        self.w = w
        self.h = h
        self.t = t
        self.px, self.py = params
