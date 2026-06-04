"""Drive the REAL engine pipeline with the hardware-HEVC clip pool selected,
no keyboard needed. Measures compose_frame() (decode + FX + composite) and
render()+flip (adds the display blit) at the 2K canvas."""
import time
import pygame

from config import Config
from engine import Engine

cfg = Config(hevc=True, width=2048, height=1152, gpu_scale=True)
pygame.init()
eng = Engine(cfg, None)                 # screen created by init_gpu_output
gpu_ok = eng.init_gpu_output()
print(f"[test] gpu_scale active: {gpu_ok}")
if not gpu_ok:                          # fall back to a CPU window
    eng.screen = pygame.display.set_mode((1280, 720))
print(f"[test] clips found: {len(eng.clips)}")
eng.clips.first()                      # select clip 0 -> spawns HEVC worker
print(f"[test] active clip: {eng.clips.name(eng.clips.active_idx)}")


def bench(label, fn, n=150, warmup=20):
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = time.perf_counter() - t0
    print(f"[test] {label}: {n/dt:5.1f} fps  ({1000*dt/n:.1f} ms/frame)", flush=True)


frame = eng.compose_frame()
print(f"[test] canvas {frame.shape[1]}x{frame.shape[0]}")
bench("compose_frame (decode+FX+composite)", eng.compose_frame)
bench("render (compose + GPU-scaled output)", eng.render)
eng.clips.release_all()
pygame.quit()
