# PC Clip Baker — make Pi-5 HEVC clips on your PC

The Pi 5 can **hardware-decode HEVC (H.265)** but can't hardware-*encode* it,
and software HEVC encoding on the Pi is painfully slow. So bake the library
here, on a PC with a real GPU encoder (your RTX 4090's NVENC), then copy the
finished clips to the Pi.

The output is tuned to exactly what the Pi already decodes in hardware:
HEVC, 8-bit `yuv420p`, `main` profile, `hvc1` tag, faststart, scaled to fit
within **1920×1080**, **30 fps**.

## Use it

1. Put your source clips (any format) in the **`input`** folder next to the
   `.bat`.
2. Double-click **`Bake Clips.bat`**.
3. Watch the progress bars. When it finishes, the HEVC `.mp4`s are in the
   **`output`** folder.
4. Copy the `output` clips onto the Pi (into the VJ clip library folder we'll
   wire up for HEVC).

Already-baked files are skipped, so you can add more clips and re-run without
redoing everything.

## Requirements (one-time, on the PC)

- **Python 3** — https://www.python.org/downloads/ (tick "Add to PATH").
- **ffmpeg full build** with NVENC — https://www.gyan.dev/ffmpeg/builds/
  (`ffmpeg-release-full`). Unzip and add its `bin` folder to PATH.

The `.bat` checks for both and tells you what's missing. If NVENC isn't
present it automatically falls back to CPU encoding (slower) so it still works.

## Tuning (optional)

Defaults are good. To override, run from a terminal, e.g.:

```
python bake_clips.py --height 720 --cq 20 --preset p6
```

- `--cq` quality, lower = better/bigger (default 23)
- `--height` / `--width` fit box (default 1080 / 1920)
- `--fps` frame-rate cap (default 30)
- `--preset` NVENC speed/quality `p1`..`p7` (default `p5`)
- `--encoder` `hevc_nvenc` (GPU) or `libx265` (CPU)

Per-run ffmpeg details are written to `bake.log`.
