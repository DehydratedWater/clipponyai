"""Screen capture (mss) — runs only when screenshot_enabled is on in config."""

from __future__ import annotations

import logging

log = logging.getLogger("clipponyai.capture")


def take_screenshot() -> bytes | None:
    """PNG bytes of all screens combined, or None on failure."""
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[0]
            img = sct.grab(monitor)
            return mss.tools.to_png(img.rgb, img.size)
    except Exception:
        log.exception("screenshot failed")
        return None
