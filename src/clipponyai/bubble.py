"""Position-aware speech bubble: never clips off-screen, follows the pony."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QTextDocument
from PySide6.QtWidgets import QApplication, QWidget

from .macos import join_all_spaces, raise_without_activating
from .markdown import md_to_html

PAD = 12
TAIL = 14
MAX_TEXT_W = 300


class SpeechBubble(QWidget):
    clicked = Signal()  # user dismissed the bubble by clicking it

    def __init__(self) -> None:
        # Same macOS Qt.Tool auto-hide fix as the pony window: drop Qt.Tool on
        # macOS so the bubble stays visible when the app is not frontmost.
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                 | Qt.WindowDoesNotAcceptFocus)
        if sys.platform != "darwin":
            flags |= Qt.Tool
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._text = ""
        self._doc = QTextDocument(self)
        self._doc.setDocumentMargin(0)
        self._tail_down = True  # tail points down at the pony
        self._tail_x = 0.5
        self._held = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def event(self, e) -> bool:  # noqa: N802
        # Re-apply on every Show/WinIdChange: Qt recreates the NSWindow when
        # window flags change, which wipes the collection behavior.
        if e.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange):
            join_all_spaces(self, overlay=True)
        return super().event(e)

    def default_msec(self) -> int:
        """Generous dwell: the reader may only glance over mid-task, so give
        ~2x a comfortable reading pace plus a fat base before it fades."""
        return min(8000 + len(self._text) * 80, 45000)

    def hold(self, on: bool) -> None:
        """Pin the bubble open (attention mode) — releasing restarts the
        normal fade clock so the text still gets its full reading time."""
        self._held = on
        if on:
            self._timer.stop()
        elif self.isVisible():
            self._timer.start(self.default_msec())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Any click dismisses — even a held (attention-mode) bubble."""
        self._timer.stop()
        self.hide()
        self.clicked.emit()

    # ── layout ────────────────────────────────────────────────────────
    def _content_size(self) -> tuple[int, int]:
        self._doc.setTextWidth(-1)
        ideal = min(self._doc.idealWidth(), MAX_TEXT_W)
        self._doc.setTextWidth(ideal)
        size = self._doc.size()
        return max(int(size.width()) + 6, 40), max(int(size.height()), 18)

    def show_message(self, text: str, anchor: QPoint, msec: int | None = None) -> None:
        """anchor = point the tail should point at (pony's head), in globals."""
        self._text = text.strip()
        self._doc.setHtml(f'<div style="color:#efeaff">{md_to_html(self._text)}</div>')
        self.reanchor(anchor)
        self.show()
        # never raise_(): speaking is unprompted, and on macOS that would
        # activate the app and swallow the keystroke the user is mid-way
        # through typing somewhere else.
        raise_without_activating(self)
        if self._held:
            self._timer.stop()
            return
        self._timer.start(msec if msec is not None else self.default_msec())

    def reanchor(self, anchor: QPoint) -> None:
        """(Re)position near the anchor — also called while the pony moves."""
        cw, ch = self._content_size()
        w = cw + PAD * 2
        h = ch + PAD * 2 + TAIL
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        area = screen.availableGeometry()

        # prefer above the anchor; flip below when clipping the top
        x = anchor.x() - w // 2
        y = anchor.y() - h - 6
        self._tail_down = True
        if y < area.top():
            y = anchor.y() + 28
            self._tail_down = False
        x = max(area.left() + 4, min(x, area.right() - w - 4))
        y = max(area.top() + 4, min(y, area.bottom() - h - 4))
        self._tail_x = min(max((anchor.x() - x) / max(w, 1), 0.08), 0.92)

        self.resize(w, h)
        self.move(x, y)
        self.update()

    # ── painting ──────────────────────────────────────────────────────
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        body = QRectF(1, 1 if self._tail_down else TAIL + 1, w - 2, h - TAIL - 2)
        path = QPainterPath()
        path.addRoundedRect(body, 12, 12)
        tx = self._tail_x * w
        if self._tail_down:
            path.moveTo(tx - 9, body.bottom())
            path.lineTo(tx, h - 1)
            path.lineTo(tx + 9, body.bottom())
        else:
            path.moveTo(tx - 9, body.top())
            path.lineTo(tx, 1)
            path.lineTo(tx + 9, body.top())
        path.closeSubpath()
        p.setPen(QColor("#b28ff2"))
        p.setBrush(QColor(30, 26, 46, 242))
        p.drawPath(path)

        if self._text:
            p.save()
            p.translate(body.left() + PAD, body.top() + PAD)
            self._doc.drawContents(p)
            p.restore()
        p.end()
