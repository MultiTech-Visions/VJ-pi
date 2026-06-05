"""Print the USB cameras the rig can actually capture from.

Driven by the `List Cameras.sh` launcher so the GUI-only operator can see
what's detected (and which index to force, if auto-probe ever picks wrong)
without touching a terminal. Output is plain text — the launcher pipes it
into a zenity dialog.
"""
from camera import probe_cameras


def main():
    print("Scanning /dev/video0-5 for working cameras…\n")
    found = probe_cameras()
    if not found:
        print("No working camera found.\n")
        print("Checklist:")
        print("  • Is the USB webcam plugged in?")
        print("  • Try a different USB port (use a blue USB-3 port).")
        print("  • Re-plug it, then run this again.")
        return
    print(f"Found {len(found)} working camera(s):\n")
    for idx, size in found:
        print(f"  /dev/video{idx}   (captures {size})")
    print("\nThe app auto-picks the first one — just press \\ while it runs.")
    print("To force a specific one, set CAMERA_DEVICE in the launcher.")


if __name__ == "__main__":
    main()
