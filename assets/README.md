# Base clips

Drop `.mp4` (or `.mov`) files here. They become slots `1-0` on the
keyboard, in **sorted filename order**, so prefix with numbers if you
want a specific arrangement:

```
01-tunnel-warp.mp4      → key 1
02-purple-plasma.mp4    → key 2
03-mantissa-cubes.mp4   → key 3
...
```

Recommended specs:

- Resolution: match the projector (default `854x480`). Anything bigger
  will be downscaled per frame and waste CPU.
- Codec: H.264 (libx264). Avoid HEVC/H.265 — slower software decode.
- No audio track. Strip it with `-an` in ffmpeg.
- Length: 10-60 seconds is the sweet spot. Loops cleanly is the goal.

Pre-process anything from Beeple / Mantissa / Videezy:

```bash
ffmpeg -i input.mp4 -vf scale=854:480 -c:v libx264 -preset slow -crf 22 -an output.mp4
```

## Suggested starter pack

- 2-3 abstract / generative loops (Mantissa, Beeple)
- 1-2 city / cityscape loops (chill vibe)
- 1-2 high-energy strobey loops (peak time)
- 1-2 slow-mo nature / cosmic loops (downtempo)

## Free sources

- https://www.beeple-crap.com/vjloops — the staple
- https://mantissa.xyz/vj.html — CC0 4K, Blender source included
- https://www.videezy.com/free-video/vj-loop — 6k+ free
- https://uppbeat.io/motion-graphics/category/backgrounds/vj-loops
- https://neuromixer.com/pages/visual-loops
