from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from clipponyai import screen_context
from clipponyai.screen_context import ForegroundContext, foreground_context, redact_title


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS APIs")
def test_live_foreground_context_has_application_and_idle_time():
    context = foreground_context()

    assert isinstance(context, ForegroundContext)
    assert context.app
    assert context.idle_seconds >= 0


def test_missing_quartz_returns_cocoa_context(monkeypatch):
    class Application:
        def localizedName(self):
            return "Test App"

        def bundleIdentifier(self):
            return "example.test"

        def processIdentifier(self):
            return 42

    workspace = SimpleNamespace(frontmostApplication=lambda: Application())
    appkit = SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setattr(screen_context.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Quartz", None)

    context = foreground_context()

    assert context == ForegroundContext(
        app="Test App",
        bundle_id="example.test",
        window_title="",
        idle_seconds=0.0,
    )


def test_redact_title_replaces_patterns_and_caps_length():
    title = "Account 1234 - " + ("private " * 100)

    redacted = redact_title(title, [r"\d+", "private"])

    assert redacted.startswith("Account *** - ***")
    assert "1234" not in redacted
    assert len(redacted) == 200


def test_redact_title_skips_invalid_pattern():
    assert redact_title("visible title", ["["]) == "visible title"
