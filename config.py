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

    @property
    def clips_dir(self) -> Path:
        return self.assets_dir / "clips"

    @property
    def overlays_dir(self) -> Path:
        return self.assets_dir / "overlays"
