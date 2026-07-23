"""Headless tests for the PySide6 dashboard window and its tabs.

Runs with QT_QPA_PLATFORM=offscreen.  Tests construction, tab presence,
adding entities via public methods, and action side-effects.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication

from clipponyai.accountability import get_stores
from clipponyai.tasks import TaskStore


@pytest.fixture
def core(tmp_path):
    """Build a minimal Core-like object with all accountability stores."""
    store = TaskStore(tmp_path / "test.db")
    store._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            cadence TEXT NOT NULL DEFAULT 'daily',
            weekdays TEXT NOT NULL DEFAULT '[]',
            time_of_day TEXT,
            day_of_month INTEGER,
            deadline_time TEXT,
            priority TEXT NOT NULL DEFAULT 'medium',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS routine_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            occurrence_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            at TEXT NOT NULL,
            task_id INTEGER,
            UNIQUE(routine_id, occurrence_date)
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            condition TEXT NOT NULL DEFAULT '',
            target_count INTEGER,
            target_streak INTEGER,
            linked_routine_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            achieved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS goal_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            met INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE(goal_id, date)
        );
        CREATE TABLE IF NOT EXISTS accountability_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            rule_type TEXT NOT NULL DEFAULT 'custom',
            condition TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            cooldown_minutes INTEGER NOT NULL DEFAULT 0,
            last_fired_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            ref_type TEXT,
            ref_id TEXT
        );
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            lane TEXT NOT NULL DEFAULT 'chat',
            purpose TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    store._conn.commit()
    acct = get_stores(store)

    # Build a minimal Core mock
    class _MockBrain:
        def __init__(self):
            self._routine_engine = None
            self._goal_engine = None

    from clipponyai.routines import RoutineEngine
    from clipponyai.goals import GoalEngine

    brain = _MockBrain()

    async def _noop(msg):
        pass

    brain._routine_engine = RoutineEngine(
        acct["routines"], acct["routine_completions"],
        store, _noop, acct["activity"],
    )
    brain._goal_engine = GoalEngine(
        acct["goals"], acct["goal_progress"],
        acct["routines"], acct["routine_completions"],
        acct["activity"],
    )

    # Build a minimal Core-like object
    class _Core:
        pass

    core_obj = _Core()
    core_obj.store = store
    core_obj.accountability = acct
    core_obj.brain = brain
    yield core_obj
    store.close()


def test_dashboard_constructs_with_all_tabs(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import DashboardWindow

    dash = DashboardWindow(core)
    assert dash.windowTitle() == "clipponyai — Planner & Activity"
    assert dash.tabs.count() == 6

    expected = ["Tasks", "Routines", "Goals", "Rules", "Activity", "Token Usage"]
    for i, name in enumerate(expected):
        assert name in dash.tabs.tabText(i), f"Tab {i}: {dash.tabs.tabText(i)}"

    dash.close()
    app.processEvents()


def test_tasks_tab_populated(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import DashboardWindow

    # Seed a task
    core.store.add("Test task from tests", deadline=datetime(2099, 1, 1, 12, 0))

    dash = DashboardWindow(core)
    tasks_tab = dash.tabs.widget(0)
    tasks_tab.refresh()
    app.processEvents()

    # Tree should have at least one top-level section
    assert tasks_tab.tree.topLevelItemCount() > 0
    dash.close()
    app.processEvents()


def test_tasks_add_and_complete(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import TasksTab

    tab = TasksTab(core)
    tab.refresh()
    app.processEvents()

    # Add via store directly (public API)
    task, created = core.store.add("Dashboard test task")
    assert created
    tab.refresh()
    app.processEvents()

    # Complete via store
    core.store.complete(task, actor="test")
    tab.refresh()
    app.processEvents()

    # Should show in completed section
    found = False
    for i in range(tab.tree.topLevelItemCount()):
        item = tab.tree.topLevelItem(i)
        if "Completed" in item.text(0):
            found = True
            assert item.childCount() >= 1
    assert found, "Completed section not found"


def test_routines_tab_empty_state(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import RoutinesTab

    tab = RoutinesTab(core)
    tab.refresh()
    app.processEvents()

    assert not tab._empty.isHidden()
    assert tab.table.isHidden()


def test_routines_add_via_store(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import RoutinesTab

    tab = RoutinesTab(core)

    # Add via store
    core.accountability["routines"].add(
        "Morning stretch",
        cadence="daily",
        time_of_day="08:00",
    )
    tab.refresh()
    app.processEvents()

    assert not tab._empty.isVisible()
    assert tab.table.rowCount() >= 1
    assert tab.table.item(0, 1).text() == "Morning stretch"


def test_routines_toggle_and_complete(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import RoutinesTab

    routine = core.accountability["routines"].add(
        "Toggle test",
        cadence="daily",
        time_of_day="10:00",
    )
    tab = RoutinesTab(core)
    tab.refresh()
    app.processEvents()

    # Toggle off
    core.accountability["routines"].toggle(routine.id)
    tab.refresh()
    app.processEvents()

    # Complete today via engine
    core.brain._routine_engine.complete_today(routine.id, datetime.now())
    tab.refresh()
    app.processEvents()


def test_goals_tab_empty(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import GoalsTab

    tab = GoalsTab(core)
    tab.refresh()
    app.processEvents()

    assert not tab._empty.isHidden()


def test_goals_add_and_checkin(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import GoalsTab

    tab = GoalsTab(core)

    # Add goal via store
    goal = core.accountability["goals"].add(
        "Test goal",
        description="For testing",
        target_count=5,
    )
    tab.refresh()
    app.processEvents()

    assert tab.table.rowCount() >= 1

    # Check-in via engine
    core.brain._goal_engine.check_in(goal.id, datetime.now().date(), met=True, note="test")
    tab.refresh()
    app.processEvents()

    # Summary should show count=1
    summaries = core.brain._goal_engine.summaries()
    assert any(s.goal_id == goal.id and s.count == 1 for s in summaries)


def test_goals_link_routine_validation(core):
    """Linked routine IDs are validated against existing routines."""
    QApplication.instance() or QApplication([])
    from clipponyai.dashboard import AddGoalDialog

    # Create a routine first
    routine = core.accountability["routines"].add("Link test routine", cadence="daily")

    dialog = AddGoalDialog(
        available_routines=core.accountability["routines"].list_all(),
        parent=None,
    )
    dialog.title_field.setText("Linked goal")
    dialog.routine_ids_field.setText(str(routine.id))
    data = dialog.collect()
    assert data["linked_routine_ids"] == [routine.id]

    # Invalid ID should raise
    dialog.routine_ids_field.setText("99999")
    with pytest.raises(ValueError, match="does not exist"):
        dialog.collect()

    # Non-integer should raise
    dialog.routine_ids_field.setText("abc")
    with pytest.raises(ValueError, match="Invalid routine ID"):
        dialog.collect()


def test_rules_add_and_toggle(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import RulesTab

    tab = RulesTab(core)
    tab.refresh()
    app.processEvents()

    # Add via store
    rule = core.accountability["rules"].add(
        "Night rule",
        rule_type="time",
        condition="after 22:00",
        message="Time to rest!",
        cooldown_minutes=60,
    )
    tab.refresh()
    app.processEvents()

    assert tab.table.rowCount() >= 1
    assert tab.table.item(0, 1).text() == "Night rule"

    # Toggle via store
    core.accountability["rules"].toggle(rule.id)
    tab.refresh()
    app.processEvents()


def test_rules_delete(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import RulesTab

    rule = core.accountability["rules"].add(
        "Delete me",
        rule_type="custom",
        condition="test",
    )
    tab = RulesTab(core)
    tab.refresh()
    app.processEvents()

    assert tab.table.rowCount() >= 1
    core.accountability["rules"].delete(rule.id)
    tab.refresh()
    app.processEvents()
    assert tab.table.rowCount() == 0


def test_rules_validation_helpers(core):
    """Test validation helpers used by the Rules tab."""
    from clipponyai.rules import validate_add_rule, validate_update_rule

    # Valid
    validate_add_rule("Test", rule_type="time", condition="after 10 PM", cooldown_minutes=30)

    # Empty title
    with pytest.raises(ValueError, match="title"):
        validate_add_rule("", rule_type="time", condition="after 10 PM")

    # Empty condition
    with pytest.raises(ValueError, match="condition"):
        validate_add_rule("Test", rule_type="time", condition="")

    # Invalid type
    with pytest.raises(ValueError, match="rule_type"):
        validate_add_rule("Test", rule_type="invalid", condition="x")

    # Negative cooldown
    with pytest.raises(ValueError, match="cooldown"):
        validate_add_rule("Test", rule_type="time", condition="x", cooldown_minutes=-1)

    # Update with partial fields
    validate_update_rule(title="Updated")
    validate_update_rule(cooldown_minutes=0)


def test_activity_tab_read_only(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import ActivityTab

    tab = ActivityTab(core)

    # Seed some activity
    core.accountability["activity"].record("test_action", actor="test", detail="hello")
    core.accountability["activity"].record("another_action", actor="test", detail="world")

    tab.refresh()
    app.processEvents()

    assert tab.table.rowCount() >= 2
    assert not tab._empty.isVisible()


def test_activity_cap_visible(core):
    """Activity tab shows the 200-entry cap label."""
    QApplication.instance() or QApplication([])
    from clipponyai.dashboard import ActivityTab

    tab = ActivityTab(core)
    # The toolbar should contain a label mentioning the cap
    found_cap_label = False
    for child in tab.findChildren(type(tab._empty)):  # QLabel
        text = child.text()
        if "200" in text:
            found_cap_label = True
            break
    assert found_cap_label, "Cap label not found in activity tab"


def test_token_usage_tab_empty(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import TokenUsageTab

    tab = TokenUsageTab(core)
    tab.refresh()
    app.processEvents()

    assert not tab._empty_recent.isHidden()


def test_token_usage_summary(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import TokenUsageTab

    tab = TokenUsageTab(core)

    # Seed token usage
    core.accountability["token_usage"].record(
        lane="chat",
        purpose="conversation",
        provider="openai",
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        estimated=False,
    )
    core.accountability["token_usage"].record(
        lane="sensor",
        purpose="screen_analysis",
        provider="openai",
        model="gpt-4o",
        prompt_tokens=200,
        completion_tokens=30,
        estimated=True,
    )

    tab.refresh()
    app.processEvents()

    # Summary tables should have data
    assert tab.today_table.rowCount() >= 1
    assert tab.week_table.rowCount() >= 1
    assert tab.all_table.rowCount() >= 1

    # Recent table should show entries
    assert tab.recent_table.rowCount() >= 2
    assert not tab._empty_recent.isVisible()

    # Check exact vs estimated column
    for row in range(tab.recent_table.rowCount()):
        exact_text = tab.recent_table.item(row, 7).text()
        assert exact_text in ("yes", "estimated")


def test_dashboard_refresh_all(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import DashboardWindow

    dash = DashboardWindow(core)
    # refresh_all should not crash
    dash.refresh_all()
    app.processEvents()

    dash.close()
    app.processEvents()


def test_dashboard_show_tasks_tab(core):
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import DashboardWindow

    dash = DashboardWindow(core)
    dash.show_tasks_tab()
    app.processEvents()

    assert dash.tabs.currentIndex() == 0
    dash.close()
    app.processEvents()


def test_dashboard_hide_on_close(core):
    """Dashboard closeEvent hides instead of destroys."""
    app = QApplication.instance() or QApplication([])
    from clipponyai.dashboard import DashboardWindow

    dash = DashboardWindow(core)
    dash.show()
    app.processEvents()

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    dash.closeEvent(event)
    app.processEvents()

    assert not event.isAccepted()
    assert not dash.isVisible()
    # Can still show again
    dash.show()
    assert dash.isVisible()
    dash.close()
    app.processEvents()


def test_routines_add_dialog_validation():
    """AddRoutineDialog validates required fields."""
    QApplication.instance() or QApplication([])
    from clipponyai.dashboard import AddRoutineDialog

    dialog = AddRoutineDialog()
    dialog.title_field.setText("")
    with pytest.raises(ValueError, match="Title"):
        dialog.collect()

    dialog.title_field.setText("Valid routine")
    data = dialog.collect()
    assert data["title"] == "Valid routine"
    assert data["cadence"] in ("daily", "weekdays", "monthly")


def test_add_rule_dialog_validation():
    """AddRuleDialog validates required fields."""
    QApplication.instance() or QApplication([])
    from clipponyai.dashboard import AddRuleDialog

    dialog = AddRuleDialog()
    dialog.title_field.setText("")
    with pytest.raises(ValueError, match="title"):
        dialog.collect()

    dialog.title_field.setText("Test rule")
    dialog.condition_field.setText("")
    with pytest.raises(ValueError, match="condition"):
        dialog.collect()


def test_add_goal_dialog_validation():
    """AddGoalDialog validates required fields."""
    QApplication.instance() or QApplication([])
    from clipponyai.dashboard import AddGoalDialog

    dialog = AddGoalDialog()
    dialog.title_field.setText("")
    with pytest.raises(ValueError, match="title"):
        dialog.collect()

    dialog.title_field.setText("Test goal")
    data = dialog.collect()
    assert data["title"] == "Test goal"
    assert data["linked_routine_ids"] == []
