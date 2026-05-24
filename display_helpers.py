"""SDL2-level helpers for things pygame doesn't expose reliably.

Moves pygame's main display window between physical monitors while in
fullscreen. The naive sequence (SDL_SetWindowFullscreen(0) → move →
SDL_SetWindowFullscreen(DESKTOP)) races X11: the position change is
async (it needs a ConfigureNotify round-trip with the WM), so SDL's
GetWindowDisplayIndex still reports the old display when we re-enter
fullscreen — and we snap back to where we started.

The reliable version pumps SDL events in a poll loop until
SDL_GetWindowDisplayIndex actually reports the target display, then
re-enters fullscreen. If after a generous timeout SDL still hasn't
caught up, we fall back to "fake fullscreen": a borderless window
sized to cover the target display, no SDL_WINDOW_FULLSCREEN flag,
which can't be snapped back by SDL.
"""
import ctypes
import ctypes.util
import time

import pygame


SDL_WINDOW_FULLSCREEN          = 0x00000001
SDL_WINDOW_FULLSCREEN_DESKTOP  = 0x00001001
SDL_WINDOW_BORDERLESS          = 0x00000010


class _SDL_Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


_sdl = None
_sdl_load_attempted = False


def _load_sdl():
    """Lazily load libSDL2 and bind the function signatures we need."""
    global _sdl, _sdl_load_attempted
    if _sdl_load_attempted:
        return _sdl
    _sdl_load_attempted = True

    candidates = []
    found = ctypes.util.find_library("SDL2")
    if found:
        candidates.append(found)
    candidates += ["libSDL2-2.0.so.0", "libSDL2.so", "SDL2.dll", "libSDL2.dylib"]

    lib = None
    for name in candidates:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        return None

    try:
        lib.SDL_GetDisplayBounds.restype = ctypes.c_int
        lib.SDL_GetDisplayBounds.argtypes = [ctypes.c_int, ctypes.POINTER(_SDL_Rect)]
        lib.SDL_GetWindowFromID.restype = ctypes.c_void_p
        lib.SDL_GetWindowFromID.argtypes = [ctypes.c_uint32]
        lib.SDL_SetWindowPosition.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.SDL_GetWindowPosition.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        ]
        lib.SDL_SetWindowSize.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.SDL_SetWindowFullscreen.restype = ctypes.c_int
        lib.SDL_SetWindowFullscreen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.SDL_SetWindowBordered.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.SDL_SetWindowResizable.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.SDL_RaiseWindow.argtypes = [ctypes.c_void_p]
        lib.SDL_GetWindowFlags.restype = ctypes.c_uint32
        lib.SDL_GetWindowFlags.argtypes = [ctypes.c_void_p]
        lib.SDL_GetWindowDisplayIndex.restype = ctypes.c_int
        lib.SDL_GetWindowDisplayIndex.argtypes = [ctypes.c_void_p]
        lib.SDL_PumpEvents.argtypes = []
    except AttributeError:
        return None

    _sdl = lib
    return _sdl


def get_display_bounds(idx):
    """Return absolute (x, y, w, h) for display `idx`, or None."""
    lib = _load_sdl()
    if lib is None:
        return None
    rect = _SDL_Rect()
    if lib.SDL_GetDisplayBounds(idx, ctypes.byref(rect)) != 0:
        return None
    return rect.x, rect.y, rect.w, rect.h


