"""Pinned, she never changes position on her own.

Three code paths move the pony without being asked — idle wandering, the nudge
cursor-chase, and the walk back to the floor when a nudge ends — and all three
funnel through `walk_to`. These tests pin each one down, plus the drag that is
the *only* sanctioned way to move her while pinned.

Runs with QT_QPA_PLATFORM=offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from clipponyai.overlay import PonyWindow


@pytest.fixture(autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pinned():
    pony = PonyWindow(idle_wander=True, stay_put=True)
    yield pony
    pony.close()


@pytest.fixture
def roaming():
    pony = PonyWindow(idle_wander=True)
    yield pony
    pony.close()


def _run_ticks(pony: PonyWindow, n: int, *, no_bounce: bool = False) -> None:
    """Drive the movement loop by hand — the real QTimer never fires offscreen.

    A bounce is a net-zero ±4px vertical hop that preempts the whole _step body,
    so sampling pos() mid-hop reads as movement. `no_bounce` suppresses it to
    isolate genuine travel, and to let the every-12-ticks chase retarget be reached.
    """
    for _ in range(n):
        if no_bounce:
            pony._bounce_left = 0
        pony._step()


# ── path 1: explicit walk requests ────────────────────────────────────
def test_pinned_walk_to_never_moves_her(pinned):
    before = pinned.pos()
    pinned.walk_to(QPoint(10, 10))
    _run_ticks(pinned, 200)
    assert pinned.pos() == before
    assert pinned._target is None


def test_pinned_walk_to_still_fires_the_arrival_callback(pinned, qt_app):
    """No caller passes on_arrive today; the contract must not silently rot."""
    fired = []
    pinned.walk_to(QPoint(10, 10), on_arrive=lambda: fired.append(True))
    qt_app.processEvents()  # the callback is deferred via singleShot(0)
    assert fired == [True]


def test_roaming_walk_to_still_moves_her(roaming):
    """The gate must not leak into the default, unpinned behaviour."""
    before = roaming.pos()
    roaming.walk_to(QPoint(roaming.x() - 300, roaming.y()))
    _run_ticks(roaming, 200)
    assert roaming.pos() != before


# ── path 2: idle wandering ────────────────────────────────────────────
def test_pinned_idle_wander_never_moves_her(pinned, monkeypatch):
    """Force the wander roll into the walk branch (roll >= 0.30) and tick past
    the cooldown; she must still be standing exactly where she was."""
    monkeypatch.setattr("clipponyai.overlay.random.random", lambda: 0.99)
    before = pinned.pos()
    pinned._wander_cooldown = 1
    _run_ticks(pinned, 200)
    assert pinned.pos() == before
    assert pinned._target is None


def test_pinned_idle_wander_still_quips(pinned, monkeypatch):
    """Pinning is not a mute switch — the stationary charm survives."""
    monkeypatch.setattr("clipponyai.overlay.random.random", lambda: 0.25)
    before = pinned.pos()
    pinned._wander_cooldown = 1
    _run_ticks(pinned, 2)
    assert pinned.bubble.isVisible()
    assert pinned.pos() == before


def test_roaming_idle_wander_does_move_her(roaming, monkeypatch):
    monkeypatch.setattr("clipponyai.overlay.random.random", lambda: 0.99)
    before = roaming.pos()
    roaming._wander_cooldown = 1
    _run_ticks(roaming, 200)
    assert roaming.pos() != before


# ── path 3: the nudge cursor-chase and the walk back ──────────────────
def test_pinned_nudge_holds_the_bubble_without_travelling(pinned):
    before = pinned.pos()
    pinned.seek_attention(1000)
    assert pinned.attention_active()
    assert pinned.bubble._held
    _run_ticks(pinned, 30, no_bounce=True)
    assert pinned.pos() == before
    assert pinned._target is None


def test_pinned_end_attention_leaves_her_where_she_stands(pinned):
    pinned.seek_attention(1000)
    _run_ticks(pinned, 5, no_bounce=True)
    before = pinned.pos()
    pinned._end_attention(True)
    _run_ticks(pinned, 200, no_bounce=True)
    assert pinned.pos() == before


def test_pinned_nudge_faces_the_cursor(pinned, monkeypatch):
    """She can't come to you, so she at least turns toward you."""
    monkeypatch.setattr("clipponyai.overlay.QCursor.pos", staticmethod(lambda: QPoint(0, 0)))
    pinned.facing = "right"
    pinned.seek_attention(5000)
    # the chase only re-aims every ATTENTION_RETARGET_TICKS, so give it a full cycle
    _run_ticks(pinned, 15, no_bounce=True)
    # the cursor is far to her upper-left, so she should now be looking that way
    assert pinned.facing == "left"


# ── toggling at runtime ───────────────────────────────────────────────
def test_set_stay_put_cancels_a_walk_in_flight(roaming):
    roaming.walk_to(QPoint(roaming.x() - 400, roaming.y()))
    _run_ticks(roaming, 3)
    assert roaming._target is not None
    roaming.set_stay_put(True)
    assert roaming.stay_put is True
    assert roaming._target is None
    assert roaming.state == "idle"
    frozen = roaming.pos()
    _run_ticks(roaming, 200)
    assert roaming.pos() == frozen


