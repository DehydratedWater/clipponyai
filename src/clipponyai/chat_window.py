"""Small floating chat window: the same one conversation, in a typeable form."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from .markdown import md_to_html

BUBBLE_BG = {"user": "#453a6e", "assistant": "#23324a", "system": "#2a2440"}
NAME_COLORS = {"user": "#c9b7f5", "assistant": "#8fd3f0", "system": "#8d86a8"}


class ChatWindow(QWidget):
    send_text = Signal(str)
    tasks_clicked = Signal()
    closed = Signal()

    def __init__(self, pony_name: str = "pony") -> None:
        super().__init__(None, Qt.WindowStaysOnTopHint | Qt.Tool)
        self.pony_name = pony_name
        self.setWindowTitle("clipponyai")
        self.resize(420, 480)
        self.setStyleSheet("""
            QWidget { background: #1e1a2e; color: #e8e4f5; font-size: 13px; }
            QTextBrowser { background: #14121f; border: 1px solid #3a3355;
                           border-radius: 8px; padding: 6px; }
            QLineEdit { background: #262138; border: 1px solid #3a3355;
                        border-radius: 8px; padding: 8px 10px; }
            QLineEdit:focus { border-color: #b28ff2; }
            QPushButton { background: #b28ff2; color: #191430; border: none;
                          border-radius: 8px; padding: 8px 14px; font-weight: 600; }
            QPushButton:hover { background: #c4a8f7; }
            QLabel#typing { color: #9b93b8; font-style: italic; }
        """)

        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        # The markdown renderer wraps paragraphs in <p> tags; Qt gives those default
        # block margins (~12px) that create a large gap after the header line.
        # Zero them out so bubbles stay compact.
        self.log.document().setDefaultStyleSheet(
            "p { margin: 0px; padding: 0px; }"
            "pre { margin: 4px 0px; }"
            "ul, ol { margin: 2px 0px; padding-left: 20px; }"
        )
        self.typing_label = QLabel("")
        self.typing_label.setObjectName("typing")
        self.input = QLineEdit()
        self.input.setPlaceholderText("talk to your pony…")
        self.input.returnPressed.connect(self._submit)
        send_btn = QPushButton("send")
        send_btn.clicked.connect(self._submit)
        tasks_btn = QPushButton("📋")
        tasks_btn.setToolTip("show everything she's tracking (verbatim)")
        tasks_btn.setFixedWidth(44)
        tasks_btn.clicked.connect(self.tasks_clicked.emit)

        row = QHBoxLayout()
        row.addWidget(tasks_btn)
        row.addWidget(self.input)
        row.addWidget(send_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.log)
        lay.addWidget(self.typing_label)
        lay.addLayout(row)

    def set_pony_name(self, name: str) -> None:
        self.pony_name = name

    def add_message(self, role: str, text: str, when: datetime | None = None) -> None:
        when = when or datetime.now()
        if role == "system":
            self.log.append(
                f'<p align="center" style="color:#8d86a8;font-size:11px">'
                f'{md_to_html(text)} · {when:%H:%M}</p>')
        else:
            who = "you" if role == "user" else html.escape(self.pony_name)
            bg = BUBBLE_BG.get(role, "#3a3355")
            color = NAME_COLORS.get(role, "#c9b7f5")
            header = (f'<span style="color:{color};font-weight:600">{who}</span> '
                      f'<span style="color:#8d86a8;font-size:10px">{when:%H:%M}</span>')
            cell = f'<td bgcolor="{bg}" style="padding:7px">{header}<br>{md_to_html(text)}</td>'
            spacer = '<td width="18%"></td>'
            row = (spacer + cell) if role == "user" else (cell + spacer)
            self.log.append(
                f'<table width="100%" cellspacing="2" cellpadding="0"><tr>{row}</tr></table>')
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def load_history(self, messages: list[dict]) -> None:
        self.log.clear()
        for m in messages:
            self.add_message(m["role"], m["content"])

    def show_typing(self, on: bool) -> None:
        self.typing_label.setText(f"{self.pony_name} is thinking…" if on else "")

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.send_text.emit(text)
            self.input.clear()

    def closeEvent(self, e) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(e)
