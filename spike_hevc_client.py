"""SPIKE: measure the real out-of-process HEVC decode path end to end.

Spawns hevc_decode_worker.py as a SEPARATE process (HW HEVC decode + GPU
detile/convert + readback), pulls BGR frames across the pipe into this
process exactly as the main app would, optionally runs an FX load on each
frame, and reports sustained FPS + all-core CPU%.

This is the bench's winning gl path PLUS the new unknowns the bench didn't
cover: the worker->main IPC hop (shipping ~7 MB/frame at 2K) and headroom
for FX on top. Decision metric: stays comfortably above 30 fps (and never
near the 13 fps floor) at native 2K, which is MORE pixels than 1080p.

    ./venv/bin/python spike_hevc_client.py CLIP [--fx N] [--frames N] [--warmup N]
      --fx N : apply N Gaussian blurs per frame as a stand-in FX load (0 = pure
               decode+IPC, the architecture's own overhead).
"""
import argparse
import subprocess
import sys
import time

import numpy as np

from bench_decode import CpuMeter, FLOOR_FPS

WORKER = "hevc_decode_worker.py"
SYS_PY = "/usr/bin/python3"


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("clip")
    p.add_argument("--fx", type=int, default=0)
    p.add_argument("--frames", type=int, default=300)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--show", action="store_true", help="display in a window")
    p.add_argument("--win", default="1280x720", help="window WxH for --show")
    p.add_argument("--seconds", type=float, default=0,
                   help="auto-close --show after N seconds (0 = until closed)")
    a = p.parse_args(argv)

    # In --show, ask the worker for RGB so the window can blit with a single
    # copy and no numpy channel-swap (BGR otherwise, to match the cv2 app).
    worker_cmd = [SYS_PY, WORKER, a.clip] + (["RGB"] if a.show else [])
    proc = subprocess.Popen(
        worker_cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    cv2 = None
    if a.fx > 0:
        import cv2 as _cv2
        cv2 = _cv2

    def get_frame():
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        header = proc.stdout.readline()
        if not header:
            return None
        import json
        msg = json.loads(header)
        if not msg.get("ok"):
            return None
        n = int(msg["n"])
        payload = proc.stdout.read(n)
        if len(payload) != n:
            return None
        fr = np.frombuffer(payload, np.uint8).reshape(
            int(msg["height"]), int(msg["width"]), 3).copy()
        for _ in range(a.fx):                 # stand-in FX load
            fr = cv2.GaussianBlur(fr, (7, 7), 0)
        return fr

    if a.show:
        import pygame
        ww, wh = (int(x) for x in a.win.lower().split("x"))
        pygame.init()
        screen = pygame.display.set_mode((ww, wh))
        pygame.display.set_caption("HEVC hardware-decode test")
        font = pygame.font.SysFont("monospace", 22, bold=True)
        times, t_start, n = [], time.perf_counter(), 0
        running = True
        while running:
            for e in pygame.event.get():
                if e.type == pygame.QUIT or (
                        e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q)):
                    running = False
            t = time.perf_counter()
            fr = get_frame()
            if fr is None:
                break
            surf = pygame.image.frombuffer(fr.tobytes(),
                                           (fr.shape[1], fr.shape[0]), "RGB")
            screen.blit(pygame.transform.scale(surf, (ww, wh)), (0, 0))
            times = (times + [time.perf_counter() - t])[-30:]
            fps = len(times) / sum(times) if sum(times) else 0.0
            screen.blit(font.render(
                f"HW HEVC + IPC + fx={a.fx}   {fps:4.1f} fps   "
                f"{fr.shape[1]}x{fr.shape[0]}", True, (0, 255, 0)), (12, 10))
            pygame.display.flip()
            n += 1
            if a.seconds and (time.perf_counter() - t_start) >= a.seconds:
                running = False
        dt = time.perf_counter() - t_start
        pygame.quit()
        proc.kill()
        print(f"RESULT spike-hevc SHOW fx={a.fx} avg_fps={n / dt:5.1f} "
              f"frames={n} secs={dt:.1f}", flush=True)
        return 0

    try:
        dims = ""
        for _ in range(a.warmup):
            fr = get_frame()
            if fr is None:
                err = proc.stderr.read().decode(errors="replace")[:300]
                print(f"RESULT spike-hevc fx={a.fx}  FAIL :: worker gave no frame :: {err}",
                      flush=True)
                return 1
            dims = f"{fr.shape[1]}x{fr.shape[0]}"
        meter = CpuMeter()
        t0 = time.perf_counter()
        got = 0
        for _ in range(a.frames):
            if get_frame() is None:
                break
            got += 1
        elapsed = time.perf_counter() - t0
        cpu = meter.percent()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.kill()

    fps = got / elapsed if elapsed else 0.0
    verdict = "PASS" if fps >= FLOOR_FPS else "FAIL"
    print(f"RESULT spike-hevc fx={a.fx:<2} fps={fps:6.1f} [{verdict}]  "
          f"cpu%={cpu:5.1f}  ms/frame={1000*elapsed/got:6.2f}  "
          f"frames={got} {dims}  (worker-process decode + IPC)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
