#!/usr/bin/env python3
"""Install the labwc rule that fullscreens the GStreamer GL video window.

The cinematic 4K player uses glimagesink. Under labwc/Wayland that window
appears as app-id "python3", title "OpenGL Renderer". GStreamer cannot
choose a fullscreen output itself here, so labwc must move the window to
the projector first, then fullscreen it.
"""
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


RC = os.path.expanduser("~/.config/labwc/rc.xml")
OUTPUT = os.environ.get("VJ_PROJECTOR_OUTPUT", "HDMI-A-2")
MATCH_ID = "python3"
MATCH_TITLE = "OpenGL Renderer"


def rule_inner():
    return (
        f'    <windowRule identifier="{MATCH_ID}" title="{MATCH_TITLE}" matchOnce="false">\n'
        f'      <action name="MoveToOutput" output="{OUTPUT}" />\n'
        '      <action name="ToggleFullscreen" />\n'
        '    </windowRule>\n'
    )


def main():
    if not os.path.exists(RC):
        print(f"[rule] no rc.xml at {RC}; is labwc running?", flush=True)
        return 1

    with open(RC, encoding="utf-8") as f:
        text = f.read()

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        print(f"[rule] current rc.xml is invalid XML ({exc}); leaving it alone.",
              flush=True)
        return 1

    if MATCH_TITLE in text:
        print("[rule] OpenGL Renderer rule already present.", flush=True)
        return 0

    inner = rule_inner()
    rootname = root.tag.split("}")[-1]
    close_root = f"</{rootname}>"

    if "<windowRules" in text:
        pos = text.rfind("</windowRules>")
        if pos == -1:
            print("[rule] found <windowRules> without a closing tag; aborting.",
                  flush=True)
            return 1
        new_text = text[:pos] + inner + text[pos:]
    else:
        pos = text.rfind(close_root)
        if pos == -1:
            print(f"[rule] could not find {close_root}; aborting.", flush=True)
            return 1
        new_text = text[:pos] + "  <windowRules>\n" + inner + "  </windowRules>\n" + text[pos:]

    try:
        ET.fromstring(new_text)
    except ET.ParseError as exc:
        print(f"[rule] generated XML would be invalid ({exc}); not writing.",
              flush=True)
        return 1

    bak = RC + ".bak." + time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(RC, bak)
    with open(RC, "w", encoding="utf-8") as f:
        f.write(new_text)

    subprocess.run(["pkill", "-HUP", "labwc"], check=False)
    print(f"[rule] installed fullscreen rule for {OUTPUT}. Backup: {bak}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
