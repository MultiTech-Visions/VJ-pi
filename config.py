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

    @property
    def clips_dir(self) -> Path:
        return self.assets_dir / "clips"

    @property
    def overlays_dir(self) -> Path:
        return self.assets_dir / "overlays"
