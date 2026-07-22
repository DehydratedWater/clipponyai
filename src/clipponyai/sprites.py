"""Qt sprite loading: Desktop Ponies GIFs + procedural clippy/orb forms.

Adapted from the original clippony client. Pony characters are animated GIFs
(rendered by QMovie) fetched into the user's data dir; clippy and orb are
drawn frame-by-frame with QPainter so they need no assets at all.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QIcon, QMovie, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)

from .config import sprites_dir

STATE_FALLBACKS = {"run": "walk", "drag": "idle", "read": "idle",
                   "magic": "idle", "teleport": "idle", "walk": "idle"}


def character_states(slug: str, base: Path | None = None) -> set[str]:
    d = (base or sprites_dir()) / slug
    return {p.stem.rsplit("_", 1)[0] for p in d.glob("*_left.gif")} if d.is_dir() else set()


def pony_movie(state: str, facing: str, slug: str = "twilight",
               base: Path | None = None) -> QMovie:
    d = (base or sprites_dir()) / slug
    if not d.is_dir():
        d = (base or sprites_dir()) / "twilight"
    path = d / f"{state}_{facing}.gif"
    while not path.exists() and state in STATE_FALLBACKS:
        state = STATE_FALLBACKS[state]
        path = d / f"{state}_{facing}.gif"
    return QMovie(str(path))


# ── procedural forms ──────────────────────────────────────────────────
def clippy_frames(size: int = 96, n: int = 24) -> list[QPixmap]:
    """A friendly paperclip with googly eyes, gently bobbing."""
    frames = []
    for i in range(n):
        bob = math.sin(2 * math.pi * i / n) * size * 0.03
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(0, bob)

        w = size * 0.34
        cx = size / 2
        top, bottom = size * 0.12, size * 0.82
        pen = QPen(QColor("#8f9bb3"), size * 0.07, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)

        path = QPainterPath()
        r = w / 2
        path.moveTo(cx - r * 0.55, bottom - r)
        path.lineTo(cx - r * 0.55, top + r * 1.6)
        path.arcTo(QRectF(cx - r * 0.55, top, r * 1.1, r * 1.6), 180, -180)
        path.lineTo(cx + r * 0.55, bottom - r * 0.9)
        path.arcTo(QRectF(cx - r, bottom - r * 1.8, r * 2, r * 1.8), 0, -180)
        path.lineTo(cx - r, top + r * 2.6)
        path.arcTo(QRectF(cx - r, top + r * 0.8, r * 2, r * 2.2), 180, -180)
        path.lineTo(cx + r, bottom - r * 1.4)
        p.drawPath(path)

        for ex in (cx - r * 0.45, cx + r * 0.45):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white"))
            p.drawEllipse(QPointF(ex, top + r * 1.1), size * 0.10, size * 0.12)
            p.setBrush(QColor("#222833"))
            p.drawEllipse(QPointF(ex, top + r * 1.25), size * 0.045, size * 0.055)
        p.end()
        frames.append(pm)
    return frames


def orb_frames(size: int = 96, n: int = 36) -> list[QPixmap]:
    """Calm pulsing blue orb — maximum cringe-hiding mode."""
    frames = []
    for i in range(n):
        pulse = 0.82 + 0.14 * math.sin(2 * math.pi * i / n)
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r = size * 0.36 * pulse
        center = QPointF(size / 2, size / 2)
        glow = QRadialGradient(center, r * 1.35)
        glow.setColorAt(0.0, QColor(120, 190, 255, 235))
        glow.setColorAt(0.55, QColor(70, 140, 235, 180))
        glow.setColorAt(1.0, QColor(40, 90, 200, 0))
        p.setBrush(glow)
        p.setPen(Qt.NoPen)
        p.drawEllipse(center, r * 1.35, r * 1.35)
        core = QRadialGradient(QPointF(size / 2 - r * 0.3, size / 2 - r * 0.3), r)
        core.setColorAt(0.0, QColor(235, 246, 255, 255))
        core.setColorAt(1.0, QColor(90, 150, 240, 255))
        p.setBrush(core)
        p.drawEllipse(center, r, r)
        p.end()
        frames.append(pm)
    return frames


def app_icon() -> QIcon:
    """Neutral rounded chat-bubble icon with a tiny spark — nothing pony."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#6c7a94"))
    path = QPainterPath()
    path.addRoundedRect(QRectF(6, 8, 52, 40), 12, 12)
    path.moveTo(20, 46)
    path.lineTo(16, 58)
    path.lineTo(32, 48)
    p.drawPath(path)
    p.setBrush(QColor("#dfe7f2"))
    star = QPainterPath()
    cx, cy, ro, ri = 32.0, 28.0, 9.0, 3.6
    for k in range(8):
        ang = math.pi / 2 + k * math.pi / 4
        rr = ro if k % 2 == 0 else ri
        pt = QPointF(cx + rr * math.cos(ang), cy - rr * math.sin(ang))
        if k == 0:
            star.moveTo(pt)
        else:
            star.lineTo(pt)
    star.closeSubpath()
    p.drawPath(star)
    p.end()
    return QIcon(pm)
