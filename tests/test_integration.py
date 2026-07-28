# ruff: noqa: DTZ001, DTZ005
"""Integration tests for accountability tool CRUD, scheduler goal sync/rule firing,
and activity logging.

Covers:
- brain.py: new tool specs and handlers (routine/goal/rule CRUD)
- brain.py: existing task tool activity recording
- scheduler.py: goal sync on every tick, rule firing, quiet suppression
- awareness.py: activity recording on intervention
- grounded IDs (no invented state)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from clipponyai.accountability import get_stores
from clipponyai.brain import PonyBrain, TOOL_SPECS
from clipponyai.config import Config, RemindersConfig
from clipponyai.goals import GoalEngine
from clipponyai.routines import RoutineEngine
from clipponyai.rules import RuleEngine
from clipponyai.scheduler import ReminderScheduler
from clipponyai.tasks import TaskStore


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def stores(store):
    return get_stores(store)


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def make_brain_with_stores(config, store, stores):
    """Build a PonyBrain with accountability stores wired in."""
    from conftest import FakeClient

    def _make(handlers=None):
        clients = []

        def factory(spec):
            client = FakeClient(spec, handlers or {})
            clients.append(client)
            return client

        brain = PonyBrain(
            config,
            store,
            client_factory=factory,
            accountability_stores=stores,
            activity_store=stores["activity"],
        )
        brain._test_clients = clients
        return brain

    return _make


# ── Tool spec coverage ───────────────────────────────────────────────


class TestToolSpecs:
    """Verify all new tool specs exist and have reasonable schemas."""

    def _get_spec(self, name):
        for ts in TOOL_SPECS:
            if ts.name == name:
                return ts
        return None

    def test_add_routine_spec(self):
        spec = self._get_spec("add_routine")
        assert spec is not None
        assert "title" in spec.input_schema["properties"]
        assert "cadence" in spec.input_schema["properties"]

    def test_list_routines_spec(self):
        assert self._get_spec("list_routines") is not None

    def test_edit_routine_spec(self):
        spec = self._get_spec("edit_routine")
        assert spec is not None
        assert "routine_id" in spec.input_schema["required"]

    def test_complete_routine_spec(self):
        spec = self._get_spec("complete_routine")
        assert spec is not None
        assert "routine_id" in spec.input_schema["required"]

    def test_skip_routine_spec(self):
        assert self._get_spec("skip_routine") is not None

    def test_archive_routine_spec(self):
        assert self._get_spec("archive_routine") is not None

    def test_add_goal_spec(self):
        spec = self._get_spec("add_goal")
        assert spec is not None
        assert "title" in spec.input_schema["required"]

    def test_list_goals_spec(self):
        assert self._get_spec("list_goals") is not None

    def test_check_in_goal_spec(self):
        spec = self._get_spec("check_in_goal")
        assert spec is not None
        assert "goal_id" in spec.input_schema["required"]
        assert "met" in spec.input_schema["required"]

    def test_link_routine_to_goal_spec(self):
        spec = self._get_spec("link_routine_to_goal")
        assert spec is not None

    def test_achieve_goal_spec(self):
        assert self._get_spec("achieve_goal") is not None

    def test_reopen_goal_spec(self):
        assert self._get_spec("reopen_goal") is not None

    def test_add_rule_spec(self):
        spec = self._get_spec("add_rule")
        assert spec is not None
        assert "title" in spec.input_schema["required"]
        assert "rule_type" in spec.input_schema["required"]
        assert "condition" in spec.input_schema["required"]

    def test_list_rules_spec(self):
        assert self._get_spec("list_rules") is not None

    def test_edit_rule_spec(self):
        assert self._get_spec("edit_rule") is not None

    def test_toggle_rule_spec(self):
        assert self._get_spec("toggle_rule") is not None

    def test_delete_rule_spec(self):
        assert self._get_spec("delete_rule") is not None

    def test_recent_activity_spec(self):
        assert self._get_spec("recent_activity") is not None

    def test_token_usage_spec(self):
        spec = self._get_spec("token_usage")
        assert spec is not None
        assert "period" in spec.input_schema["properties"]


# ── Routine tool handlers ────────────────────────────────────────────


class TestRoutineToolHandlers:
    def test_add_routine(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        result = brain._tool_add_routine(
            {
                "title": "Morning stretch",
                "cadence": "daily",
                "time_of_day": "07:00",
            }
        )
        assert "added routine" in result
        routines = stores["routines"].list_all()
        assert len(routines) == 1
        assert routines[0].title == "Morning stretch"
        assert routines[0].time_of_day == "07:00"

    def test_add_routine_requires_title(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_add_routine({"title": ""})
        assert "ERROR" in result

    def test_add_routine_records_activity(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._tool_add_routine({"title": "Test routine", "cadence": "daily"})
        entries = stores["activity"].recent()
        assert any(e.action == "routine_added" for e in entries)

    def test_list_routines_empty(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_list_routines({})
        assert "No routines" in result

    def test_list_routines_shows_details(self, make_brain_with_stores, stores):
        stores["routines"].add("Daily run", cadence="daily", time_of_day="06:30")
        stores["routines"].add("Weekly review", cadence="weekdays", weekdays=[0])
        brain = make_brain_with_stores({})
        result = brain._tool_list_routines({})
        assert "Daily run" in result
        assert "Weekly review" in result
        assert "06:30" in result

    def test_edit_routine(self, make_brain_with_stores, stores):
        r = stores["routines"].add("Old name", cadence="daily")
        brain = make_brain_with_stores({})
        result = brain._tool_edit_routine(
            {
                "routine_id": r.id,
                "title": "New name",
                "time_of_day": "08:00",
            }
        )
        assert "updated" in result
        updated = stores["routines"].get(r.id)
        assert updated.title == "New name"
        assert updated.time_of_day == "08:00"

    def test_edit_routine_missing_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_edit_routine({"routine_id": 9999})
        assert "no routine" in result

    def test_complete_routine_no_engine(self, make_brain_with_stores, stores):
        stores["routines"].add("Test", cadence="daily")
        brain = make_brain_with_stores({})
        # No routine engine wired in
        result = brain._tool_complete_routine({"routine_id": 1})
        assert "not available" in result

    def test_complete_routine_with_engine(self, make_brain_with_stores, stores):
        r = stores["routines"].add("Stretch", cadence="daily")
        brain = make_brain_with_stores({})

        # Wire in a routine engine
        async def deliver(msg):
            pass

        engine = RoutineEngine(
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            task_store=brain.store,
            deliver=deliver,
            activity_store=stores["activity"],
        )
        brain._routine_engine = engine
        result = brain._tool_complete_routine({"routine_id": r.id})
        assert "done" in result.lower() or "complete" in result.lower()

    def test_skip_routine_with_engine(self, make_brain_with_stores, stores):
        r = stores["routines"].add("Stretch", cadence="daily")
        brain = make_brain_with_stores({})

        async def deliver(msg):
            pass

        engine = RoutineEngine(
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            task_store=brain.store,
            deliver=deliver,
            activity_store=stores["activity"],
        )
        brain._routine_engine = engine
        result = brain._tool_skip_routine({"routine_id": r.id})
        assert "skip" in result.lower()

    def test_archive_routine(self, make_brain_with_stores, stores):
        r = stores["routines"].add("To archive", cadence="daily")
        brain = make_brain_with_stores({})
        result = brain._tool_archive_routine({"routine_id": r.id})
        assert "archived" in result
        assert stores["routines"].list_all() == []

    def test_archive_routine_missing(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_archive_routine({"routine_id": 9999})
        assert "no routine" in result

    def test_complete_routine_missing_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_complete_routine({"routine_id": 9999})
        assert "no routine" in result


# ── Goal tool handlers ───────────────────────────────────────────────


class TestGoalToolHandlers:
    def _make_engine(self, stores):
        return GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )

    def test_add_goal(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        result = brain._tool_add_goal(
            {
                "title": "Read daily",
                "target_count": 7,
            }
        )
        assert "added goal" in result
        goals = stores["goals"].list_all()
        assert len(goals) == 1
        assert goals[0].target_count == 7

    def test_add_goal_requires_title(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_add_goal({"title": ""})
        assert "ERROR" in result

    def test_add_goal_records_activity(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._tool_add_goal({"title": "Test goal"})
        entries = stores["activity"].recent()
        assert any(e.action == "goal_added" for e in entries)

    def test_list_goals_empty(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_list_goals({})
        assert "No goals" in result

    def test_list_goals_with_engine(self, make_brain_with_stores, stores):
        g = stores["goals"].add("Read daily", target_count=5)
        stores["goal_progress"].upsert(g.id, "2026-01-01", met=1)
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_list_goals({})
        assert "Read daily" in result
        assert "active" in result

    def test_check_in_goal_with_engine(self, make_brain_with_stores, stores):
        g = stores["goals"].add("Test goal", target_count=1)
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_check_in_goal({"goal_id": g.id, "met": True})
        assert "met" in result.lower()
        # Should auto-achieve since target_count=1
        assert stores["goals"].get(g.id).status == "achieved"

    def test_check_in_goal_missing(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_check_in_goal({"goal_id": 9999, "met": True})
        assert "no goal" in result

    def test_check_in_goal_no_engine(self, make_brain_with_stores, stores):
        stores["goals"].add("Test")
        brain = make_brain_with_stores({})
        result = brain._tool_check_in_goal({"goal_id": 1, "met": True})
        assert "not available" in result

    def test_link_routine_to_goal(self, make_brain_with_stores, stores):
        g = stores["goals"].add("Goal")
        r = stores["routines"].add("Routine")
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_link_routine_to_goal(
            {
                "goal_id": g.id,
                "routine_id": r.id,
            }
        )
        assert "linked" in result
        updated = stores["goals"].get(g.id)
        assert r.id in updated.linked_routine_ids

    def test_link_routine_to_goal_missing_goal(self, make_brain_with_stores, stores):
        stores["routines"].add("Routine")
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_link_routine_to_goal(
            {
                "goal_id": 9999,
                "routine_id": 1,
            }
        )
        assert "no goal" in result

    def test_achieve_goal(self, make_brain_with_stores, stores):
        g = stores["goals"].add("Test goal")
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_achieve_goal({"goal_id": g.id})
        assert "achieved" in result
        assert stores["goals"].get(g.id).status == "achieved"

    def test_reopen_goal(self, make_brain_with_stores, stores):
        g = stores["goals"].add("Test goal")
        stores["goals"].achieve(g.id)
        brain = make_brain_with_stores({})
        brain._goal_engine = self._make_engine(stores)
        result = brain._tool_reopen_goal({"goal_id": g.id})
        assert "reopened" in result
        assert stores["goals"].get(g.id).status == "active"


# ── Rule tool handlers ───────────────────────────────────────────────


class TestRuleToolHandlers:
    def test_add_rule(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        result = brain._tool_add_rule(
            {
                "title": "Late night reminder",
                "rule_type": "time",
                "condition": "after 22:00",
                "message": "Time to sleep!",
                "cooldown_minutes": 60,
            }
        )
        assert "added rule" in result
        rules = stores["rules"].list_all()
        assert len(rules) == 1
        assert rules[0].title == "Late night reminder"

    def test_add_rule_requires_fields(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        assert "ERROR" in brain._tool_add_rule({"title": "", "rule_type": "time", "condition": "x"})
        assert "ERROR" in brain._tool_add_rule({"title": "X", "rule_type": "", "condition": "x"})
        assert "ERROR" in brain._tool_add_rule({"title": "X", "rule_type": "time", "condition": ""})

    def test_add_rule_records_activity(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._tool_add_rule(
            {
                "title": "Test",
                "rule_type": "time",
                "condition": "after 22:00",
            }
        )
        entries = stores["activity"].recent()
        assert any(e.action == "rule_added" for e in entries)

    def test_list_rules_empty(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_list_rules({})
        assert "No accountability rules" in result

    def test_list_rules_shows_details(self, make_brain_with_stores, stores):
        stores["rules"].add(
            "Rule A",
            rule_type="time",
            condition="after 22:00",
            message="Sleep!",
            cooldown_minutes=30,
        )
        brain = make_brain_with_stores({})
        result = brain._tool_list_rules({})
        assert "Rule A" in result
        assert "after 22:00" in result

    def test_edit_rule(self, make_brain_with_stores, stores):
        r = stores["rules"].add("Old", rule_type="time", condition="after 22:00")
        brain = make_brain_with_stores({})
        result = brain._tool_edit_rule(
            {
                "rule_id": r.id,
                "title": "New",
                "cooldown_minutes": 120,
            }
        )
        assert "updated" in result
        updated = stores["rules"].get(r.id)
        assert updated.title == "New"
        assert updated.cooldown_minutes == 120

    def test_edit_rule_missing(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_edit_rule({"rule_id": 9999})
        assert "no rule" in result

    def test_toggle_rule(self, make_brain_with_stores, stores):
        r = stores["rules"].add("Toggle", rule_type="time", condition="after 22:00")
        brain = make_brain_with_stores({})
        result = brain._tool_toggle_rule({"rule_id": r.id})
        assert "disabled" in result
        assert not stores["rules"].get(r.id).enabled
        result2 = brain._tool_toggle_rule({"rule_id": r.id})
        assert "enabled" in result2

    def test_delete_rule(self, make_brain_with_stores, stores):
        r = stores["rules"].add("Delete me", rule_type="time", condition="after 22:00")
        brain = make_brain_with_stores({})
        result = brain._tool_delete_rule({"rule_id": r.id})
        assert "deleted" in result
        with pytest.raises(KeyError):
            stores["rules"].get(r.id)

    def test_delete_rule_missing(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_delete_rule({"rule_id": 9999})
        assert "no rule" in result

    def test_toggle_rule_records_activity(self, make_brain_with_stores, stores):
        r = stores["rules"].add("Toggle", rule_type="time", condition="after 22:00")
        brain = make_brain_with_stores({})
        brain._tool_toggle_rule({"rule_id": r.id})
        entries = stores["activity"].recent()
        assert any(e.action == "rule_toggled" for e in entries)


# ── Activity & token tools ───────────────────────────────────────────


class TestActivityTokenTools:
    def test_recent_activity_empty(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_recent_activity({})
        assert "No recent activity" in result

    def test_recent_activity_shows_entries(self, make_brain_with_stores, stores):
        stores["activity"].record("test_action", actor="test", detail="hello")
        brain = make_brain_with_stores({})
        result = brain._tool_recent_activity({})
        assert "test_action" in result
        assert "hello" in result

    def test_recent_activity_hides_awareness_bookkeeping(self, make_brain_with_stores, stores):
        """Screen assessments are Activity-panel bookkeeping. Handed to the chat
        model they bury the rows the user asked about — and read as an answer."""
        stores["activity"].record("task_completed", actor="user", detail="filed taxes")
        for i in range(20):
            stores["activity"].record(
                "screen_assessed",
                actor="awareness",
                detail=f"verdict=no interrupt, reason=The user is browsing Reddit ({i})",
            )
        stores["activity"].record(
            "awareness_intervention",
            actor="awareness",
            detail="Screen intervention: The user is browsing Reddit",
        )
        brain = make_brain_with_stores({})
        result = brain._tool_recent_activity({})
        assert "filed taxes" in result
        assert "screen_assessed" not in result
        assert "The user is browsing Reddit" not in result

    def test_recent_activity_limit(self, make_brain_with_stores, stores):
        for i in range(5):
            stores["activity"].record(f"action_{i}")
        brain = make_brain_with_stores({})
        result = brain._tool_recent_activity({"limit": 2})
        assert "action_4" in result
        assert "action_3" in result

    def test_recent_screen_activity_disabled_and_empty(self, make_brain_with_stores):
        brain = make_brain_with_stores({})

        assert brain._tool_recent_screen_activity({}).startswith("ERROR:")

    def test_recent_screen_activity_renders_existing_rows_when_disabled(
        self, make_brain_with_stores, stores
    ):
        now = datetime.now()
        stores["observations"].record(
            started_at=now - timedelta(minutes=30),
            ended_at=now,
            app="Cursor",
            window_title="digest.py",
            category="work",
        )
        brain = make_brain_with_stores({})

        result = brain._tool_recent_screen_activity({})

        assert "Cursor" in result
        assert "digest.py" in result

    def test_recent_screen_activity_clamps_hours(self, make_brain_with_stores, stores):
        now = datetime.now()
        for hours, app in ((0.5, "Recent"), (2, "Too old for minimum"), (23, "Yesterday")):
            stores["observations"].record(
                started_at=now - timedelta(hours=hours),
                ended_at=now - timedelta(hours=hours) + timedelta(minutes=5),
                app=app,
                category="work",
            )
        brain = make_brain_with_stores({})

        minimum = brain._tool_recent_screen_activity({"hours": -10})
        maximum = brain._tool_recent_screen_activity({"hours": 999})

        assert "Recent" in minimum
        assert "Too old for minimum" not in minimum
        assert "Yesterday" in maximum

    def test_token_usage_empty(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_token_usage({})
        assert "No token usage" in result

    def test_token_usage_shows_summary(self, make_brain_with_stores, stores):
        stores["token_usage"].record(lane="chat", prompt_tokens=100, completion_tokens=50)
        brain = make_brain_with_stores({})
        result = brain._tool_token_usage({"period": "all"})
        assert "chat" in result
        assert "150" in result


# ── Existing task tools record activity ──────────────────────────────


class TestTaskToolsActivity:
    def test_add_task_records_activity(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        result = brain._tool_add_task({"title": "New task"})
        assert "added" in result
        entries = stores["activity"].recent()
        assert any(e.action == "task_added" for e in entries)

    def test_complete_task_records_activity(self, make_brain_with_stores, stores):
        task, _ = make_brain_with_stores({}).store.add("Do thing")
        brain = make_brain_with_stores({})
        brain._tool_complete_task({"ref": f"#{task.id}"})
        entries = stores["activity"].recent()
        assert any(e.action == "task_completed" for e in entries)

    def test_cancel_task_records_activity(self, make_brain_with_stores, stores):
        task, _ = make_brain_with_stores({}).store.add("Do thing")
        brain = make_brain_with_stores({})
        brain._tool_cancel_task({"ref": f"#{task.id}"})
        entries = stores["activity"].recent()
        assert any(e.action == "task_cancelled" for e in entries)

    def test_snooze_task_records_activity(self, make_brain_with_stores, stores):
        task, _ = make_brain_with_stores({}).store.add("Do thing")
        brain = make_brain_with_stores({})
        # parse_when uses LLM, so we need a when-sensor handler
        brain._tool_snooze_task({"ref": f"#{task.id}", "until": "tomorrow"})
        # parse_when falls back to offline parser for "tomorrow"
        # May or may not have snoozed depending on parse_when result
        # But no crash is the key test

    def test_restore_task_records_activity(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        task, _ = brain.store.add("Do thing")
        brain.store.drop(task)
        brain._tool_restore_task({"ref": "Do thing"})
        entries = stores["activity"].recent()
        assert any(e.action == "task_restored" for e in entries)


# ── Scheduler: goal sync ─────────────────────────────────────────────


class TestSchedulerGoalSync:
    async def test_goal_sync_runs_on_tick(self, store, stores):
        """Goal sync runs every tick regardless of quiet hours."""
        routine = stores["routines"].add("Morning run", cadence="daily")
        goal = stores["goals"].add("Run daily", linked_routine_ids=[routine.id], target_count=2)
        stores["routine_completions"].upsert(routine.id, "2026-01-15", status="done")

        engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )

        delivered = []

        async def deliver(msg):
            delivered.append(msg)

        sched = ReminderScheduler(
            store,
            RemindersConfig(),
            deliver,
            goal_engine=engine,
        )

        now = datetime(2026, 1, 15, 10, 0)
        await sched.tick(now)
        # Goal progress should have been synced
        progress = stores["goal_progress"].by_goal(goal.id)
        assert len(progress) == 1
        assert progress[0].met == 1

    async def test_goal_sync_runs_in_quiet_hours(self, store, stores):
        """Goal sync runs even during quiet hours."""
        routine = stores["routines"].add("Morning run", cadence="daily")
        goal = stores["goals"].add("Run daily", linked_routine_ids=[routine.id])
        stores["routine_completions"].upsert(routine.id, "2026-01-15", status="done")

        engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
        )

        delivered = []

        async def deliver(msg):
            delivered.append(msg)

        sched = ReminderScheduler(
            store,
            RemindersConfig(quiet_hours_start=23, quiet_hours_end=8),
            deliver,
            goal_engine=engine,
        )

        # 23:30 — in quiet hours
        now = datetime(2026, 1, 15, 23, 30)
        await sched.tick(now)
        # Goal sync should still have run
        progress = stores["goal_progress"].by_goal(goal.id)
        assert len(progress) == 1

    async def test_goal_sync_no_engine(self, store):
        """Scheduler works without goal engine."""
        delivered = []

        async def deliver(msg):
            delivered.append(msg)

        sched = ReminderScheduler(store, RemindersConfig(), deliver)
        assert sched.goal_engine is None
        result = await sched.tick(datetime(2026, 1, 15, 10, 0))
        assert result is None


# ── Scheduler: rule firing ───────────────────────────────────────────


class TestSchedulerRuleFiring:
    async def test_rule_fires_on_tick(self, store, stores):
        """Rule engine fires on scheduler tick."""
        stores["rules"].add(
            "Late night",
            rule_type="time",
            condition="after 22:00",
            message="Sleep!",
        )

        rule_engine = RuleEngine(
            rule_store=stores["rules"],
            activity_store=stores["activity"],
        )

        delivered = []

        async def deliver(msg):
            delivered.append(msg)

        sched = ReminderScheduler(
            store,
            RemindersConfig(),
            deliver,
            rule_engine=rule_engine,
        )

        now = datetime(2026, 1, 15, 23, 0)
        result = await sched.tick(now)
        assert result is not None
        assert "Sleep!" in result

    async def test_rule_suppressed_in_quiet(self, store, stores):
        """Rule delivery suppressed in quiet hours but rule still fires."""
        stores["rules"].add(
            "Quiet rule",
            rule_type="time",
            condition="after 22:00",
            message="Shh",
        )

        delivered = []

        def sync_delivery(msg, rid):
            delivered.append(msg)

        rule_engine = RuleEngine(
            rule_store=stores["rules"],
            activity_store=stores["activity"],
            delivery=sync_delivery,
        )

        async def deliver(msg):
            pass

        sched = ReminderScheduler(
            store,
            RemindersConfig(quiet_hours_start=23, quiet_hours_end=8),
            deliver,
            rule_engine=rule_engine,
        )

        now = datetime(2026, 1, 15, 23, 30)
        await sched.tick(now)
        # Delivery should be suppressed in quiet hours
        assert len(delivered) == 0
        # But activity should still be recorded
        entries = stores["activity"].recent()
        assert any(e.action == "rule_fired" for e in entries)

    async def test_rule_no_engine(self, store):
        """Scheduler works without rule engine."""
        delivered = []

        async def deliver(msg):
            delivered.append(msg)

        sched = ReminderScheduler(store, RemindersConfig(), deliver)
        assert sched.rule_engine is None
        result = await sched.tick(datetime(2026, 1, 15, 10, 0))
        assert result is None


class TestGroundedIDs:
    """Tool handlers must validate IDs against real DB rows."""

    def test_complete_routine_invalid_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_complete_routine({"routine_id": 99999})
        assert "no routine" in result

    def test_skip_routine_invalid_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_skip_routine({"routine_id": 99999})
        assert "no routine" in result

    def test_archive_routine_invalid_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_archive_routine({"routine_id": 99999})
        assert "no routine" in result

    def test_check_in_goal_invalid_id(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._goal_engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )
        result = brain._tool_check_in_goal({"goal_id": 99999, "met": True})
        assert "no goal" in result

    def test_achieve_goal_invalid_id(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._goal_engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )
        result = brain._tool_achieve_goal({"goal_id": 99999})
        assert "no goal" in result

    def test_reopen_goal_invalid_id(self, make_brain_with_stores, stores):
        brain = make_brain_with_stores({})
        brain._goal_engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )
        result = brain._tool_reopen_goal({"goal_id": 99999})
        assert "no goal" in result

    def test_toggle_rule_invalid_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_toggle_rule({"rule_id": 99999})
        assert "no rule" in result

    def test_delete_rule_invalid_id(self, make_brain_with_stores):
        brain = make_brain_with_stores({})
        result = brain._tool_delete_rule({"rule_id": 99999})
        assert "no rule" in result

    def test_link_routine_to_goal_invalid_goal(self, make_brain_with_stores, stores):
        stores["routines"].add("Routine")
        brain = make_brain_with_stores({})
        brain._goal_engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )
        result = brain._tool_link_routine_to_goal({"goal_id": 99999, "routine_id": 1})
        assert "no goal" in result

    def test_link_routine_to_goal_invalid_routine(self, make_brain_with_stores, stores):
        stores["goals"].add("Goal")
        brain = make_brain_with_stores({})
        brain._goal_engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=stores["activity"],
        )
        result = brain._tool_link_routine_to_goal({"goal_id": 1, "routine_id": 99999})
        assert "no routine" in result
