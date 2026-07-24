"""macOS-native window tweaks that Qt has no cross-platform API for.

Qt's Cocoa backend gives every window ``FullScreenAuxiliary | MoveToActiveSpace``
collection behavior. ``MoveToActiveSpace`` only fires when the window is
*activated* — and the pony and bubble are shown with WA_ShowWithoutActivating,
so they stay imprisoned on the Space they were born on. The fix is to mark the
NSWindow as ``CanJoinAllSpaces`` so it simply exists on every Space.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger("clipponyai.macos")

# NSWindowCollectionBehavior bits (AppKit NSWindow.h)
_CAN_JOIN_ALL_SPACES = 1 << 0
_MOVE_TO_ACTIVE_SPACE = 1 << 1
_STATIONARY = 1 << 4
_IGNORES_CYCLE = 1 << 6
_FULLSCREEN_AUXILIARY = 1 << 8

_warned = False


def join_all_spaces(widget, *, overlay: bool = False) -> None:
    """Make a top-level widget's window visible on every macOS Space.

    Overlay windows (pony, bubble) additionally stay put during Mission
    Control and are excluded from Cmd-` window cycling. Call from the
    widget's Show / WinIdChange event handling: Qt destroys and recreates
    the NSWindow whenever window flags change, wiping this behavior.

    Safe to call at any point in that lifecycle — while the window is being
    torn down there is nothing to talk to, and this is a no-op.
    """
    if sys.platform != "darwin":
        return
    global _warned
    try:
        import objc
    except ImportError:
        if not _warned:
            _warned = True
            log.warning("pyobjc not available — windows will not follow across Spaces")
        return
    # Never call winId() here: it force-*creates* a native window. WinIdChange
    # fires while Qt is tearing one down, so creating from this handler
    # resurrects a view Qt immediately discards and leaves the cached winId
    # pointing at freed memory. Messaging that pointer is a segfault, not a
    # catchable exception, so the try/except below would not save us.
    # internalWinId()/windowHandle() only ever report what already exists.
    if widget.windowHandle() is None:
        return
    raw = int(widget.internalWinId() or 0)
    if not raw:
        return
    try:
        # internalWinId() on macOS is the NSView*; its window() is the NSWindow.
        view = objc.objc_object(c_void_p=ctypes.c_void_p(raw))
        ns_window = view.window()
        if ns_window is None:
            return
        behavior = ns_window.collectionBehavior()
        behavior &= ~_MOVE_TO_ACTIVE_SPACE  # same group as CanJoinAllSpaces
        behavior |= _CAN_JOIN_ALL_SPACES | _FULLSCREEN_AUXILIARY
        if overlay:
            behavior |= _STATIONARY | _IGNORES_CYCLE
        ns_window.setCollectionBehavior_(behavior)
    except Exception:
        if not _warned:
            _warned = True
            log.warning("failed to set NSWindow collection behavior", exc_info=True)
