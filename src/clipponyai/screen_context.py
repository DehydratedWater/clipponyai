"""Read lightweight foreground context from macOS without taking a screenshot.

NSWorkspace supplies the frontmost application without requiring permission.
Quartz supplies the active window title (which needs Screen Recording access)
and seconds since the last input event (which needs no permission). Missing
permissions or optional bindings degrade to empty values instead of interrupting
the desktop pet.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Pattern

log = logging.getLogger("clipponyai.screen_context")


@dataclass(frozen=True)
class ForegroundContext:
    app: str
    bundle_id: str
    window_title: str
    idle_seconds: float


def foreground_context(*, capture_window_titles: bool = True) -> ForegroundContext | None:
    """Return the current macOS foreground context, or ``None`` off macOS.

    This function is deliberately best-effort and never raises. A missing
    Quartz installation still returns the application name supplied by Cocoa.
    """
    if sys.platform != "darwin":
        return None

    try:
        from AppKit import NSWorkspace

        application = NSWorkspace.sharedWorkspace().frontmostApplication()
        if application is None:
            return None
        app = application.localizedName() or ""
        bundle_id = application.bundleIdentifier() or ""
        pid = int(application.processIdentifier())
    except Exception:
        log.debug("could not read the foreground application", exc_info=True)
        return None

    try:
        import Quartz
    except ImportError:
        return ForegroundContext(
            app=app,
            bundle_id=bundle_id,
            window_title="",
            idle_seconds=0.0,
        )
    except Exception:
        log.debug("could not load Quartz foreground APIs", exc_info=True)
        return None

    window_title = ""
    idle_seconds = 0.0
    if capture_window_titles:
        try:
            options = (
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            )
            windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or ()
            for window in windows:
                if (
                    window.get(Quartz.kCGWindowOwnerPID) == pid
                    and window.get(Quartz.kCGWindowLayer) == 0
                ):
                    candidate = window.get(Quartz.kCGWindowName)
                    if candidate:
                        window_title = str(candidate)
                        break
        except Exception:
            # Screen Recording denial normally omits kCGWindowName rather than
            # raising, but other Quartz failures should be just as harmless.
            log.debug("could not read the foreground window title", exc_info=True)
    try:
        idle_seconds = max(
            0.0,
            float(
                Quartz.CGEventSourceSecondsSinceLastEventType(
                    Quartz.kCGEventSourceStateCombinedSessionState,
                    Quartz.kCGAnyInputEventType,
                )
            ),
        )
    except Exception:
        log.debug("could not read input idle time", exc_info=True)

    return ForegroundContext(
        app=app,
        bundle_id=bundle_id,
        window_title=window_title,
        idle_seconds=idle_seconds,
    )


@lru_cache(maxsize=128)
def _compile_pattern(pattern: str) -> Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def redact_title(title: str, patterns: list[str]) -> str:
    """Redact configured regex matches and cap the persisted title length."""
    redacted = title
    for pattern in patterns:
        compiled = _compile_pattern(pattern)
        if compiled is not None:
            redacted = compiled.sub("***", redacted)
    return redacted[:200]
