#!/usr/bin/env python3
"""Bake the VJ clip library to Pi-5-decodable HEVC, on a beefy PC.

Why this exists: the Pi 5 has a hardware HEVC decoder but no HW encoder, and
software HEVC encoding is slow. Transcode on the PC (an RTX 4090 has a great
HEVC NVENC encoder), then drop the results onto the Pi where they decode in
hardware. Output settings mirror what the Pi already decodes successfully
(yuv420p / main profile / hvc1 tag / faststart / fit-within-1080p / 30fps).

Usage (normally via "Bake Clips.bat"):
    python bake_clips.py [--input DIR] [--output DIR] [--height 1080]
                         [--fps 30] [--cq 23] [--encoder hevc_nvenc]

Drop source clips (any format) in input/, run, collect HEVC mp4s from output/.
Already-baked files are skipped. Shows an overall bar (clip k of N) and a
per-clip bar driven by ffmpeg's own progress output. No third-party deps.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi", ".wmv", ".flv")


def have(tool):
    return shutil.which(tool) is not None


def ffprobe_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return float(out)
    except Exception:
        return 0.0


def bar(pct, width=34):
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct*100:5.1f}%"


def fmt_t(s):
    s = int(s)
    return f"{s // 60:02d}:{s % 60:02d}"


def encoder_available(name):
    try:
        out = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"],
                                      stderr=subprocess.STDOUT).decode()
        return name in out
    except Exception:
        return False


def build_cmd(src, dst_tmp, a):
    # Scale to fit, then letterbox to EXACTLY width x height — a uniform
    # gl-friendly geometry for every clip (and no runtime resize in the app).
    vf = (f"scale={a.width}:{a.height}:force_original_aspect_ratio=decrease:"
          f"force_divisible_by=2,"
          f"pad={a.width}:{a.height}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={a.fps},format=yuv420p")
    common = ["ffmpeg", "-hide_banner", "-y", "-nostdin", "-i", src,
              "-map", "0:v:0", "-an", "-vf", vf]
    if a.encoder == "hevc_nvenc":
        enc = ["-c:v", "hevc_nvenc", "-preset", a.preset, "-rc", "vbr",
               "-cq", str(a.cq), "-b:v", "0", "-profile:v", "main"]
    else:  # software fallback
        enc = ["-c:v", "libx265", "-preset", "medium", "-crf", str(a.cq),
               "-profile:v", "main"]
    tail = ["-tag:v", "hvc1", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", dst_tmp]
    return common + enc + tail


def transcode(src, dst, a, logf):
    dur = ffprobe_duration(src) or 0.0
    tmp = dst + ".tmp.mp4"
    if os.path.exists(tmp):
        os.remove(tmp)
    cmd = build_cmd(src, tmp, a)
    logf.write("\n\n=== " + os.path.basename(src) + " ===\n" + " ".join(cmd) + "\n")
    logf.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf,
                            universal_newlines=True)
    last = 0.0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and dur > 0:
            try:
                secs = int(line.split("=", 1)[1]) / 1_000_000.0
            except ValueError:
                continue
            now = time.time()
            if now - last > 0.1:
                sys.stdout.write("\r    " + bar(secs / dur) +
                                 f"  {fmt_t(secs)}/{fmt_t(dur)}   ")
                sys.stdout.flush()
                last = now
        elif line == "progress=end":
            sys.stdout.write("\r    " + bar(1.0) + f"  {fmt_t(dur)}/{fmt_t(dur)}   \n")
            sys.stdout.flush()
    proc.wait()
    if proc.returncode == 0 and os.path.exists(tmp):
        os.replace(tmp, dst)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=os.path.join(HERE, "input"))
    p.add_argument("--output", default=os.path.join(HERE, "output"))
    # 2048x1152 is THE sweet spot on the Pi 5: the only geometry where the
    # fast gl decode path negotiates the HW decoder's tiled format (1080p and
    # 4K both fail or crawl), and it's higher quality than 1080p. Every clip is
    # scaled+letterboxed to exactly this so decode is fast AND the engine never
    # resizes at runtime. 4K-with-FX isn't viable; 4K stays cinematic-only.
    p.add_argument("--width", type=int, default=2048)
    p.add_argument("--height", type=int, default=1152)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--cq", type=int, default=23, help="quality, lower=better")
    p.add_argument("--preset", default="p5", help="nvenc preset p1(fast)..p7(slow)")
    p.add_argument("--encoder", default="hevc_nvenc",
                   help="hevc_nvenc (GPU) or libx265 (CPU fallback)")
    a = p.parse_args(argv)

    if not have("ffmpeg") or not have("ffprobe"):
        print("ERROR: ffmpeg/ffprobe not found in PATH.\n"
              "Install from https://www.gyan.dev/ffmpeg/builds/ (full build) and\n"
              "add its bin folder to PATH, then re-run.")
        return 2
    if a.encoder == "hevc_nvenc" and not encoder_available("hevc_nvenc"):
        print("WARNING: hevc_nvenc not available in this ffmpeg build — falling\n"
              "back to CPU (libx265). For GPU speed, install an ffmpeg build with\n"
              "NVENC support. Continuing on CPU...\n")
        a.encoder = "libx265"

    os.makedirs(a.input, exist_ok=True)
    os.makedirs(a.output, exist_ok=True)
    srcs = sorted(
        os.path.join(a.input, f) for f in os.listdir(a.input)
        if f.lower().endswith(VIDEO_EXTS) and not f.startswith((".", "_"))
    )
    if not srcs:
        print(f"No source clips found in {a.input}\n"
              f"Drop video files there and run again.")
        return 1

    total = len(srcs)
    print(f"Baking {total} clip(s)  ->  HEVC {a.width}x{a.height} @ {a.fps}fps  "
          f"(encoder={a.encoder}, cq={a.cq})")
    print(f"  input : {a.input}\n  output: {a.output}\n")

    log_path = os.path.join(HERE, "bake.log")
    done = skipped = failed = 0
    t_start = time.time()
    with open(log_path, "w", encoding="utf-8") as logf:
        for i, src in enumerate(srcs, 1):
            stem = os.path.splitext(os.path.basename(src))[0]
            dst = os.path.join(a.output, stem + ".mp4")
            head = f"[{i}/{total}] {bar(i / total, 20)}  {os.path.basename(src)}"
            print(head)
            if os.path.exists(dst):
                print("    already baked — skipped\n")
                skipped += 1
                continue
            ok = transcode(src, dst, a, logf)
            if ok:
                done += 1
                print()
            else:
                failed += 1
                print("    FAILED (see bake.log)\n")

    dt = time.time() - t_start
    print("=" * 56)
    print(f"Done in {fmt_t(dt)}.  baked={done} skipped={skipped} failed={failed}")
    print(f"HEVC clips are in: {a.output}")
    if failed:
        print(f"Some failed — details in {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