def test_releasing_her_lets_her_walk_again(pinned):
    pinned.set_stay_put(False)
    before = pinned.pos()
    pinned.walk_to(QPoint(pinned.x() - 300, pinned.y()))
    _run_ticks(pinned, 200)
    assert pinned.pos() != before


# ── remembering the spot ──────────────────────────────────────────────
def _drag(pony: PonyWindow, to: QPoint) -> None:
    start = pony.pos() + QPoint(5, 5)
    handlers = (
        (QMouseEvent.Type.MouseButtonPress, start, pony.mousePressEvent),
        (QMouseEvent.Type.MouseMove, to, pony.mouseMoveEvent),
        (QMouseEvent.Type.MouseButtonRelease, to, pony.mouseReleaseEvent),
    )
    for kind, pos, handler in handlers:
        handler(QMouseEvent(kind, QPoint(5, 5), pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def test_dragging_reports_the_drop_point(pinned):
    spy = QSignalSpy(pinned.anchor_changed)
    _drag(pinned, pinned.pos() + QPoint(200, -120))
    assert spy.count() == 1
    assert tuple(spy.at(0)) == (pinned.x(), pinned.y())


def test_a_click_is_not_a_drag(pinned):
    spy = QSignalSpy(pinned.anchor_changed)
    _drag(pinned, pinned.pos() + QPoint(5, 5))  # under the 6px hysteresis
    assert spy.count() == 0


def test_move_to_anchor_restores_a_saved_spot(pinned):
    area = QApplication.primaryScreen().availableGeometry()
    target = QPoint(area.left() + 120, area.top() + 90)
    pinned.move_to_anchor(target)
    assert pinned.pos() == target


def test_move_to_anchor_falls_back_when_the_spot_is_gone(pinned):
    """An anchor saved on a display that is no longer plugged in."""
    pinned.move_to_anchor(QPoint(-99999, -99999))
    offscreen = pinned.pos()
    pinned.move_to_anchor(None)
    assert pinned.pos() == offscreen  # both took the move_to_default() path
    area = QApplication.primaryScreen().availableGeometry()
    assert pinned.x() == area.right() - pinned.width() - 80


# ── end to end: menu toggle → config.yaml → next launch ───────────────
def _wire(pony: PonyWindow, core, config) -> None:
    """Reproduce run_gui's signal wiring so the whole round trip is exercised."""

    def on_stay_put(on: bool) -> None:
        core.set_stay_put(on, anchor=(pony.x(), pony.y()) if on else None)
        pony.set_stay_put(on)

    def on_anchor_changed(x: int, y: int) -> None:
        if config.ui.stay_put:
            core.set_anchor(x, y)

    pony.stay_put_toggled.connect(on_stay_put)
    pony.anchor_changed.connect(on_anchor_changed)


def test_menu_toggle_then_drag_persists_and_survives_a_relaunch(roaming):
    from clipponyai.app import Core
    from clipponyai.config import Config, config_path

    config = Config()
    core = Core(config)
    _wire(roaming, core, config)

    # right-click → 📌 stay put: pins her AND remembers where she stands
    roaming.stay_put_toggled.emit(True)
    assert roaming.stay_put is True
    assert Config.load(config_path()).ui.stay_put is True

    # now carry her somewhere on purpose
    area = QApplication.primaryScreen().availableGeometry()
    dropped = QPoint(area.left() + 200, area.top() + 150)
    _drag(roaming, dropped)
    saved = Config.load(config_path())
    assert (saved.ui.anchor_x, saved.ui.anchor_y) == (roaming.x(), roaming.y())

    # next launch restores that spot instead of the bottom-right default
    relaunched = PonyWindow(
        stay_put=saved.ui.stay_put,
        anchor=QPoint(saved.ui.anchor_x, saved.ui.anchor_y),
    )
    try:
        assert relaunched.pos() == roaming.pos()
        assert relaunched.stay_put is True
    finally:
        relaunched.close()


def test_dragging_while_unpinned_is_not_remembered(roaming):
    from clipponyai.app import Core
    from clipponyai.config import Config, config_path

    config = Config()
    core = Core(config)
    config.save()
    _wire(roaming, core, config)

    _drag(roaming, roaming.pos() + QPoint(150, -100))
    saved = Config.load(config_path())
    assert saved.ui.anchor_x is None and saved.ui.anchor_y is None


# ── the set_state fix the "face the cursor" behaviour depends on ──────
def test_set_state_applies_a_facing_only_change(roaming):
    roaming.set_state("idle", "right")
    assert (roaming.state, roaming.facing) == ("idle", "right")
    roaming.set_state("idle", "left")
    assert roaming.facing == "left"


def test_set_state_is_a_noop_when_nothing_changed(roaming, monkeypatch):
    roaming.set_state("idle", "right")
    applied = []
    monkeypatch.setattr(roaming, "_apply_visual", lambda: applied.append(True))
    roaming.set_state("idle", "right")
    roaming.set_state("idle")
    assert applied == []
