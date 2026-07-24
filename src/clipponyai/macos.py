"""macOS-native window tweaks that Qt has no cross-platform API for.

Two Cocoa-backend behaviors need correcting for a desktop pet:

*Spaces.* Qt gives every window ``FullScreenAuxiliary | MoveToActiveSpace``
collection behavior. ``MoveToActiveSpace`` only fires when the window is
*activated* — and the pony and bubble are shown with WA_ShowWithoutActivating,
so they stay imprisoned on the Space they were born on. The fix is to mark the
NSWindow as ``CanJoinAllSpaces`` so it simply exists on every Space.

*Focus.* ``QWidget.raise_()`` ends in ``[NSApp activateIgnoringOtherApps:YES]``
on this backend, so merely reordering the pony or her speech bubble yanks the
keyboard away from whatever the user is typing in. AppKit's ``orderFront:``
does the z-order half of that job on its own, without activating.
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

_warned_import = False
_warned_behavior = False
_warned_order = False


def _ns_window(widget):
    """The AppKit NSWindow behind a Qt widget, or None if it has none (yet).

    Never call winId() here: it force-*creates* a native window. WinIdChange
    fires while Qt is tearing one down, so creating from that handler
    resurrects a view Qt immediately discards and leaves the cached winId
    pointing at freed memory. Messaging that pointer is a segfault, not a
    catchable exception, so callers' try/except would not save us.
    internalWinId()/windowHandle() only ever report what already exists.
    """
    global _warned_import
    # Only Qt's cocoa backend hands out an NSView* as the window id. Under
    # offscreen/minimal (headless tests, CI) it is an unrelated pointer, and
    # messaging that as an object is another uncatchable segfault.
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() != "cocoa":
        return None
    try:
        import objc
    except ImportError:
        if not _warned_import:
            _warned_import = True
            log.warning("pyobjc not available — windows will not follow across "
                        "Spaces and cannot be reordered without stealing focus")
        return None
    if widget.windowHandle() is None:
        return None
    raw = int(widget.internalWinId() or 0)
    if not raw:
        return None
    # internalWinId() on macOS is the NSView*; its window() is the NSWindow.
    view = objc.objc_object(c_void_p=ctypes.c_void_p(raw))
    return view.window()


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
    global _warned_behavior
    try:
        ns_window = _ns_window(widget)
        if ns_window is None:
            return
        behavior = ns_window.collectionBehavior()
        behavior &= ~_MOVE_TO_ACTIVE_SPACE  # same group as CanJoinAllSpaces
        behavior |= _CAN_JOIN_ALL_SPACES | _FULLSCREEN_AUXILIARY
        if overlay:
            behavior |= _STATIONARY | _IGNORES_CYCLE
        ns_window.setCollectionBehavior_(behavior)
    except Exception:
        if not _warned_behavior:
            _warned_behavior = True
            log.warning("failed to set NSWindow collection behavior", exc_info=True)


def raise_without_activating(widget) -> None:
    """Bring a window to the front of its window level, keeping focus put.

    Use this for anything the pony does on her own initiative (speaking,
    starting a cursor chase, coming back from hidden). Qt's ``raise_()``
    activates the whole application on macOS, which steals the keyboard
    mid-keystroke; ``orderFront:`` reorders and nothing else.

    If pyobjc is missing we deliberately skip the reorder rather than fall
    back to ``raise_()``: these windows already sit above normal ones via
    WindowStaysOnTopHint, so the cost is peer ordering, never visibility —
    a far better trade than interrupting the user.
    """
    if sys.platform != "darwin":
        widget.raise_()
        return
    global _warned_order
    try:
        ns_window = _ns_window(widget)
        if ns_window is None:
            return
        ns_window.orderFront_(None)
    except Exception:
        if not _warned_order:
            _warned_order = True
            log.warning("failed to order NSWindow front without activating",
                        exc_info=True)
