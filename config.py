from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    width: int = 854
    height: int = 480
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
