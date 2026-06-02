#!/usr/bin/env python3
"""Add a labwc window rule that fullscreens the GPU compositor's video
window onto the projector.

Target window (from `wlrctl toplevel list`): app-id "python3",
title "OpenGL Renderer". Projector output: HDMI-A-2.

Order matters: labwc can't move a window that's already fullscreen, so the
rule moves it to HDMI-A-2 first, THEN toggles fullscreen.

Safety: backs up rc.xml, validates XML before and after, won't write
anything that isn't well-formed, and is idempotent (safe to run twice).
Edits only the user's own ~/.config/labwc/rc.xml — no sudo.
"""
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

RC = os.path.expanduser("~/.config/labwc/rc.xml")
OUTPUT = "HDMI-A-2"
MATCH_ID = "python3"
MATCH_TITLE = "OpenGL Renderer"

RULE_INNER = (
    '    <windowRule identifier="{id}" title="{title}" matchOnce="false">\n'
    '      <action name="MoveToOutput" output="{out}" />\n'
    '      <action name="ToggleFullscreen" />\n'
    '    </windowRule>\n'
).format(id=MATCH_ID, title=MATCH_TITLE, out=OUTPUT)

RULE_BLOCK = "  <windowRules>\n" + RULE_INNER + "  </windowRules>\n"


def main():
    if not os.path.exists(RC):
        print(f"[rule] no rc.xml at {RC} — is this really labwc?", flush=True)
        return 1

    with open(RC, encoding="utf-8") as f:
        text = f.read()

    # Validate the current file first; never touch a file we can't parse.
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        print(f"[rule] current rc.xml is not valid XML ({exc}); leaving it "
              f"alone.", flush=True)
        return 1

    if MATCH_TITLE in text:
        print("[rule] a rule for the video window is already present — "
              "nothing to do.", flush=True)
        return 0

    rootname = root.tag.split("}")[-1]      # strip any namespace
    close_root = f"</{rootname}>"

    if "<windowRules" in text:
        anchor = "</windowRules>"
        pos = text.rfind(anchor)
        if pos == -1:
            print("[rule] found <windowRules> but no closing tag; aborting.",
                  flush=True)
            return 1
        new_text = text[:pos] + RULE_INNER + text[pos:]
    else:
        pos = text.rfind(close_root)
        if pos == -1:
            print(f"[rule] couldn't find {close_root}; aborting.", flush=True)
            return 1
        new_text = text[:pos] + RULE_BLOCK + text[pos:]

    # Validate the RESULT before writing anything.
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
    print(f"[rule] rc.xml updated (backup: {bak})", flush=True)

    # Reload labwc config (SIGHUP). Only affects NEW windows, which is what
    # we want — next compositor launch lands fullscreen on the projector.
    subprocess.run(["pkill", "-HUP", "labwc"], check=False)
    print(f"[rule] labwc reloaded. Re-run the GPU compositor — its window "
          f"should jump to {OUTPUT} and go fullscreen.", flush=True)
    print(f"[rule] to undo: restore {bak} over {RC}, then 'pkill -HUP labwc'.",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
