# Overlay clips

Drop `.mp4` files here. They become slots `Q-P` on the keyboard, in
**sorted filename order**:

```
01-fire-burst.mp4       → Q
02-sparks.mp4           → W
03-laser-fan.mp4        → E
04-lens-flare.mp4       → R
05-lightning.mp4        → T
06-smoke.mp4            → Y
07-confetti.mp4         → U
08-light-leaks.mp4      → I
09-strobe-bars.mp4      → O
10-glitch-blocks.mp4    → P
```

## Critical: black background, no alpha

Overlays composite using a **screen blend** (`1 - (1-a)(1-b)`), so any
**black** pixel disappears and bright pixels add on top of the base
layer. This is much faster than true alpha on a Pi and looks identical
for additive content (fire, sparks, light).

If you have a clip with a transparent background or a green screen, key
it to black first:

```bash
# Green screen to black
ffmpeg -i greenscreen.mp4 -vf "chromakey=0x00ff00:0.1:0.2,scale=854:480" \
  -c:v libx264 -crf 22 -an out.mp4

# Alpha to black (alpha → multiply)
ffmpeg -i with-alpha.mov -vf "split[a][b];[a]alphaextract[alpha];[b][alpha]alphamerge,format=yuv420p,scale=854:480" \
  -c:v libx264 -crf 22 -an out.mp4
```

## Free sources

- https://www.videezy.com/free-video/spark-overlay — 925+ spark/fire clips
- https://www.vecteezy.com/free-videos/sparks-overlay
- https://www.videezy.com/free-video/fire (search "fire overlay black")
- Pexels / Pixabay: search `"lens flare"`, `"sparks"`, `"fire"`,
  `"smoke"`, `"laser"`, `"glitch"`

## Starter pack suggestions

The high-value overlays for a DJ set:

1. **Fire burst** — for drops
2. **Sparks** — celebratory moments
3. **Laser cone / fan** — sustained intensity
4. **Lens flare sweep** — accents
5. **Lightning strike** — punctuation
6. **Smoke wisps** — ambient texture
7. **Light leaks** — softener, between hard hits
8. **Strobe bars** — peak energy
