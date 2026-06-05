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

    # Scale the output to the projector on the GPU instead of the CPU. When
    # on, the projector's window owns the process's single SDL renderer and
    # stretches the render canvas to the display in hardware (so 'disp' cost
    # is ~independent of projector resolution — crisp 2K/4K for free). The
    # control HUD then renders in plain software (it's small and never
    # scales). Exactly one GL context either way — never two (the V3D rule).
    gpu_scale: bool = False

    # Decode clips with the Pi 5 hardware HEVC decoder via an out-of-process
    # gl worker (hevc_clips.HevcClipPool) instead of OpenCV software decode.
    # Reads from clips_hevc/ (clips baked to exactly 2048x1152 — the geometry
    # the gl path handles). Pair with --width 2048 --height 1152 --gpu-scale.
    hevc: bool = False

    # Live USB-webcam base layer (toggled with `\` in the app). camera_device
    # < 0 auto-probes /dev/video0-5 for the first one that delivers frames, so
    # the operator never needs to know an index. camera_mirror flips the feed
    # left/right (selfie-natural) — toggle live with Shift+\.
    camera_device: int = -1
    camera_size: tuple = (1280, 720)
    camera_mirror: bool = True

    @property
    def clips_dir(self) -> Path:
        return self.assets_dir / "clips"

    @property
    def hevc_clips_dir(self) -> Path:
        return self.assets_dir / "clips_hevc"

    @property
    def overlays_dir(self) -> Path:
        return self.assets_dir / "overlays"
