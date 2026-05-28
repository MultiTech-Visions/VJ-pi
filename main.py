"""pi-paint VJ — GStreamer rewrite, entry point.

Phase 1 scaffold: a thin shim around app.VJApp.run(). The reason
main.py exists at all is so launchers can keep doing
`python3 main.py` like before — the heavy lifting moves under app.py.
"""
import sys

from app import VJApp


if __name__ == "__main__":
    sys.exit(VJApp().run(sys.argv))
