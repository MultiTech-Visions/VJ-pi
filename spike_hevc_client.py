"""SPIKE client for the pipelined / shared-memory HEVC decode worker.

Spawns hevc_decode_worker.py (separate process: HW HEVC decode + ISP detile),
shares frames through /dev/shm (no per-frame pipe copy), and PREFETCHES the next
frame so the worker decodes while this process runs FX / display — decode and
consume overlap instead of running serially.

    ./venv/bin/python spike_hevc_client.py CLIP [--fx N] [--frames N]
                                           [--show [--win WxH] [--seconds S]]
"""
import argparse
import mmap
import os
import subprocess
import sys
import time

import numpy as np

from bench_decode import CpuMeter, FLOOR_FPS

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "hevc_decode_worker.py")
SYS_PY = "/usr/bin/python3"
FMT = "RGB"
BPP = 3
SLOTS = 2


def probe_dims(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        stderr=subprocess.DEVNULL).decode().strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("clip")
    p.add_argument("--fx", type=int, default=0)
    p.add_argument("--frames", type=int, default=300)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--show", action="store_true")
    p.add_argument("--win", default="1280x720")
    p.add_argument("--seconds", type=float, default=0)
    a = p.parse_args(argv)

    w, h = probe_dims(a.clip)
    fb = w * h * BPP
    shm_path = f"/dev/shm/vj_hevc_{os.getpid()}"
    fd = os.open(shm_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.ftruncate(fd, SLOTS * fb)
    mm = mmap.mmap(fd, SLOTS * fb)
    views = [np.frombuffer(mm, np.uint8, count=fb, offset=s * fb).reshape(h, w, 3)
             for s in range(SLOTS)]

    proc = subprocess.Popen(
        [SYS_PY, WORKER, a.clip, shm_path, str(w), str(h), str(SLOTS), FMT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    cv2 = None
    if a.fx > 0:
        import cv2 as _cv2
        cv2 = _cv2

    def req(slot):
        proc.stdin.write(f"{slot}\n".encode())
        proc.stdin.flush()

    def wait():
        line = proc.stdout.readline()
        return bool(line) and line.strip() == b"1"

    def fail(msg):
        err = proc.stderr.read().decode(errors="replace")[:300]
        print(f"RESULT spike-hevc FAIL :: {msg} :: {err}", flush=True)

    try:
        req(0)
        if not wait():
            fail("worker produced no first frame")
            return 1
        cur = 0

        def step():
            """Prefetch the other slot, return the ready current view."""
            nonlocal cur
            nxt = 1 - cur
            req(nxt)                 # worker decodes nxt ...
            view = views[cur]        # ... while we hand back cur
            return view, nxt

        def finish(nxt):
            nonlocal cur
            ok = wait()
            cur = nxt
            return ok

        if a.show:
            import pygame
            ww, wh = (int(x) for x in a.win.lower().split("x"))
            pygame.init()
            screen = pygame.display.set_mode((ww, wh))
            pygame.display.set_caption("HEVC HW decode (pipelined)")
            font = pygame.font.SysFont("monospace", 22, bold=True)
            times, t0, n = [], time.perf_counter(), 0
            run = True
            while run:
                for e in pygame.event.get():
                    if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN
                            and e.key in (pygame.K_ESCAPE, pygame.K_q)):
                        run = False
                t = time.perf_counter()
                view, nxt = step()
                for _ in range(a.fx):
                    view = cv2.GaussianBlur(view, (7, 7), 0)
                surf = pygame.image.frombuffer(view.tobytes(), (w, h), "RGB")
                screen.blit(pygame.transform.scale(surf, (ww, wh)), (0, 0))
                if not finish(nxt):
                    break
                times = (times + [time.perf_counter() - t])[-30:]
                fps = len(times) / sum(times) if sum(times) else 0
                screen.blit(font.render(
                    f"HW HEVC pipelined fx={a.fx}  {fps:4.1f} fps  {w}x{h}",
                    True, (0, 255, 0)), (12, 10))
                pygame.display.flip()
                n += 1
                if a.seconds and time.perf_counter() - t0 >= a.seconds:
                    run = False
            dt = time.perf_counter() - t0
            pygame.quit()
            print(f"RESULT spike-hevc SHOW fx={a.fx} avg_fps={n/dt:5.1f} "
                  f"frames={n} {w}x{h}", flush=True)
            return 0

        for _ in range(a.warmup):
            view, nxt = step()
            for _ in range(a.fx):
                view = cv2.GaussianBlur(view, (7, 7), 0)
            if not finish(nxt):
                fail("worker stopped during warmup")
                return 1
        meter = CpuMeter()
        t0 = time.perf_counter()
        got = 0
        for _ in range(a.frames):
            view, nxt = step()
            for _ in range(a.fx):
                view = cv2.GaussianBlur(view, (7, 7), 0)
            if not finish(nxt):
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
        # Drop every numpy view into the mmap before closing it, or mmap
        # refuses ("cannot close exported pointers exist").
        views = view = None
        import gc
        gc.collect()
        try:
            mm.close()
        except BufferError:
            pass
        os.close(fd)
        try:
            os.unlink(shm_path)
        except OSError:
            pass

    fps = got / elapsed if elapsed else 0
    verdict = "PASS" if fps >= FLOOR_FPS else "FAIL"
    print(f"RESULT spike-hevc fx={a.fx:<2} fps={fps:6.1f} [{verdict}]  "
          f"cpu%={cpu:5.1f}  ms/frame={1000*elapsed/got if got else 0:6.2f}  "
          f"frames={got} {w}x{h}  (pipelined shm decode)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
