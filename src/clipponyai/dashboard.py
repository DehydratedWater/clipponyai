"""Polished PySide6 dashboard: tasks, routines, goals, rules, activity, token usage.

Uses the existing shared stores and engines from Core.  No designer files or
external dependencies beyond PySide6.  Each tab is a self-contained widget
so the file stays navigable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .accountability import (
    AccountabilityRule,
    Goal,
    Routine,
)
from .digest import summarize_categories
from .routines import VALID_CADENCES, validate_time_of_day
from .rules import validate_add_rule, validate_update_rule
from .tasks import Task

log = logging.getLogger("clipponyai.dashboard")

# ── palette (matches settings dialog) ──────────────────────────────────

_DASHBOARD_STYLE = """
    QMainWindow, QDialog { background: #1e1a2e; color: #e8e4f5; }
    QTabWidget::pane { background: #1e1a2e; border: 1px solid #3a3355; border-radius: 6px; }
    QTabBar::tab { background: #262138; color: #c9b7f5; padding: 6px 14px;
                   border-radius: 6px 6px 0 0; margin-right: 2px; min-width: 70px; }
    QTabBar::tab:selected { background: #3a3355; color: #efeaff; font-weight: 600; }
    QGroupBox { color: #c9b7f5; font-weight: 600; border: 1px solid #3a3355;
                border-radius: 6px; margin-top: 8px; padding-top: 10px; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
    QLabel { color: #e8e4f5; }
    QLineEdit { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                border-radius: 4px; padding: 4px 6px; }
    QLineEdit:focus { border-color: #b28ff2; }
    QComboBox { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                border-radius: 4px; padding: 4px; }
    QSpinBox { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
               border-radius: 4px; padding: 4px; }
    QTimeEdit { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                border-radius: 4px; padding: 4px; }
    QCheckBox { color: #e8e4f5; spacing: 6px; }
    QPushButton { background: #b28ff2; color: #191430; border: none;
                  border-radius: 6px; padding: 6px 14px; font-weight: 600; }
    QPushButton:hover { background: #c4a8f7; }
    QPushButton:disabled { background: #5a5270; color: #8d86a8; }
    QPushButton#danger { background: #a33; color: #eee; }
    QPushButton#danger:hover { background: #c55; }
    QPushButton#secondary { background: #3a3355; color: #e8e4f5; }
    QPushButton#secondary:hover { background: #4a4365; }
    QTableWidget { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                   border-radius: 4px; gridline-color: #3a3355; alternate-background-color: #2c2745; }
    QTableWidget::item { padding: 3px; }
    QTableWidget::item:selected { background: #4a4365; color: #efeaff; }
    QHeaderView::section { background: #3a3355; color: #c9b7f5; border: none;
                           padding: 4px 6px; font-weight: 600; }
    QTreeWidget { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                  border-radius: 4px; }
    QTreeWidget::item { padding: 2px; }
    QTreeWidget::item:selected { background: #4a4365; color: #efeaff; }
    QPlainTextEdit, QTextEdit { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                                border-radius: 4px; padding: 4px; }
    QToolBar { background: #262138; border: none; spacing: 4px; }
    QToolButton { background: transparent; color: #e8e4f5; border: none; padding: 4px 8px; }
    QToolButton:hover { background: #3a3355; }
    QListWidget { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                  border-radius: 4px; }
    QListWidget::item { padding: 2px; }
    QListWidget::item:selected { background: #4a4365; }
    QFrame#separator { background: #3a3355; max-height: 1px; }
"""

# ── helpers ─────────────────────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _fmt_minutes(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h{remainder:02d}m" if hours else f"{remainder}m"


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def _cadence_label(r: Routine) -> str:
    if r.cadence == "daily":
        return "Daily"
    if r.cadence == "weekdays":
        if r.weekdays:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            return ", ".join(days[d] for d in r.weekdays)
        return "Weekdays (Mon-Fri)"
    if r.cadence == "monthly":
        d = r.day_of_month or 1
        return f"Monthly (day {d})"
    return r.cadence


def _status_icon(status: str) -> str:
    return {
        "active": "🟢",
        "achieved": "✅",
        "cancelled": "⚪",
        "pending": "🔵",
        "done": "✅",
        "dropped": "⚰️",
    }.get(status, status)


def _empty_label(text: str = "") -> QLabel:
    lbl = QLabel(text or "No data yet.")
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet("color: #8d86a8; font-size: 14px; padding: 20px;")
    return lbl


def _toolbar_spacer() -> QWidget:
    """Spacer widget for QToolBar (which lacks addStretch)."""
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w


# ── Tasks tab ──────────────────────────────────────────────────────────


class TasksTab(QWidget):
    """Task list with add/complete/snooze/edit actions."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.store = core.store
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_add = QToolButton()
        btn_add.setText("➕ Add Task")
        btn_add.setToolTip("Add a new one-time task")
        btn_add.clicked.connect(self._add_task)
        toolbar.addWidget(btn_add)

        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh tasks")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        lay.addWidget(toolbar)

        # Tree widget: sections as parent items, tasks as children
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ID", "Title", "Status", "Deadline", "Notes"])
        self.tree.setHeaderHidden(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setSortingEnabled(False)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self.tree)

        # Action bar
        action_lay = QHBoxLayout()
        self.btn_complete = QPushButton("✅ Complete")
        self.btn_complete.setToolTip("Mark selected task as done")
        self.btn_complete.clicked.connect(self._complete_selected)
        self.btn_snooze = QPushButton("⏰ Snooze")
        self.btn_snooze.setToolTip("Reschedule selected task")
        self.btn_snooze.clicked.connect(self._snooze_selected)
        self.btn_edit = QPushButton("✏️ Edit Deadline")
        self.btn_edit.setToolTip("Change deadline of selected task")
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_cancel = QPushButton("❌ Cancel")
        self.btn_cancel.setToolTip("Cancel selected task")
        self.btn_cancel.setProperty("danger", True)
        self.btn_cancel.clicked.connect(self._cancel_selected)
        self.btn_restore = QPushButton("♻️ Restore")
        self.btn_restore.setToolTip("Restore dropped/cancelled task")
        self.btn_restore.setProperty("secondary", True)
        self.btn_restore.clicked.connect(self._restore_selected)
        for btn in (
            self.btn_complete,
            self.btn_snooze,
            self.btn_edit,
            self.btn_cancel,
            self.btn_restore,
        ):
            action_lay.addWidget(btn)
        action_lay.addStretch()
        lay.addLayout(action_lay)

    def refresh(self):
        """Reload tasks from store and rebuild the tree."""
        self.tree.clear()
        self.tree.setHeaderLabels(["ID", "Title", "Status", "Deadline", "Notes"])

        now = datetime.now()
        today_end = now.replace(hour=23, minute=59, second=59)
        week_end = today_end + timedelta(days=7)

        sections = {
            "🔴 Overdue": [],
            "📌 Today": [],
            "📅 Upcoming (7d)": [],
            "📆 Later": [],
            "🗂 No deadline": [],
        }

        for task in self.store.pending():
            if task.deadline is None:
                sections["🗂 No deadline"].append(task)
            elif task.deadline < now:
                sections["🔴 Overdue"].append(task)
            elif task.deadline <= today_end:
                sections["📌 Today"].append(task)
            elif task.deadline <= week_end:
                sections["📅 Upcoming (7d)"].append(task)
            else:
                sections["📆 Later"].append(task)

        # Completed / dropped / cancelled
        completed = self.store.by_status("done", limit=50)
        dropped = self.store.by_status("dropped", limit=20)
        cancelled = self.store.by_status("cancelled", limit=20)

        for label, tasks in sections.items():
            if not tasks:
                continue
            parent = QTreeWidgetItem(self.tree, [label, "", "", "", ""])
            parent.setFont(
                0, Qt.systemFont().font() if hasattr(Qt, "systemFont") else parent.font(0)
            )
            for t in tasks:
                child = QTreeWidgetItem(
                    parent,
                    [
                        f"#{t.id}",
                        t.title,
                        _status_icon(t.status),
                        _fmt_dt(t.deadline),
                        t.notes or "",
                    ],
                )
                child.setData(0, Qt.UserRole, t)

        if completed:
            parent = QTreeWidgetItem(self.tree, ["✅ Completed", "", "", "", ""])
            for t in completed:
                child = QTreeWidgetItem(
                    parent,
                    [
                        f"#{t.id}",
                        t.title,
                        _status_icon(t.status),
                        _fmt_dt(t.completed_at),
                        t.notes or "",
                    ],
                )
                child.setData(0, Qt.UserRole, t)

        if dropped:
            parent = QTreeWidgetItem(self.tree, ["⚰️ Dropped", "", "", "", ""])
            for t in dropped:
                child = QTreeWidgetItem(
                    parent,
                    [
                        f"#{t.id}",
                        t.title,
                        _status_icon(t.status),
                        _fmt_dt(t.deadline),
                        t.notes or "",
                    ],
                )
                child.setData(0, Qt.UserRole, t)

        if cancelled:
            parent = QTreeWidgetItem(self.tree, ["⚪ Cancelled", "", "", "", ""])
            for t in cancelled:
                child = QTreeWidgetItem(
                    parent,
                    [
                        f"#{t.id}",
                        t.title,
                        _status_icon(t.status),
                        _fmt_dt(t.deadline),
                        t.notes or "",
                    ],
                )
                child.setData(0, Qt.UserRole, t)

        # Expand all section headers
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

        # Auto-size columns
        for col in range(5):
            self.tree.resizeColumnToContents(col)

    def _get_selected_task(self) -> Task | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        task = item.data(0, Qt.UserRole)
        return task if isinstance(task, Task) else None

    def _on_double_click(self, item, column):
        task = item.data(0, Qt.UserRole)
        if isinstance(task, Task) and task.status == "pending":
            self._complete_selected()

    def _add_task(self):
        dialog = AddTaskDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        title = dialog.title_field.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Task title is required.")
            return
        notes = dialog.notes_field.text().strip()
        deadline = None
        if dialog.deadline_check.isChecked():
            dt = dialog.datetime_edit.dateTime().toPython()
            deadline = dt
        try:
            task, created = self.store.add(title, notes=notes, deadline=deadline)
            self.refresh()
            if not created:
                self.core.brain.say(f"Merged into existing task: {task.title}")
        except ValueError as e:
            QMessageBox.warning(self, "Error", str(e))

    def _complete_selected(self):
        task = self._get_selected_task()
        if task is None:
            QMessageBox.information(self, "Select Task", "Double-click a task or select one first.")
            return
        if task.status != "pending":
            return
        self.store.complete(task, actor="dashboard")
        self.refresh()

    def _snooze_selected(self):
        task = self._get_selected_task()
        if task is None or task.status != "pending":
            return
        dialog = SnoozeDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        until = dialog.datetime_edit.dateTime().toPython()
        self.store.snooze(task, until, actor="dashboard")
        self.refresh()

    def _edit_selected(self):
        task = self._get_selected_task()
        if task is None or task.status != "pending":
            return
        dialog = EditDeadlineDialog(task, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        clear = dialog.clear_check.isChecked()
        deadline = None if clear else dialog.datetime_edit.dateTime().toPython()
        self.store.set_deadline(task, deadline, actor="dashboard")
        self.refresh()

    def _cancel_selected(self):
        task = self._get_selected_task()
        if task is None or task.status != "pending":
            return
        self.store.cancel(task, actor="dashboard", note="cancelled from dashboard")
        self.refresh()

    def _restore_selected(self):
        task = self._get_selected_task()
        if task is None:
            QMessageBox.information(
                self, "Select Task", "Select a dropped or cancelled task to restore."
            )
            return
        if task.status not in ("dropped", "cancelled"):
            return
        restored = self.store.restore(task.title, actor="dashboard")
        if restored:
            self.refresh()
        else:
            QMessageBox.information(self, "Restore", "Could not find a matching task to restore.")


class AddTaskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Add Task")
        self.setMinimumWidth(360)
        self.setStyleSheet(_DASHBOARD_STYLE)

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.title_field = QLineEdit()
        self.title_field.setPlaceholderText("e.g. Finish quarterly report")
        form.addRow("Title *:", self.title_field)

        self.notes_field = QLineEdit()
        self.notes_field.setPlaceholderText("Optional notes")
        form.addRow("Notes:", self.notes_field)

        self.deadline_check = QCheckBox("Set deadline")
        self.datetime_edit = __import__(
            "PySide6.QtWidgets", fromlist=["QDateTimeEdit"]
        ).QDateTimeEdit(datetime.now() + timedelta(hours=1))
        self.datetime_edit.setCalendarPopup(True)
        self.datetime_edit.setEnabled(False)
        self.deadline_check.toggled.connect(self.datetime_edit.setEnabled)
        form.addRow("", self.deadline_check)
        form.addRow("", self.datetime_edit)

        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


class SnoozeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Snooze Task")
        self.setMinimumWidth(320)
        self.setStyleSheet(_DASHBOARD_STYLE)

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.datetime_edit = __import__(
            "PySide6.QtWidgets", fromlist=["QDateTimeEdit"]
        ).QDateTimeEdit(datetime.now() + timedelta(hours=2))
        self.datetime_edit.setCalendarPopup(True)
        form.addRow("Remind me at:", self.datetime_edit)

        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


class EditDeadlineDialog(QDialog):
    def __init__(self, task: Task, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle(f"Edit Deadline — #{task.id}")
        self.setMinimumWidth(320)
        self.setStyleSheet(_DASHBOARD_STYLE)

        lay = QVBoxLayout(self)
        form = QFormLayout()

        QLabel(f"Task: {task.title}").setParent(self)
        lay.addWidget(lay.widget(0) if lay.count() > 0 else QLabel())

        self.datetime_edit = __import__(
            "PySide6.QtWidgets", fromlist=["QDateTimeEdit"]
        ).QDateTimeEdit()
        self.datetime_edit.setCalendarPopup(True)
        if task.deadline:
            self.datetime_edit.setDateTime(task.deadline)
        else:
            self.datetime_edit.setDateTime(datetime.now() + timedelta(hours=1))

        self.clear_check = QCheckBox("Clear deadline (no due time)")
        if task.deadline is None:
            self.clear_check.setChecked(True)
            self.datetime_edit.setEnabled(False)
        self.clear_check.toggled.connect(lambda c: self.datetime_edit.setEnabled(not c))

        form.addRow("Deadline:", self.datetime_edit)
        form.addRow("", self.clear_check)
        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


# ── Routines tab ───────────────────────────────────────────────────────


class RoutinesTab(QWidget):
    """Routine table with add/edit/complete/skip/toggle/archive."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.routine_store = core.accountability["routines"]
        self.completion_store = core.accountability["routine_completions"]
        self.routine_engine = core.brain._routine_engine
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_add = QToolButton()
        btn_add.setText("➕ Add Routine")
        btn_add.setToolTip("Add a new recurring routine")
        btn_add.clicked.connect(self._add_routine)
        toolbar.addWidget(btn_add)
        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh routines")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        lay.addWidget(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Title",
                "Cadence",
                "Time",
                "Deadline",
                "Enabled",
                "Streak",
                "Actions",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().resizeSection(1, 180)
        self.table.horizontalHeader().resizeSection(2, 120)
        self.table.horizontalHeader().resizeSection(7, 180)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        self._empty = _empty_label("No routines yet. Click '➕ Add Routine' to create one.")
        lay.addWidget(self._empty)

    def refresh(self):
        routines = self.routine_store.list_all(include_archived=True)
        self.table.setRowCount(0)
        self._empty.setVisible(len(routines) == 0)
        self.table.setVisible(len(routines) > 0)

        for r in routines:
            row = self.table.rowCount()
            self.table.insertRow(row)

            completions = self.completion_store.by_routine(r.id)
            from .routines import current_streak as calc_current
            from .routines import longest_streak as calc_longest

            cur_streak = calc_current(r, completions)
            long_streak = calc_longest(r, completions)

            self.table.setItem(row, 0, QTableWidgetItem(str(r.id)))
            title_item = QTableWidgetItem(r.title)
            if r.archived_at is not None:
                title_item.setForeground(Qt.darkGray)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, QTableWidgetItem(_cadence_label(r)))
            self.table.setItem(row, 3, QTableWidgetItem(r.time_of_day or ""))
            self.table.setItem(row, 4, QTableWidgetItem(r.deadline_time or ""))

            enabled_check = QCheckBox()
            enabled_check.setChecked(r.enabled)
            enabled_check.toggled.connect(lambda _, rid=r.id: self._toggle_routine(rid))
            self.table.setCellWidget(row, 5, enabled_check)

            self.table.setItem(row, 6, QTableWidgetItem(f"{cur_streak}/{long_streak}"))

            # Actions column
            actions = QWidget()
            actions_lay = QHBoxLayout(actions)
            actions_lay.setContentsMargins(2, 0, 2, 0)
            actions_lay.setSpacing(2)

            if r.archived_at is None:
                btn_done = QToolButton()
                btn_done.setText("✅")
                btn_done.setToolTip("Complete today")
                btn_done.clicked.connect(lambda _, rid=r.id: self._complete_today(rid))
                actions_lay.addWidget(btn_done)

                btn_skip = QToolButton()
                btn_skip.setText("⏭️")
                btn_skip.setToolTip("Skip today")
                btn_skip.clicked.connect(lambda _, rid=r.id: self._skip_today(rid))
                actions_lay.addWidget(btn_skip)

                btn_archive = QToolButton()
                btn_archive.setText("📦")
                btn_archive.setToolTip("Archive routine")
                btn_archive.clicked.connect(lambda _, rid=r.id: self._archive_routine(rid))
                actions_lay.addWidget(btn_archive)
            else:
                btn_unarchive = QToolButton()
                btn_unarchive.setText("♻️")
                btn_unarchive.setToolTip("Unarchive routine")
                btn_unarchive.clicked.connect(lambda _, rid=r.id: self._unarchive_routine(rid))
                actions_lay.addWidget(btn_unarchive)

            btn_edit = QToolButton()
            btn_edit.setText("✏️")
            btn_edit.setToolTip("Edit routine")
            btn_edit.clicked.connect(lambda _, rid=r.id: self._edit_routine(rid))
            actions_lay.addWidget(btn_edit)

            actions_lay.addStretch()
            self.table.setCellWidget(row, 7, actions)

            # Store the full routine on the already-created ID item.
            self.table.item(row, 0).setData(Qt.UserRole, r)

    def _toggle_routine(self, routine_id: int):
        self.routine_store.toggle(routine_id)
        self.refresh()

    def _complete_today(self, routine_id: int):
        self.routine_engine.complete_today(routine_id, datetime.now())
        self.refresh()

    def _skip_today(self, routine_id: int):
        self.routine_engine.skip_today(routine_id, datetime.now())
        self.refresh()

    def _archive_routine(self, routine_id: int):
        self.routine_store.archive(routine_id)
        self.refresh()

    def _unarchive_routine(self, routine_id: int):
        self.routine_store.unarchive(routine_id)
        self.refresh()

    def _add_routine(self):
        dialog = AddRoutineDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.collect()
            self.routine_store.add(**data)
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Validation", str(e))

    def _edit_routine(self, routine_id: int):
        routine = self.routine_store.get(routine_id)
        dialog = AddRoutineDialog(existing=routine, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.collect()
            self.routine_store.update(routine_id, **data)
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Validation", str(e))


class AddRoutineDialog(QDialog):
    def __init__(self, existing: Routine | None = None, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Edit Routine" if existing else "Add Routine")
        self.setMinimumWidth(400)
        self.setStyleSheet(_DASHBOARD_STYLE)
        self.existing = existing

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.title_field = QLineEdit(existing.title if existing else "")
        self.title_field.setPlaceholderText("e.g. Morning meditation")
        form.addRow("Title *:", self.title_field)

        self.notes_field = QLineEdit(existing.notes if existing else "")
        self.notes_field.setPlaceholderText("Optional notes")
        form.addRow("Notes:", self.notes_field)

        # Cadence
        self.cadence_combo = __import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox()
        for c in sorted(VALID_CADENCES):
            self.cadence_combo.addItem(c.capitalize(), c)
        if existing:
            self.cadence_combo.setCurrentText(existing.cadence.capitalize())
        self.cadence_combo.currentTextChanged.connect(self._on_cadence_change)
        form.addRow("Cadence:", self.cadence_combo)

        # Weekday checkboxes (for weekdays cadence)
        weekday_box = QGroupBox("Weekdays")
        wk_lay = QHBoxLayout(weekday_box)
        self.weekday_checks = []
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        selected_days = existing.weekdays if existing else list(range(5))
        for i, name in enumerate(day_names):
            chk = QCheckBox(name)
            chk.setChecked(i in selected_days)
            self.weekday_checks.append(chk)
            wk_lay.addWidget(chk)
        weekday_box.setVisible(self.cadence_combo.currentData() == "weekdays")
        form.addRow("", weekday_box)
        self._weekday_box = weekday_box

        # Day of month (for monthly cadence)
        self.dom_spin = QSpinBox()
        self.dom_spin.setRange(1, 31)
        self.dom_spin.setValue(existing.day_of_month or 1 if existing else 1)
        dom_label = QLabel("Day of month (clamped to month end, e.g. day 31 in Feb = last day)")
        dom_row = QWidget()
        dom_row_lay = QHBoxLayout(dom_row)
        dom_row_lay.setContentsMargins(0, 0, 0, 0)
        dom_row_lay.addWidget(self.dom_spin)
        dom_row_lay.addWidget(dom_label)
        dom_row.setVisible(self.cadence_combo.currentData() == "monthly")
        form.addRow("Day of month:", dom_row)
        self._dom_row = dom_row

        # Time of day
        from PySide6.QtCore import QTime

        self.time_edit = QTimeEdit()
        if existing and existing.time_of_day:
            h, m = existing.time_of_day.split(":")
            self.time_edit.setTime(QTime(int(h), int(m)))
        else:
            self.time_edit.setTime(QTime(9, 0))
        form.addRow("Time of day:", self.time_edit)

        # Deadline time
        self.deadline_edit = QTimeEdit()
        if existing and existing.deadline_time:
            h, m = existing.deadline_time.split(":")
            self.deadline_edit.setTime(QTime(int(h), int(m)))
        else:
            self.deadline_edit.setTime(QTime(21, 0))
        self.deadline_check = QCheckBox("Set deadline time")
        if existing and existing.deadline_time:
            self.deadline_check.setChecked(True)
        self.deadline_check.toggled.connect(self.deadline_edit.setEnabled)
        form.addRow("", self.deadline_check)
        form.addRow("Deadline:", self.deadline_edit)

        # Priority
        self.priority_combo = __import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox()
        for p in ("low", "medium", "high"):
            self.priority_combo.addItem(p.capitalize(), p)
        if existing:
            self.priority_combo.setCurrentText(existing.priority.capitalize())
        form.addRow("Priority:", self.priority_combo)

        # Enabled
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(existing.enabled if existing else True)
        form.addRow("", self.enabled_check)

        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_cadence_change(self, text: str):
        cadence = text.lower()
        self._weekday_box.setVisible(cadence == "weekdays")
        self._dom_row.setVisible(cadence == "monthly")

    def collect(self) -> dict:
        title = self.title_field.text().strip()
        if not title:
            raise ValueError("Title is required.")
        cadence = self.cadence_combo.currentData()
        weekdays = [i for i, chk in enumerate(self.weekday_checks) if chk.isChecked()]
        time_of_day = self.time_edit.time().toString("HH:mm")
        deadline_time = (
            self.deadline_edit.time().toString("HH:mm") if self.deadline_check.isChecked() else None
        )
        day_of_month = self.dom_spin.value() if cadence == "monthly" else None

        if cadence == "weekdays":
            validate_time_of_day(time_of_day)
        if deadline_time:
            validate_time_of_day(deadline_time)

        data = {
            "title": title,
            "notes": self.notes_field.text().strip(),
            "cadence": cadence,
            "weekdays": weekdays if cadence == "weekdays" else [],
            "time_of_day": time_of_day,
            "day_of_month": day_of_month,
            "deadline_time": deadline_time,
            "priority": self.priority_combo.currentData(),
            "enabled": self.enabled_check.isChecked(),
        }
        return data


# ── Goals tab ──────────────────────────────────────────────────────────


class GoalsTab(QWidget):
    """Goal list with add/edit/check-in/achieve/reopen."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.goal_store = core.accountability["goals"]
        self.goal_engine = core.brain._goal_engine
        self.routine_store = core.accountability["routines"]
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_add = QToolButton()
        btn_add.setText("➕ Add Goal")
        btn_add.setToolTip("Add a new goal")
        btn_add.clicked.connect(self._add_goal)
        toolbar.addWidget(btn_add)
        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh goals")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        lay.addWidget(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Title",
                "Status",
                "Met",
                "Target Count",
                "Target Streak",
                "Current Streak",
                "Actions",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().resizeSection(1, 200)
        self.table.horizontalHeader().resizeSection(7, 200)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        self._empty = _empty_label("No goals yet. Click '➕ Add Goal' to create one.")
        lay.addWidget(self._empty)

        # Info label
        info = QLabel(
            "Linked routines: comma-separated routine IDs (validated against existing routines)."
        )
        info.setStyleSheet("color: #8d86a8; font-size: 11px;")
        lay.addWidget(info)

    def refresh(self):
        summaries = self.goal_engine.summaries()
        self.table.setRowCount(0)
        self._empty.setVisible(len(summaries) == 0)
        self.table.setVisible(len(summaries) > 0)

        for s in summaries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(s.goal_id)))
            self.table.setItem(row, 1, QTableWidgetItem(s.title))
            self.table.setItem(row, 2, QTableWidgetItem(f"{_status_icon(s.status)} {s.status}"))
            self.table.setItem(row, 3, QTableWidgetItem(str(s.count)))
            self.table.setItem(
                row, 4, QTableWidgetItem(str(s.target_count) if s.target_count else "—")
            )
            self.table.setItem(
                row, 5, QTableWidgetItem(str(s.target_streak) if s.target_streak else "—")
            )
            self.table.setItem(row, 6, QTableWidgetItem(f"{s.current_streak}/{s.longest_streak}"))

            # Actions
            actions = QWidget()
            actions_lay = QHBoxLayout(actions)
            actions_lay.setContentsMargins(2, 0, 2, 0)
            actions_lay.setSpacing(2)

            if s.status == "active":
                btn_checkin = QToolButton()
                btn_checkin.setText("📝")
                btn_checkin.setToolTip("Check-in (met/not met)")
                btn_checkin.clicked.connect(lambda _, gid=s.goal_id: self._check_in(gid))
                actions_lay.addWidget(btn_checkin)

                btn_achieve = QToolButton()
                btn_achieve.setText("✅")
                btn_achieve.setToolTip("Mark achieved")
                btn_achieve.clicked.connect(lambda _, gid=s.goal_id: self._achieve(gid))
                actions_lay.addWidget(btn_achieve)
            elif s.status == "achieved":
                btn_reopen = QToolButton()
                btn_reopen.setText("🔄")
                btn_reopen.setToolTip("Reopen goal")
                btn_reopen.clicked.connect(lambda _, gid=s.goal_id: self._reopen(gid))
                actions_lay.addWidget(btn_reopen)

            btn_edit = QToolButton()
            btn_edit.setText("✏️")
            btn_edit.setToolTip("Edit goal")
            btn_edit.clicked.connect(lambda _, gid=s.goal_id: self._edit_goal(gid))
            actions_lay.addWidget(btn_edit)

            actions_lay.addStretch()
            self.table.setCellWidget(row, 7, actions)

    def _check_in(self, goal_id: int):
        dialog = CheckInDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        met = dialog.met_check.isChecked()
        note = dialog.note_field.text().strip()
        try:
            self.goal_engine.check_in(goal_id, datetime.now().date(), met, note)
            self.refresh()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Error", str(e))

    def _achieve(self, goal_id: int):
        self.goal_engine.mark_achieved(goal_id)
        self.refresh()

    def _reopen(self, goal_id: int):
        self.goal_engine.reopen(goal_id)
        self.refresh()

    def _add_goal(self):
        dialog = AddGoalDialog(
            available_routines=self.routine_store.list_all(),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.collect()
            self.goal_store.add(**data)
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Validation", str(e))

    def _edit_goal(self, goal_id: int):
        goal = self.goal_store.get(goal_id)
        dialog = AddGoalDialog(
            existing=goal,
            available_routines=self.routine_store.list_all(),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.collect()
            self.goal_store.update(goal_id, **data)
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Validation", str(e))


class CheckInDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Goal Check-in")
        self.setMinimumWidth(300)
        self.setStyleSheet(_DASHBOARD_STYLE)

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.met_check = QCheckBox("Met today?")
        self.met_check.setChecked(True)
        form.addRow("", self.met_check)

        self.note_field = QLineEdit()
        self.note_field.setPlaceholderText("Optional note")
        form.addRow("Note:", self.note_field)

        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


class AddGoalDialog(QDialog):
    def __init__(
        self,
        existing: Goal | None = None,
        available_routines: list[Routine] | None = None,
        parent=None,
    ):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Edit Goal" if existing else "Add Goal")
        self.setMinimumWidth(420)
        self.setStyleSheet(_DASHBOARD_STYLE)
        self.available_routines = available_routines or []

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.title_field = QLineEdit(existing.title if existing else "")
        self.title_field.setPlaceholderText("e.g. Meditate 30 days in a row")
        form.addRow("Title *:", self.title_field)

        self.desc_field = QLineEdit(existing.description if existing else "")
        self.desc_field.setPlaceholderText("Short description")
        form.addRow("Description:", self.desc_field)

        self.condition_field = QLineEdit(existing.condition if existing else "")
        self.condition_field.setPlaceholderText("e.g. routine_completions.done.count >= target")
        form.addRow("Condition:", self.condition_field)

        self.count_check = QCheckBox("Target count (total met days)")
        self.count_check.setChecked(existing.target_count is not None if existing else False)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(existing.target_count or 30 if existing else 30)
        self.count_spin.setEnabled(self.count_check.isChecked())
        self.count_check.toggled.connect(self.count_spin.setEnabled)
        form.addRow("", self.count_check)
        form.addRow("Target count:", self.count_spin)

        self.streak_check = QCheckBox("Target streak (consecutive met days)")
        self.streak_check.setChecked(existing.target_streak is not None if existing else False)
        self.streak_spin = QSpinBox()
        self.streak_spin.setRange(1, 9999)
        self.streak_spin.setValue(existing.target_streak or 7 if existing else 7)
        self.streak_spin.setEnabled(self.streak_check.isChecked())
        self.streak_check.toggled.connect(self.streak_spin.setEnabled)
        form.addRow("", self.streak_check)
        form.addRow("Target streak:", self.streak_spin)

        # Linked routines
        linked_label = QLabel("Linked routines (comma-separated IDs, e.g. 1,3,5)")
        linked_label.setWordWrap(True)
        linked_hint = QLabel("Only active, non-archived routine IDs are accepted.")
        linked_hint.setStyleSheet("color: #8d86a8; font-size: 11px;")
        form.addRow("", linked_label)
        form.addRow("", linked_hint)

        self.routine_ids_field = QLineEdit(
            ",".join(str(r) for r in (existing.linked_routine_ids or [])) if existing else ""
        )
        self.routine_ids_field.setPlaceholderText("e.g. 1, 3, 5")
        form.addRow("Linked routine IDs:", self.routine_ids_field)

        # Show available routines
        if self.available_routines:
            avail_text = ", ".join(f"#{r.id} {r.title}" for r in self.available_routines)
            avail_label = QLabel(f"Available: {avail_text}")
            avail_label.setWordWrap(True)
            avail_label.setStyleSheet("color: #8d86a8; font-size: 11px;")
            form.addRow("", avail_label)

        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def collect(self) -> dict:
        title = self.title_field.text().strip()
        if not title:
            raise ValueError("Goal title is required.")

        linked_ids_str = self.routine_ids_field.text().strip()
        linked_ids = []
        valid_ids = {r.id for r in self.available_routines}
        if linked_ids_str:
            for part in linked_ids_str.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    rid = int(part)
                except ValueError:
                    raise ValueError(f"Invalid routine ID: {part!r}")
                if rid not in valid_ids:
                    raise ValueError(f"Routine #{rid} does not exist or is archived.")
                linked_ids.append(rid)

        data = {
            "title": title,
            "description": self.desc_field.text().strip(),
            "condition": self.condition_field.text().strip(),
            "target_count": self.count_spin.value() if self.count_check.isChecked() else None,
            "target_streak": self.streak_spin.value() if self.streak_check.isChecked() else None,
            "linked_routine_ids": linked_ids,
        }
        return data


# ── Rules tab ──────────────────────────────────────────────────────────


class RulesTab(QWidget):
    """Accountability rules: add/edit/toggle/delete."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.rule_store = core.accountability["rules"]
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        # Info banner
        info = QLabel(
            "Screen rules require opt-in screen awareness (enabled in Settings > Awareness). "
            "Time rules fire deterministically. Custom rules are reserved for future LLM evaluation."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "color: #8d86a8; font-size: 11px; padding: 4px; "
            "background: #2a2a1a; border: 1px solid #5a5a33; border-radius: 4px;"
        )
        lay.addWidget(info)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_add = QToolButton()
        btn_add.setText("➕ Add Rule")
        btn_add.setToolTip("Add a new accountability rule")
        btn_add.clicked.connect(self._add_rule)
        toolbar.addWidget(btn_add)
        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh rules")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        lay.addWidget(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Title",
                "Type",
                "Condition",
                "Message",
                "Enabled",
                "Actions",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().resizeSection(1, 160)
        self.table.horizontalHeader().resizeSection(3, 160)
        self.table.horizontalHeader().resizeSection(4, 180)
        self.table.horizontalHeader().resizeSection(6, 140)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        self._empty = _empty_label("No rules yet. Click '➕ Add Rule' to create one.")
        lay.addWidget(self._empty)

    def refresh(self):
        rules = self.rule_store.list_all()
        self.table.setRowCount(0)
        self._empty.setVisible(len(rules) == 0)
        self.table.setVisible(len(rules) > 0)

        for rule in rules:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(rule.id)))
            self.table.setItem(row, 1, QTableWidgetItem(rule.title))
            self.table.setItem(row, 2, QTableWidgetItem(rule.rule_type))
            self.table.setItem(row, 3, QTableWidgetItem(rule.condition))
            self.table.setItem(row, 4, QTableWidgetItem(rule.message))

            enabled_check = QCheckBox()
            enabled_check.setChecked(rule.enabled)
            enabled_check.toggled.connect(lambda _, rid=rule.id: self._toggle_rule(rid))
            self.table.setCellWidget(row, 5, enabled_check)

            actions = QWidget()
            actions_lay = QHBoxLayout(actions)
            actions_lay.setContentsMargins(2, 0, 2, 0)
            actions_lay.setSpacing(2)

            btn_edit = QToolButton()
            btn_edit.setText("✏️")
            btn_edit.setToolTip("Edit rule")
            btn_edit.clicked.connect(lambda _, rid=rule.id: self._edit_rule(rid))
            actions_lay.addWidget(btn_edit)

            btn_delete = QToolButton()
            btn_delete.setText("🗑️")
            btn_delete.setToolTip("Delete rule")
            btn_delete.setProperty("danger", True)
            btn_delete.clicked.connect(lambda _, rid=rule.id: self._delete_rule(rid))
            actions_lay.addWidget(btn_delete)

            actions_lay.addStretch()
            self.table.setCellWidget(row, 6, actions)

    def _toggle_rule(self, rule_id: int):
        self.rule_store.toggle(rule_id)
        self.refresh()

    def _delete_rule(self, rule_id: int):
        reply = QMessageBox.question(
            self,
            "Delete Rule",
            "Are you sure you want to delete this rule?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.rule_store.delete(rule_id)
            self.refresh()

    def _add_rule(self):
        dialog = AddRuleDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.collect()
            validate_add_rule(**data)
            self.rule_store.add(**data)
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Validation", str(e))

    def _edit_rule(self, rule_id: int):
        rule = self.rule_store.get(rule_id)
        dialog = AddRuleDialog(existing=rule, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dialog.collect()
            validate_update_rule(**data)
            self.rule_store.update(rule_id, **data)
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Validation", str(e))


class AddRuleDialog(QDialog):
    def __init__(self, existing: AccountabilityRule | None = None, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Edit Rule" if existing else "Add Rule")
        self.setMinimumWidth(400)
        self.setStyleSheet(_DASHBOARD_STYLE)
        self.existing = existing

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.title_field = QLineEdit(existing.title if existing else "")
        self.title_field.setPlaceholderText("e.g. No social media at night")
        form.addRow("Title *:", self.title_field)

        self.type_combo = __import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox()
        for t in ("time", "screen", "custom"):
            self.type_combo.addItem(t.capitalize(), t)
        if existing:
            self.type_combo.setCurrentText(existing.rule_type.capitalize())
        self.type_combo.currentIndexChanged.connect(self._on_type_change)
        form.addRow("Type:", self.type_combo)

        self.condition_field = QLineEdit(existing.condition if existing else "")
        self.condition_field.setPlaceholderText("e.g. after 10 PM")
        self._condition_hint = QLabel(
            'Time: "after HH:MM", "before HH:MM", "between X and Y"\n'
            "Screen: free-text (requires awareness opt-in)\n"
            "Custom: free-text (reserved for LLM)"
        )
        self._condition_hint.setWordWrap(True)
        self._condition_hint.setStyleSheet("color: #8d86a8; font-size: 11px;")
        form.addRow("Condition *:", self.condition_field)
        form.addRow("", self._condition_hint)

        self.message_field = QLineEdit(existing.message if existing else "")
        self.message_field.setPlaceholderText("Message shown when rule fires")
        form.addRow("Message:", self.message_field)

        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 1440)
        self.cooldown_spin.setSuffix(" min")
        self.cooldown_spin.setValue(existing.cooldown_minutes or 0 if existing else 0)
        form.addRow("Cooldown:", self.cooldown_spin)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(existing.enabled if existing else True)
        form.addRow("", self.enabled_check)

        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_type_change(self, index: int):
        rule_type = self.type_combo.currentData()
        if rule_type == "time":
            self.condition_field.setPlaceholderText(
                'e.g. "after 10 PM" or "between 9:00 and 17:00"'
            )
        elif rule_type == "screen":
            self.condition_field.setPlaceholderText("e.g. 'social media detected'")
        else:
            self.condition_field.setPlaceholderText("Free-text condition")

    def collect(self) -> dict:
        title = self.title_field.text().strip()
        condition = self.condition_field.text().strip()
        if not title:
            raise ValueError("Rule title is required.")
        if not condition:
            raise ValueError("Rule condition is required.")

        return {
            "title": title,
            "rule_type": self.type_combo.currentData(),
            "condition": condition,
            "message": self.message_field.text().strip(),
            "cooldown_minutes": self.cooldown_spin.value(),
            "enabled": self.enabled_check.isChecked(),
        }


# ── Activity tab ───────────────────────────────────────────────────────


class ActivityTab(QWidget):
    """Read-only activity log (last 200 entries)."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.activity_store = core.accountability["activity"]
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh activity log")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        count_label = QLabel("Last 200 entries (hard cap)")
        count_label.setStyleSheet("color: #8d86a8; font-size: 11px;")
        toolbar.addWidget(count_label)
        lay.addWidget(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Timestamp",
                "Actor",
                "Action",
                "Detail",
                "Ref",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().resizeSection(1, 140)
        self.table.horizontalHeader().resizeSection(3, 140)
        self.table.horizontalHeader().resizeSection(4, 260)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        self._empty = _empty_label("No activity recorded yet.")
        lay.addWidget(self._empty)

    def refresh(self):
        entries = self.activity_store.recent(limit=200)
        self.table.setRowCount(0)
        self._empty.setVisible(len(entries) == 0)
        self.table.setVisible(len(entries) > 0)

        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(entry.id)))
            self.table.setItem(row, 1, QTableWidgetItem(_fmt_dt(entry.at)))
            self.table.setItem(row, 2, QTableWidgetItem(entry.actor))
            self.table.setItem(row, 3, QTableWidgetItem(entry.action))
            self.table.setItem(row, 4, QTableWidgetItem(entry.detail))
            ref = f"{entry.ref_type} #{entry.ref_id}" if entry.ref_type and entry.ref_id else ""
            self.table.setItem(row, 5, QTableWidgetItem(ref))


# ── Observations tab ───────────────────────────────────────────────────


class ObservationsTab(QWidget):
    """Read-only structured screen observations (last 300 rows)."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.observation_store = core.accountability["observations"]
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh screen observations")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        count_label = QLabel("Last 300 rows")
        count_label.setStyleSheet("color: #8d86a8; font-size: 11px;")
        toolbar.addWidget(count_label)
        lay.addWidget(toolbar)

        self.summary = QLabel("today: no screen activity recorded")
        self.summary.setStyleSheet("color: #c9b7f5; padding: 2px 4px 6px;")
        lay.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            ["Start", "End", "Duration", "Source", "App", "Window", "Category", "Activity"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().resizeSection(0, 135)
        self.table.horizontalHeader().resizeSection(1, 135)
        self.table.horizontalHeader().resizeSection(4, 150)
        self.table.horizontalHeader().resizeSection(5, 240)
        self.table.horizontalHeader().resizeSection(7, 240)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        self._empty = _empty_label("No screen observations recorded yet.")
        lay.addWidget(self._empty)

    def refresh(self):
        entries = self.observation_store.recent(limit=300)
        self.table.setRowCount(0)
        self._empty.setVisible(len(entries) == 0)
        self.table.setVisible(len(entries) > 0)

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        totals = summarize_categories(self.observation_store.since(today, limit=5000))
        if totals:
            parts = [f"{category} {_fmt_minutes(minutes)}" for category, minutes in totals.items()]
            self.summary.setText("today: " + " · ".join(parts))
        else:
            self.summary.setText("today: no screen activity recorded")

        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                _fmt_dt(entry.started_at),
                _fmt_dt(entry.ended_at),
                _fmt_duration((entry.ended_at - entry.started_at).total_seconds()),
                entry.source,
                entry.app,
                entry.window_title,
                entry.category,
                entry.activity,
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))


# ── Token Usage tab ────────────────────────────────────────────────────


class TokenUsageTab(QWidget):
    """Token usage summaries and recent calls."""

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.token_store = core.accountability["token_usage"]
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        btn_refresh = QToolButton()
        btn_refresh.setText("🔄")
        btn_refresh.setToolTip("Refresh token usage")
        btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(btn_refresh)
        toolbar.addWidget(_toolbar_spacer())
        lay.addWidget(toolbar)

        # Summary group
        summary_box = QGroupBox("Summary by Lane")
        summary_lay = QHBoxLayout(summary_box)

        # Today
        self.today_table = QTableWidget()
        self.today_table.setColumnCount(5)
        self.today_table.setHorizontalHeaderLabels(
            ["Lane", "Prompt", "Completion", "Total", "Calls"]
        )
        self.today_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        today_header = QLabel("Today")
        today_header.setAlignment(Qt.AlignCenter)
        today_header.setStyleSheet("font-weight: 600; color: #c9b7f5;")
        today_wrapper = QWidget()
        today_wrapper_lay = QVBoxLayout(today_wrapper)
        today_wrapper_lay.setContentsMargins(0, 0, 0, 0)
        today_wrapper_lay.addWidget(today_header)
        today_wrapper_lay.addWidget(self.today_table)
        summary_lay.addWidget(today_wrapper)

        # 7 days
        self.week_table = QTableWidget()
        self.week_table.setColumnCount(5)
        self.week_table.setHorizontalHeaderLabels(
            ["Lane", "Prompt", "Completion", "Total", "Calls"]
        )
        self.week_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        week_header = QLabel("Last 7 Days")
        week_header.setAlignment(Qt.AlignCenter)
        week_header.setStyleSheet("font-weight: 600; color: #c9b7f5;")
        week_wrapper = QWidget()
        week_wrapper_lay = QVBoxLayout(week_wrapper)
        week_wrapper_lay.setContentsMargins(0, 0, 0, 0)
        week_wrapper_lay.addWidget(week_header)
        week_wrapper_lay.addWidget(self.week_table)
        summary_lay.addWidget(week_wrapper)

        # All time
        self.all_table = QTableWidget()
        self.all_table.setColumnCount(5)
        self.all_table.setHorizontalHeaderLabels(["Lane", "Prompt", "Completion", "Total", "Calls"])
        self.all_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        all_header = QLabel("All Time")
        all_header.setAlignment(Qt.AlignCenter)
        all_header.setStyleSheet("font-weight: 600; color: #c9b7f5;")
        all_wrapper = QWidget()
        all_wrapper_lay = QVBoxLayout(all_wrapper)
        all_wrapper_lay.setContentsMargins(0, 0, 0, 0)
        all_wrapper_lay.addWidget(all_header)
        all_wrapper_lay.addWidget(self.all_table)
        summary_lay.addWidget(all_wrapper)

        summary_box.setLayout(summary_lay)
        lay.addWidget(summary_box)

        # Recent calls
        recent_box = QGroupBox("Recent API Calls")
        recent_lay = QVBoxLayout(recent_box)
        recent_lay.setContentsMargins(0, 0, 0, 0)

        self.recent_table = QTableWidget()
        self.recent_table.setColumnCount(8)
        self.recent_table.setHorizontalHeaderLabels(
            [
                "Timestamp",
                "Lane",
                "Purpose",
                "Provider",
                "Model",
                "Prompt",
                "Completion",
                "Exact?",
            ]
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.recent_table.horizontalHeader().resizeSection(0, 140)
        self.recent_table.horizontalHeader().resizeSection(3, 100)
        self.recent_table.horizontalHeader().resizeSection(4, 140)
        self.recent_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.recent_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.recent_table.setAlternatingRowColors(True)
        recent_lay.addWidget(self.recent_table)

        self._empty_recent = _empty_label("No token usage recorded yet.")
        recent_lay.addWidget(self._empty_recent)

        lay.addWidget(recent_box)

    def _fill_summary_table(self, table: QTableWidget, period: str):
        data = self.token_store.summary(period=period)
        table.setRowCount(0)
        for row_data in data:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(row_data["lane"]))
            table.setItem(row, 1, QTableWidgetItem(str(row_data["prompt_tokens"])))
            table.setItem(row, 2, QTableWidgetItem(str(row_data["completion_tokens"])))
            table.setItem(row, 3, QTableWidgetItem(str(row_data["total_tokens"])))
            table.setItem(row, 4, QTableWidgetItem(str(row_data["count"])))

    def refresh(self):
        self._fill_summary_table(self.today_table, "today")
        self._fill_summary_table(self.week_table, "7d")
        self._fill_summary_table(self.all_table, "all")

        recent = self.token_store.recent(limit=100)
        self.recent_table.setRowCount(0)
        self._empty_recent.setVisible(len(recent) == 0)
        self.recent_table.setVisible(len(recent) > 0)

        for entry in recent:
            row = self.recent_table.rowCount()
            self.recent_table.insertRow(row)
            self.recent_table.setItem(row, 0, QTableWidgetItem(_fmt_dt(entry.at)))
            self.recent_table.setItem(row, 1, QTableWidgetItem(entry.lane))
            self.recent_table.setItem(row, 2, QTableWidgetItem(entry.purpose))
            self.recent_table.setItem(row, 3, QTableWidgetItem(entry.provider))
            self.recent_table.setItem(row, 4, QTableWidgetItem(entry.model))
            self.recent_table.setItem(row, 5, QTableWidgetItem(str(entry.prompt_tokens)))
            self.recent_table.setItem(row, 6, QTableWidgetItem(str(entry.completion_tokens)))
            exact = "yes" if not entry.estimated else "estimated"
            self.recent_table.setItem(row, 7, QTableWidgetItem(exact))


# ── Main Dashboard Window ──────────────────────────────────────────────


class DashboardWindow(QDialog):
    """Main dashboard window with tabbed interface.

    Takes a Core instance and wires all tabs to the shared stores/engines.
    Can be shown/hidden/reopened safely — each tab refreshes on show.
    """

    def __init__(self, core, parent=None):
        super().__init__(parent, Qt.Window)
        self.core = core
        self.setWindowTitle("clipponyai — Planner & Activity")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)
        self.setStyleSheet(_DASHBOARD_STYLE)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)

        # Top toolbar with global refresh
        top_bar = QToolBar()
        top_bar.setMovable(False)
        refresh_btn = QToolButton()
        refresh_btn.setText("🔄 Refresh All")
        refresh_btn.setToolTip("Refresh all tabs")
        refresh_btn.clicked.connect(self.refresh_all)
        top_bar.addWidget(refresh_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top_bar.addWidget(spacer)
        close_btn = QToolButton()
        close_btn.setText("✖")
        close_btn.setToolTip("Close dashboard")
        close_btn.clicked.connect(self.close)
        top_bar.addWidget(close_btn)
        main_lay.addWidget(top_bar)

        # Tab widget
        self.tabs = QTabWidget()

        self._tasks_tab = TasksTab(core)
        self.tabs.addTab(self._tasks_tab, "📋 Tasks")

        self._routines_tab = RoutinesTab(core)
        self.tabs.addTab(self._routines_tab, "🔁 Routines")

        self._goals_tab = GoalsTab(core)
        self.tabs.addTab(self._goals_tab, "🎯 Goals")

        self._rules_tab = RulesTab(core)
        self.tabs.addTab(self._rules_tab, "📏 Rules")

        self._activity_tab = ActivityTab(core)
        self.tabs.addTab(self._activity_tab, "📜 Activity")

        self._observations_tab = ObservationsTab(core)
        self.tabs.addTab(self._observations_tab, "👀 Observations")

        self._token_tab = TokenUsageTab(core)
        self.tabs.addTab(self._token_tab, "📊 Token Usage")

        self.tabs.currentChanged.connect(self._on_tab_changed)
        main_lay.addWidget(self.tabs)

        # Keyboard shortcut: Ctrl+R to refresh
        from PySide6.QtGui import QShortcut

        shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        shortcut.activated.connect(self.refresh_all)

    def _on_tab_changed(self, index: int):
        """Refresh the newly selected tab."""
        tab = self.tabs.widget(index)
        if hasattr(tab, "refresh"):
            tab.refresh()

    def refresh_all(self):
        """Refresh all tabs."""
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if hasattr(tab, "refresh"):
                tab.refresh()

    def show_tasks_tab(self):
        """Show the dashboard and switch to the Tasks tab."""
        self._tasks_tab.refresh()
        self.tabs.setCurrentIndex(0)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_tab(self, tab_name: str):
        """Show the dashboard and switch to a specific tab by name."""
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).lower().replace(" ", "") == tab_name.lower().replace(" ", ""):
                self.tabs.setCurrentIndex(i)
                break
        tab = self.tabs.widget(self.tabs.currentIndex())
        if hasattr(tab, "refresh"):
            tab.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        """Hide instead of destroy so reopening is fast and reflects DB."""
        event.ignore()
        self.hide()
