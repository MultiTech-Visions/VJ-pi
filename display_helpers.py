"""SDL2-level helpers for things pygame doesn't expose reliably.

Specifically: moving pygame's main display window between physical
monitors while in fullscreen. pygame.display.set_mode(..., display=N) with
the FULLSCREEN|SCALED flags often re-uses the existing SDL window and
pygame's display kwarg gets lost — SDL pins the fullscreen window to its
original monitor.

The reliable sequence is the SDL2 one: SDL_SetWindowFullscreen(0) to drop
fullscreen, SDL_SetWindowPosition to absolute pixel coords of the target
display (looked up via SDL_GetDisplayBounds), SDL_SetWindowFullscreen
again to re-enter fullscreen. That's what move_main_window_to_display
does. It's a no-op (returns False) if libSDL2 isn't loadable or
pygame's _sdl2 module is missing, and the caller can fall back to
pygame.display.set_mode().
"""
import ctypes
import ctypes.util

import pygame


SDL_WINDOW_FULLSCREEN          = 0x00000001
SDL_WINDOW_FULLSCREEN_DESKTOP  = 0x00001001


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
        lib.SDL_SetWindowSize.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.SDL_SetWindowFullscreen.restype = ctypes.c_int
        lib.SDL_SetWindowFullscreen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.SDL_GetWindowFlags.restype = ctypes.c_uint32
        lib.SDL_GetWindowFlags.argtypes = [ctypes.c_void_p]
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


def _resolve_target_xy(display_idx, content_size):
    """Pixel position centring an `content_size` window on `display_idx`.

    Prefers SDL_GetDisplayBounds; falls back to pygame.display.get_desktop_sizes
    with a horizontal-layout assumption (good enough for typical Pi setups).
    """
    bounds = get_display_bounds(display_idx)
    if bounds is None:
        try:
            sizes = pygame.display.get_desktop_sizes()
            x_offset = sum(s[0] for s in sizes[:display_idx])
            bw, bh = sizes[display_idx]
            bx, by = x_offset, 0
        except (pygame.error, IndexError, AttributeError):
            return None
    else:
        bx, by, bw, bh = bounds
    cw, ch = content_size
    return (bx + max(0, (bw - cw) // 2),
            by + max(0, (bh - ch) // 2))


def move_main_window_to_display(display_idx, content_size, fullscreen):
    """Move pygame's main display window to `display_idx`.

    Returns True if the move was issued, False if SDL2 / pygame._sdl2 aren't
    available so the caller can fall back to pygame.display.set_mode.
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

    xy = _resolve_target_xy(display_idx, content_size)
    if xy is None:
        return False
    target_x, target_y = xy

    flags = lib.SDL_GetWindowFlags(window_ptr)
    was_fs_desktop   = bool(flags & SDL_WINDOW_FULLSCREEN_DESKTOP
                            == SDL_WINDOW_FULLSCREEN_DESKTOP)
    was_fs_exclusive = bool(flags & SDL_WINDOW_FULLSCREEN) and not was_fs_desktop

    if was_fs_desktop or was_fs_exclusive:
        lib.SDL_SetWindowFullscreen(window_ptr, 0)
        lib.SDL_PumpEvents()

    # Restore the rendered surface size so the window isn't tiny at the new
    # spot during the brief windowed interlude.
    cw, ch = content_size
    lib.SDL_SetWindowSize(window_ptr, cw, ch)
    lib.SDL_SetWindowPosition(window_ptr, target_x, target_y)
    lib.SDL_PumpEvents()

    if fullscreen:
        target_flag = SDL_WINDOW_FULLSCREEN_DESKTOP if not was_fs_exclusive \
                      else SDL_WINDOW_FULLSCREEN
        lib.SDL_SetWindowFullscreen(window_ptr, target_flag)
        lib.SDL_PumpEvents()

    return True
