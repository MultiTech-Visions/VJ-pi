from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    # Render resolution. assets/Process Assets.sh reads these to decide
    # what size to bake clips to, so KEEP THESE IN SYNC with main.py's
    # argparse defaults — otherwise pre-processed clips get pointlessly
    # resized every frame at runtime.
    width: int = 1280
    height: int = 720
    fps: int = 30
    fullscreen: bool = False
    display: int = 0
    assets_dir: Path = Path(__file__).parent / "assets"

    # Generatives are smooth procedural patterns — they don't benefit
    # from pixel-perfect rendering at canvas resolution. Render them at
    # this fraction of (width, height) and let the placement step's
    # cv2.resize upscale for free. Default 0.5 keeps each generative
    # frame at 1/4 the pixel count, which is roughly a 4× speedup on
    # the heavy ones (waves / moire / metaballs). 1.0 = no scaling.
    gen_render_scale: float = 0.5

    # Per-group FX (kaleidoscope, edges, rgb_split…) cost the same per
    # output pixel regardless of source detail, and in mapping mode the
    # result is warped onto a quad anyway. Run FX-bearing group sources at
    # this fraction of canvas first — 0.5 is ~4× cheaper on kaleidoscope,
    # the heaviest effect. 1.0 = run FX at full source resolution (sharper).
    fx_render_scale: float = 0.5

    # Mapping mode renders each group's source serially, then warps/resizes
    # it onto its quad — the warp/resize is pure cv2 (GIL released), so it
    # parallelises across cores. With >1, the per-group warp runs in a thread
    # pool (clip decode / generators / masks stay serial — their pools aren't
    # thread-safe). 1 = serial (default). Try 3-4 on a 4-core Pi 5.
    mapping_threads: int = 1

    # Interpolation for the final canvas→display upscale: "linear" (fast,
    # default) or "cubic" (sharper, slower). On a moving projection the two
    # are hard to tell apart, so linear buys back several ms/frame.
    display_filter: str = "linear"

    @property
    def clips_dir(self) -> Path:
        return self.assets_dir / "clips"

    @property
    def overlays_dir(self) -> Path:
        return self.assets_dir / "overlays"
