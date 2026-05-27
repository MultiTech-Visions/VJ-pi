"""pi-paint VJ — GStreamer rewrite, entry point.

Argparse layer + handoff to VJApp. The GTK application owns its
own command-line handling (HANDLES_COMMAND_LINE), but we strip our
own flags here so the launchers can stay declarative — Start VJ.sh
calls `python3 main.py` for clip mode, Test Tunnel.sh calls
`python3 main.py --source tunnel`, etc. GTK gets a clean argv that
doesn't confuse it.
"""
import argparse
import sys

from app import VJApp


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="pi-paint VJ — GStreamer rewrite",
    )
    p.add_argument(
        "--source",
        choices=("clip", "plasma", "tunnel"),
        default="clip",
        help="What to render. `clip` plays the first .mp4/.mov in "
             "assets/clips/ (default). `plasma` / `tunnel` run a "
             "GLSL fragment shader generator on the GPU instead.",
    )
    p.add_argument(
        "--single-screen",
        action="store_true",
        help="Put both output and HUD on display 0 (no projector)",
    )
    return p.parse_args(argv[1:])


if __name__ == "__main__":
    args = parse_args(sys.argv)
    app = VJApp(source_kind=args.source, single_screen=args.single_screen)
    # Pass only argv[0] to GTK so its own arg parser doesn't trip on
    # our flags. Our config is already on the VJApp instance.
    sys.exit(app.run([sys.argv[0]]))