def _resolve_target_xy(display_idx, content_size, bounds=None):
    if bounds is None:
        bounds = get_display_bounds(display_idx)
    if bounds is None:
        try:
            sizes = pygame.display.get_desktop_sizes()
            x_offset = sum(s[0] for s in sizes[:display_idx])
            bw, bh = sizes[display_idx]
            bounds = (x_offset, 0, bw, bh)
        except (pygame.error, IndexError, AttributeError):
            return None
    bx, by, bw, bh = bounds
    cw, ch = content_size
    return (bx + max(0, (bw - cw) // 2),
            by + max(0, (bh - ch) // 2))


def _wait_until_on_display(lib, window_ptr, target_idx, timeout_s=1.2):
    """Pump SDL events until SDL_GetWindowDisplayIndex reports `target_idx`.

    X11 SetWindowPosition is async; SDL only updates window->x/y when it
    receives a ConfigureNotify, which is what SDL_PumpEvents triggers.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        lib.SDL_PumpEvents()
        if lib.SDL_GetWindowDisplayIndex(window_ptr) == target_idx:
            return True
        time.sleep(0.02)
    return False


def move_main_window_to_display(display_idx, content_size, fullscreen):
    """Move pygame's main display window to `display_idx`.

    Returns True if the move was issued (caller should refresh its
    pygame.display.get_surface() reference). Returns False if SDL2 or
    pygame._sdl2 aren't usable so the caller can fall back to
    pygame.display.set_mode().
    """
    lib = _load_sdl()
    if lib is None:
        return False
    try:
        from pygame._sdl2.video import Window
        win = Window.from_display_module()
    except (ImportError, AttributeError):
        return False

    window_ptr = lib.SDL_GetWindowFromID(win.id)
    if not window_ptr:
        return False

    bounds = get_display_bounds(display_idx)
    xy = _resolve_target_xy(display_idx, content_size, bounds)
    if xy is None:
        return False
    target_x, target_y = xy
    cw, ch = content_size

    flags_before = lib.SDL_GetWindowFlags(window_ptr)
    was_fs_desktop = ((flags_before & SDL_WINDOW_FULLSCREEN_DESKTOP)
                      == SDL_WINDOW_FULLSCREEN_DESKTOP)
    was_fs_exclusive = bool(flags_before & SDL_WINDOW_FULLSCREEN) and not was_fs_desktop
    initial_display = lib.SDL_GetWindowDisplayIndex(window_ptr)
    print(f"[vj] sdl: window currently on display {initial_display}, "
          f"fs_desktop={was_fs_desktop} fs_excl={was_fs_exclusive}")

    # Step 1: drop fullscreen so the WM accepts a position change.
    if was_fs_desktop or was_fs_exclusive:
        lib.SDL_SetWindowFullscreen(window_ptr, 0)
        # Settle: a few pumps so SDL sees the un-fullscreen event.
        for _ in range(10):
            lib.SDL_PumpEvents()
            time.sleep(0.02)

    # Step 2: reposition. Resize first so a half-second windowed flash
    # at the new spot doesn't fill the whole monitor.
    lib.SDL_SetWindowSize(window_ptr, cw, ch)
    lib.SDL_SetWindowPosition(window_ptr, target_x, target_y)

    # Step 3: wait for SDL to actually register the move. This is the
    # critical fix — without it, SDL_SetWindowFullscreen below uses the
    # stale position and snaps the window back to the original display.
    moved_ok = _wait_until_on_display(lib, window_ptr, display_idx, timeout_s=1.2)
    final_display_pre_fs = lib.SDL_GetWindowDisplayIndex(window_ptr)
    print(f"[vj] sdl: after move → display {final_display_pre_fs} (wanted {display_idx}, ok={moved_ok})")

    if not moved_ok and fullscreen and bounds is not None:
        # Fallback: "fake fullscreen" — make the window borderless and
        # resize it to fully cover the target display. Doesn't use
        # SDL_WINDOW_FULLSCREEN at all, so SDL has nothing to snap.
        bx, by, bw, bh = bounds
        print(f"[vj] sdl: WM ignored the move, falling back to fake-fullscreen at "
              f"({bx},{by}) {bw}x{bh}")
        lib.SDL_SetWindowBordered(window_ptr, 0)
        lib.SDL_SetWindowResizable(window_ptr, 0)
        lib.SDL_SetWindowSize(window_ptr, bw, bh)
        lib.SDL_SetWindowPosition(window_ptr, bx, by)
        lib.SDL_RaiseWindow(window_ptr)
        for _ in range(5):
            lib.SDL_PumpEvents()
            time.sleep(0.02)
        return True

    # Step 4: re-enter fullscreen on the (now correct) display.
    if fullscreen:
        target_flag = (SDL_WINDOW_FULLSCREEN_DESKTOP if not was_fs_exclusive
                       else SDL_WINDOW_FULLSCREEN)
        lib.SDL_SetWindowFullscreen(window_ptr, target_flag)
        for _ in range(10):
            lib.SDL_PumpEvents()
            time.sleep(0.02)
        final = lib.SDL_GetWindowDisplayIndex(window_ptr)
        print(f"[vj] sdl: after re-fullscreen → display {final}")

    return True
