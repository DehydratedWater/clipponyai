"""The pony herself: frameless always-on-top overlay that stands, wanders,
walks where she's told, talks in bubbles, chases your cursor when a reminder
really must be seen, and accepts clicks and drags.

Adapted from the original clippony client, with characters and forms unified:
picking Rainbow Dash or Clippy from the menu switches both the sprites AND
the personality (the brain rebuilds the persona prompt).
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QMovie, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from .bubble import SpeechBubble
from .characters import CHARACTERS, FORMS, get_character
from .sprites import character_states, clippy_frames, orb_frames, pony_movie

SPRITE = 110  # base sprite box (twilight gifs are ~96-130px wide)
TICK_MS = 33
WALK_SPEED = 2.4
RUN_SPEED = 5.5

# attention mode: chase the cursor until clicked or this budget runs out.
# A corner bubble is easy to miss; a pony galloping at your pointer is not.
ATTENTION_NEAR_PX = 150          # close enough — stop and perform instead
ATTENTION_RETARGET_TICKS = 12    # re-aim at the (moving) cursor ~3x/second
ATTENTION_STANDOFF_PX = 90       # stand beside the pointer, not on top of it

# occasional idle chatter — cheap charm, no LLM involved
QUIPS = [
    "✨ did you need something? just click me!",
    "*looks around approvingly*",
    "reminder: hydration is important for ponies AND humans 💧",
    "*hums a little tune*",
    "this spot has excellent screen feng shui.",
    "psst. you're doing great. 💜",
]
DRAG_REACTIONS = ["wheee! ✨", "*flails hooves*", "ooh, are we going somewhere?", "eep! 😳"]


class PonyWindow(QWidget):
    clicked = Signal()               # open/close chat
    attention_ended = Signal(bool)   # True = user clicked (acknowledged)
    character_selected = Signal(str)
    provider_selected = Signal(str)
    screenshot_toggled = Signal(bool)
    tasks_requested = Signal()
    dashboard_requested = Signal()
    settings_requested = Signal()
    hide_requested = Signal()
    quit_requested = Signal()

    def __init__(self, character: str = "twilight", scale: float = 1.0,
                 idle_wander: bool = True) -> None:
        # macOS force-hides Qt.Tool windows whenever this app is not the active
        # application, so the pony would vanish the instant you click another
        # window (WA_MacAlwaysShowToolWindow is unreliable on Qt6). Use a plain
        # frameless always-on-top window there, shown without stealing focus.
        # Other platforms keep Qt.Tool (keeps her off the taskbar; best on X11).
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        if sys.platform != "darwin":
            flags |= Qt.Tool
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.size_px = int(SPRITE * scale)
        self.setFixedSize(int(self.size_px * 1.35), self.size_px)

        self.label = QLabel(self)
        self.label.setGeometry(0, 0, self.width(), self.height())
        self.label.setAlignment(Qt.AlignBottom | Qt.AlignHCenter)
        self.label.setScaledContents(False)

        self.bubble = SpeechBubble()
        self.bubble.clicked.connect(self._on_bubble_clicked)

        self.character = character
        self.facing = "right"
        self.state = "idle"
        self._movie: QMovie | None = None
        self._frames: list[QPixmap] = []
        self._frame_i = 0
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._next_frame)

        # menu state mirrors (set by the app so the menu shows reality)
        self.provider_names: list[str] = []
        self.active_provider: str = ""
        self.screenshot_enabled: bool = False

        # movement
        self._target: QPoint | None = None
        self._on_arrive: Callable[[], None] | None = None
        self._speed = WALK_SPEED
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._step)
        self._tick.start(TICK_MS)
        self._idle_wander = idle_wander
        self._wander_cooldown = random.randint(300, 900)  # ticks
        self._bounce_left = 0
        self._attention_ticks = 0

        # dragging
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None
        self._attention_at_press = False

        self._apply_visual()
        self.move_to_default()

    # ── visuals ──────────────────────────────────────────────────────
    @property
    def is_pony(self) -> bool:
        return not get_character(self.character).procedural

    def _apply_visual(self) -> None:
        if self._movie is not None:
            # A QMovie holds its GIF file OPEN while it exists. Detach from the
            # label and force teardown, or each state change leaks an fd until
            # the process hits EMFILE (a real crash the original shipped with).
            self._movie.stop()
            self.label.clear()
            self._movie.deleteLater()
            self._movie = None
        self._frame_timer.stop()
        if self.is_pony:
            self._movie = pony_movie(self.state, self.facing, self.character)
            self._movie.setScaledSize(self._movie_scaled_size())
            self.label.setMovie(self._movie)
            self._movie.start()
        else:
            self._frames = clippy_frames(self.size_px) if self.character == "clippy" \
                else orb_frames(self.size_px)
            self._frame_i = 0
            self.label.setPixmap(self._frames[0])
            self._frame_timer.start(66)

    def _movie_scaled_size(self):
        from PySide6.QtCore import QSize
        self._movie.jumpToFrame(0)
        sz = self._movie.currentPixmap().size()
        if sz.width() <= 0:
            return QSize(self.size_px, self.size_px)
        f = min(self.size_px / max(sz.height(), 1), (self.width() - 4) / max(sz.width(), 1))
        return QSize(int(sz.width() * f), int(sz.height() * f))

    def _next_frame(self) -> None:
        if not self._frames:
            return
        self._frame_i = (self._frame_i + 1) % len(self._frames)
        self.label.setPixmap(self._frames[self._frame_i])

    def set_state(self, state: str, facing: str | None = None) -> None:
        if facing is not None:
            self.facing = facing
        if (state, self.facing) != (self.state, facing):
            self.state = state
            self._apply_visual()

    def set_character(self, slug: str) -> None:
        self.character = slug
        self.state = "idle"
        self._apply_visual()

    def set_idle_wander(self, on: bool) -> None:
        """Toggle idle wandering at runtime (applies immediately)."""
        self._idle_wander = on

    def set_scale(self, scale: float) -> None:
        """Resize the pony at runtime (rebuilds geometry + sprites)."""
        self.size_px = int(SPRITE * scale)
        self.setFixedSize(int(self.size_px * 1.35), self.size_px)
        self.label.setGeometry(0, 0, self.width(), self.height())
        self._apply_visual()

    # ── geometry helpers ─────────────────────────────────────────────
    def _area(self):
        screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
        return screen.availableGeometry()

    def move_to_default(self) -> None:
        area = QApplication.primaryScreen().availableGeometry()
        self.move(area.right() - self.width() - 80, area.bottom() - self.height())

    def anchor_point(self) -> QPoint:
        """Where speech bubbles point: roughly the head."""
        return QPoint(self.x() + self.width() // 2, self.y() + int(self.height() * 0.18))

    # ── behavior ─────────────────────────────────────────────────────
    def walk_to(self, target: QPoint, run: bool = False,
                on_arrive: Callable[[], None] | None = None) -> None:
        area = self._area()
        x = max(area.left(), min(target.x(), area.right() - self.width()))
        y = max(area.top(), min(target.y(), area.bottom() - self.height()))
        self._target = QPoint(x, y)
        self._on_arrive = on_arrive
        self._speed = RUN_SPEED if run else WALK_SPEED
        facing = "left" if x < self.x() else "right"
        if self.is_pony:
            self.set_state("run" if run else "walk", facing)

    # ── attention mode (a reminder that must not be missed) ──────────
    def attention_active(self) -> bool:
        return self._attention_ticks > 0

    def seek_attention(self, msec: int = 30_000) -> None:
        """Chase the cursor with the bubble pinned open until the user clicks
        the pony (acknowledged) or the time budget runs out."""
        self.show()
        self.raise_()
        self._attention_ticks = max(1, msec // TICK_MS)
        self.bubble.hold(True)
        self.bounce()

    def _on_bubble_clicked(self) -> None:
        if self.attention_active():
            self._end_attention(True)

    def _end_attention(self, acknowledged: bool) -> None:
        self._attention_ticks = 0
        self.bubble.hold(False)
        self._target = None
        area = self._area()
        self.walk_to(QPoint(self.x(), area.bottom() - self.height()))
        self.attention_ended.emit(acknowledged)

    def _chase_cursor(self) -> None:
        if self._attention_ticks % ATTENTION_RETARGET_TICKS != 0:
            return
        cur = QCursor.pos()
        me = self.geometry().center()
        delta = me - cur
        dist = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
        if dist > ATTENTION_NEAR_PX:
            # aim for a standoff point just short of the pointer so she stands
            # beside it instead of covering what the user is looking at
            k = ATTENTION_STANDOFF_PX / max(dist, 1.0)
            tx = int(cur.x() + delta.x() * k) - self.width() // 2
            ty = int(cur.y() + delta.y() * k) - self.height() // 2
            self.walk_to(QPoint(tx, ty), run=True)
        elif self._target is None:
            self.set_state("idle", "left" if cur.x() < me.x() else "right")
            if random.random() < 0.3:
                self.bounce()

    def _step(self) -> None:
        if self._bounce_left > 0:
            self._bounce_left -= 1
            dy = -4 if (self._bounce_left // 4) % 2 else 4
            self.move(self.x(), self.y() + dy)
            return
        if self._attention_ticks > 0:
            self._attention_ticks -= 1
            if self._attention_ticks == 0:
                self._end_attention(False)
            else:
                self._chase_cursor()
        if self._target is None:
            self._maybe_wander()
            return
        delta = self._target - self.pos()
        dist = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
        if dist <= self._speed:
            self.move(self._target)
            self._target = None
            self.set_state("idle")
            cb, self._on_arrive = self._on_arrive, None
            if cb is not None:
                cb()
            if self.bubble.isVisible():
                self.bubble.reanchor(self.anchor_point())
            return
        self.move(
            self.x() + int(round(delta.x() / dist * self._speed)),
            self.y() + int(round(delta.y() / dist * self._speed)),
        )

    def _maybe_wander(self) -> None:
        if not self._idle_wander or self.state != "idle" or self.bubble.isVisible() \
                or self._attention_ticks > 0:
            return
        self._wander_cooldown -= 1
        if self._wander_cooldown > 0:
            return
        self._wander_cooldown = random.randint(600, 2400)
        area = self._area()
        roll = random.random()
        specials = {"read", "magic"} & character_states(self.character)
        if roll < 0.18 and self.is_pony and specials:
            # sometimes just read a book or do a little magic instead
            self.set_state(random.choice(sorted(specials)), self.facing)
            QTimer.singleShot(random.randint(4000, 9000), lambda: self.set_state("idle"))
            return
        if roll < 0.30:
            self.say(random.choice(QUIPS), msec=5000)
            return
        x = random.randint(area.left(), area.right() - self.width())
        self.walk_to(QPoint(x, area.bottom() - self.height()))

    def bounce(self) -> None:
        self._bounce_left = 16

    def say(self, text: str, msec: int | None = None) -> None:
        self.bubble.show_message(text, self.anchor_point(), msec)

    def moveEvent(self, e) -> None:
        super().moveEvent(e)
        if self.bubble.isVisible():
            self.bubble.reanchor(self.anchor_point())

    # ── input ────────────────────────────────────────────────────────
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.pos()
            self._attention_at_press = self.attention_active()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_offset is not None:
            self._target = None
            if (e.globalPosition().toPoint() - self._press_pos).manhattanLength() > 6:
                # grabbing her counts as "I saw you" — stop chasing, keep the
                # bubble on its normal clock, and don't fight the drag
                if self.attention_active():
                    self._attention_ticks = 0
                    self.bubble.hold(False)
                    self.attention_ended.emit(True)
                if self.state != "drag" and self.is_pony:
                    self.set_state("drag")
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._press_pos is not None:
            moved = (e.globalPosition().toPoint() - self._press_pos).manhattanLength()
            self._press_pos = None
            self._drag_offset = None
            if self.state == "drag":
                self.set_state("idle")
            if moved < 6:
                if self._attention_at_press:
                    # the click acknowledges the nudge; a second click opens chat
                    self._end_attention(True)
                else:
                    self.clicked.emit()
            elif moved > 60 and random.random() < 0.35:
                self.say(random.choice(DRAG_REACTIONS), msec=2600)
            self._attention_at_press = False

    def contextMenuEvent(self, e) -> None:
        menu = QMenu(self)
        char_menu = menu.addMenu("🐴 character…")
        for c in [*CHARACTERS, *FORMS]:
            icon = {"clippy": "📎 ", "orb": "🔵 "}.get(c.slug, "")
            act = char_menu.addAction(f"{icon}{c.name}")
            act.setCheckable(True)
            act.setChecked(self.character == c.slug)
            act.triggered.connect(lambda _=False, s=c.slug: self.character_selected.emit(s))
        if self.provider_names:
            prov_menu = menu.addMenu("🧠 brain…")
            for name in self.provider_names:
                act = prov_menu.addAction(name)
                act.setCheckable(True)
                act.setChecked(name == self.active_provider)
                act.triggered.connect(lambda _=False, n=name: self.provider_selected.emit(n))
        peek = menu.addAction("👀 screen peeking")
        peek.setCheckable(True)
        peek.setChecked(self.screenshot_enabled)
        peek.toggled.connect(self.screenshot_toggled.emit)
        menu.addSeparator()
        menu.addAction("💬 chat", self.clicked.emit)
        menu.addAction("📊 planner & activity", self.dashboard_requested.emit)
        menu.addAction("📋 tasks", self.tasks_requested.emit)
        menu.addAction("⚙ settings", self.settings_requested.emit)
        menu.addAction("🙈 hide", self.hide_requested.emit)
        menu.addSeparator()
        menu.addAction("✖ quit", self.quit_requested.emit)
        menu.exec(e.globalPos())
