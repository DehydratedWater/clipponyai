"""The pony must never steal keyboard focus when she speaks on her own.

Qt's QWidget.raise_() ends in [NSApp activateIgnoringOtherApps:YES] on macOS,
so a bubble popping up mid-sentence swallowed whatever the user was typing in
another app. These tests pin the call sites to the non-activating path.

Runs with QT_QPA_PLATFORM=offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QWidget

from clipponyai import macos
from clipponyai.bubble import SpeechBubble
from clipponyai.overlay import PonyWindow


@pytest.fixture
def no_raise(monkeypatch):
    """Trip a wire on QWidget.raise_() — nothing unprompted may call it."""
    calls = []
    monkeypatch.setattr(QWidget, "raise_", lambda self: calls.append(self))
    return calls


def test_speaking_never_raises(no_raise):
    QApplication.instance() or QApplication([])
    bubble = SpeechBubble()
    bubble.show_message("hey, you promised to call mom", QPoint(400, 400))
    assert bubble.isVisible()
    assert no_raise == []


def test_seek_attention_never_raises(no_raise):
    QApplication.instance() or QApplication([])
    pony = PonyWindow(idle_wander=False)
    pony.seek_attention(1000)
    assert pony.attention_active()
    assert no_raise == []


def test_overlays_show_without_activating():
    QApplication.instance() or QApplication([])
    pony = PonyWindow(idle_wander=False)
    assert pony.testAttribute(Qt.WA_ShowWithoutActivating)
    assert pony.bubble.testAttribute(Qt.WA_ShowWithoutActivating)
    # the bubble is pure output — it must not be focusable at all
    assert pony.bubble.windowFlags() & Qt.WindowDoesNotAcceptFocus


def test_raise_without_activating_falls_back_off_macos(monkeypatch, no_raise):
    """Elsewhere raise_() is harmless, and still wanted for z-order."""
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(macos.sys, "platform", "linux")
    w = QWidget()
    macos.raise_without_activating(w)
    assert no_raise == [w]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only path")
def test_raise_without_activating_is_noop_without_pyobjc(monkeypatch, no_raise):
    """No pyobjc: skip the reorder rather than fall back to a focus-stealing
    raise_() — WindowStaysOnTopHint already keeps these windows on top."""
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(macos, "_ns_window", lambda widget: None)
    w = QWidget()
    w.show()
    macos.raise_without_activating(w)
    assert no_raise == []
