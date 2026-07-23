"""Tests for goals.py — pure helpers, linked-routine evaluation, and GoalEngine.

Covers:
- compute_streaks / count_met / is_achieved (pure functions)
- evaluate_linked_goal_met (single/multiple routines, scheduled-day logic)
- GoalEngine: manual check_in, count goals, streak goals, gaps
- GoalEngine: linked single/multiple routines, scheduled-day skipping
- GoalEngine: auto-achievement, idempotency, reopen/link APIs, activity
- GoalEngine: summaries
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from clipponyai.accountability import (
    GoalProgress,
    get_stores,
)
from clipponyai.goals import (
    GoalEngine,
    GoalSummary,
    compute_streaks,
    count_met,
    evaluate_linked_goal_met,
    is_achieved,
)
from clipponyai.tasks import TaskStore

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "goals.db")
    yield s
    s.close()


@pytest.fixture
def stores(store):
    return get_stores(store)


@pytest.fixture
def engine(stores):
    return GoalEngine(
        goal_store=stores["goals"],
        progress_store=stores["goal_progress"],
        routine_store=stores["routines"],
        completion_store=stores["routine_completions"],
        activity_store=stores["activity"],
    )


def _make_progress(goal_id: int, date_str: str, met: int = 1, note: str = "") -> GoalProgress:
    return GoalProgress(
        id=0, goal_id=goal_id, date=date_str, met=met, note=note,
    )


# ── Pure helpers ──────────────────────────────────────────────────────


class TestComputeStreaks:
    def test_empty(self):
        assert compute_streaks([]) == (0, 0)

    def test_single_met(self):
        entries = [_make_progress(1, "2026-01-01", met=1)]
        assert compute_streaks(entries) == (1, 1)

    def test_single_not_met(self):
        entries = [_make_progress(1, "2026-01-01", met=0)]
        assert compute_streaks(entries) == (0, 0)

    def test_consecutive_met(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=1),
        ]
        assert compute_streaks(entries) == (3, 3)

    def test_current_streak_broken_by_gap(self):
        """A missing day between entries breaks the current streak."""
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            # 2026-01-03 missing (gap)
            _make_progress(1, "2026-01-04", met=1),
        ]
        current, longest = compute_streaks(entries)
        assert current == 1  # only Jan 4
        assert longest == 2  # Jan 1-2

    def test_current_streak_broken_by_met_0(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=0),
            _make_progress(1, "2026-01-04", met=1),
            _make_progress(1, "2026-01-05", met=1),
        ]
        current, longest = compute_streaks(entries)
        assert current == 2  # Jan 4-5
        assert longest == 2  # Jan 1-2

    def test_longest_is_earlier_run(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=1),
            _make_progress(1, "2026-01-04", met=0),
            _make_progress(1, "2026-01-05", met=1),
        ]
        assert compute_streaks(entries) == (1, 3)

    def test_unsorted_input(self):
        """Entries may arrive in any order."""
        entries = [
            _make_progress(1, "2026-01-03", met=1),
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
        ]
        assert compute_streaks(entries) == (3, 3)

    def test_all_not_met(self):
        entries = [
            _make_progress(1, "2026-01-01", met=0),
            _make_progress(1, "2026-01-02", met=0),
        ]
        assert compute_streaks(entries) == (0, 0)


class TestCountMet:
    def test_empty(self):
        assert count_met([]) == 0

    def test_mixed(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=0),
            _make_progress(1, "2026-01-03", met=1),
        ]
        assert count_met(entries) == 2


class TestIsAchieved:
    def test_target_count_reached(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=1),
        ]
        assert is_achieved(entries, target_count=3, target_streak=None) is True

    def test_target_count_not_reached(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
        ]
        assert is_achieved(entries, target_count=3, target_streak=None) is False

    def test_target_streak_reached(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=1),
        ]
        assert is_achieved(entries, target_count=None, target_streak=3) is True

    def test_target_streak_not_reached(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=0),
            _make_progress(1, "2026-01-04", met=1),
        ]
        assert is_achieved(entries, target_count=None, target_streak=3) is False

    def test_both_targets_count_wins(self):
        """When both set, EITHER triggers (OR semantics)."""
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=1),
        ]
        assert is_achieved(entries, target_count=3, target_streak=5) is True

    def test_both_targets_streak_wins(self):
        entries = [
            _make_progress(1, "2026-01-01", met=1),
            _make_progress(1, "2026-01-02", met=1),
            _make_progress(1, "2026-01-03", met=1),
        ]
        assert is_achieved(entries, target_count=10, target_streak=3) is True

    def test_neither_target(self):
        entries = [_make_progress(1, "2026-01-01", met=1)]
        assert is_achieved(entries, target_count=None, target_streak=None) is False

    def test_empty_entries(self):
        assert is_achieved([], target_count=1, target_streak=None) is False


# ── Linked-routine evaluation ─────────────────────────────────────────


class TestEvaluateLinkedGoalMet:
    def _make_routine(self, rid: int, cadence: str = "daily", **kwargs):
        from clipponyai.accountability import Routine

        defaults = {
            "id": rid, "title": f"Routine {rid}", "notes": "", "cadence": cadence,
            "weekdays": [], "time_of_day": None, "day_of_month": None,
            "deadline_time": None, "priority": "medium", "enabled": True,
            "created_at": datetime(2026, 1, 1), "archived_at": None,
        }
        defaults.update(kwargs)
        return Routine(**defaults)

    def _make_completion(self, routine_id: int, occurrence_date: str, status: str = "done"):
        from clipponyai.accountability import RoutineCompletion

        return RoutineCompletion(
            id=0, routine_id=routine_id, occurrence_date=occurrence_date,
            status=status, at=datetime(2026, 1, 1), task_id=None,
        )

    def test_single_routine_done(self):
        routines = {1: self._make_routine(1)}
        completions = [self._make_completion(1, "2026-01-15", "done")]
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1], routines, completions,
        ) is True

    def test_single_routine_skipped(self):
        routines = {1: self._make_routine(1)}
        completions = [self._make_completion(1, "2026-01-15", "skipped")]
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1], routines, completions,
        ) is False

    def test_single_routine_missed(self):
        routines = {1: self._make_routine(1)}
        completions = [self._make_completion(1, "2026-01-15", "missed")]
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1], routines, completions,
        ) is False

    def test_single_routine_no_completion(self):
        routines = {1: self._make_routine(1)}
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1], routines, [],
        ) is False

    def test_multiple_routines_all_done(self):
        routines = {
            1: self._make_routine(1),
            2: self._make_routine(2),
        }
        completions = [
            self._make_completion(1, "2026-01-15", "done"),
            self._make_completion(2, "2026-01-15", "done"),
        ]
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1, 2], routines, completions,
        ) is True

    def test_multiple_routines_one_skipped(self):
        routines = {
            1: self._make_routine(1),
            2: self._make_routine(2),
        }
        completions = [
            self._make_completion(1, "2026-01-15", "done"),
            self._make_completion(2, "2026-01-15", "skipped"),
        ]
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1, 2], routines, completions,
        ) is False

    def test_routine_not_scheduled_returns_none(self):
        """Weekday routine on Saturday — no progress entry."""
        routines = {1: self._make_routine(1, cadence="weekdays")}
        # 2026-01-17 is Saturday
        assert evaluate_linked_goal_met(
            date(2026, 1, 17), [1], routines, [],
        ) is None

    def test_one_scheduled_one_not(self):
        """One routine scheduled, one not — evaluate only the scheduled one."""
        routines = {
            1: self._make_routine(1, cadence="daily"),
            2: self._make_routine(2, cadence="weekdays"),
        }
        completions = [self._make_completion(1, "2026-01-17", "done")]
        # 2026-01-17 is Saturday — routine 2 not scheduled
        assert evaluate_linked_goal_met(
            date(2026, 1, 17), [1, 2], routines, completions,
        ) is True

    def test_disabled_routine_ignored(self):
        routines = {1: self._make_routine(1, enabled=False)}
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1], routines, [],
        ) is None

    def test_archived_routine_ignored(self):
        routines = {
            1: self._make_routine(1, archived_at=datetime(2026, 1, 10, 0, 0)),

        }
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [1], routines, [],
        ) is None

    def test_missing_routine_ignored(self):
        """Routine ID not in map — silently skipped."""
        assert evaluate_linked_goal_met(
            date(2026, 1, 15), [999], {}, [],
        ) is None


# ── GoalEngine: manual check-in ───────────────────────────────────────


class TestGoalEngineCheckIn:
    def test_manual_check_in_met(self, engine):
        goal = engine.goals.add("Read daily", target_count=3)
        entry = engine.check_in(goal.id, date(2026, 1, 1), met=True, note="chapter 1")
        assert entry.met == 1
        assert entry.note == "chapter 1"

    def test_manual_check_in_not_met(self, engine):
        goal = engine.goals.add("Read daily", target_count=3)
        entry = engine.check_in(goal.id, date(2026, 1, 1), met=False, note="too busy")
        assert entry.met == 0

    def test_manual_check_in_idempotent(self, engine):
        goal = engine.goals.add("Read daily")
        e1 = engine.check_in(goal.id, date(2026, 1, 1), met=True, note="first")
        e2 = engine.check_in(goal.id, date(2026, 1, 1), met=True, note="second")
        assert e2.id == e1.id  # same row (upsert)
        assert e2.note == "second"

    def test_activity_recorded(self, engine):
        goal = engine.goals.add("Read daily")
        engine.check_in(goal.id, date(2026, 1, 1), met=True, note="test")
        recent = engine.activity.recent()
        assert any(e.action == "goal_check_in" for e in recent)


# ── GoalEngine: count goals ───────────────────────────────────────────


class TestGoalEngineCountGoals:
    def test_count_goal_achieved(self, engine):
        goal = engine.goals.add("Do 3 days", target_count=3)
        for d in range(1, 4):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "achieved"

    def test_count_goal_not_achieved(self, engine):
        goal = engine.goals.add("Do 5 days", target_count=5)
        for d in range(1, 4):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "active"

    def test_count_includes_only_met_entries(self, engine):
        goal = engine.goals.add("Do 3 days", target_count=3)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        engine.check_in(goal.id, date(2026, 1, 2), met=False)
        engine.check_in(goal.id, date(2026, 1, 3), met=True)
        engine.check_in(goal.id, date(2026, 1, 4), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "achieved"


# ── GoalEngine: streak goals ──────────────────────────────────────────


class TestGoalEngineStreakGoals:
    def test_streak_goal_achieved(self, engine):
        goal = engine.goals.add("3-day streak", target_streak=3)
        for d in range(1, 4):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "achieved"

    def test_streak_goal_broken_by_gap(self, engine):
        """A missing day between entries breaks the streak."""
        goal = engine.goals.add("3-day streak", target_streak=3)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        engine.check_in(goal.id, date(2026, 1, 2), met=True)
        # Jan 3 missing — gap breaks streak
        engine.check_in(goal.id, date(2026, 1, 4), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "active"  # longest streak is 2, not 3

    def test_streak_goal_broken_by_met_0(self, engine):
        goal = engine.goals.add("3-day streak", target_streak=3)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        engine.check_in(goal.id, date(2026, 1, 2), met=True)
        engine.check_in(goal.id, date(2026, 1, 3), met=False)
        engine.check_in(goal.id, date(2026, 1, 4), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "active"

    def test_both_targets_streak_wins(self, engine):
        """When both targets set, streak alone can achieve."""
        goal = engine.goals.add("Streak or count", target_count=10, target_streak=3)
        for d in range(1, 4):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "achieved"

    def test_both_targets_count_wins(self, engine):
        """When both targets set, count alone can achieve."""
        goal = engine.goals.add("Streak or count", target_count=3, target_streak=10)
        for d in range(1, 4):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        goal = engine.goals.get(goal.id)
        assert goal.status == "achieved"


# ── GoalEngine: linked single routine ─────────────────────────────────


class TestGoalEngineLinkedSingleRoutine:
    def _setup_routine_and_goal(self, engine, cadence="daily", **kw):
        routine = engine.routines.add(
            "Morning routine", cadence=cadence, **kw
        )
        goal = engine.goals.add("Complete routine", linked_routine_ids=[routine.id])
        return routine, goal

    def test_sync_met(self, engine):
        routine, _goal = self._setup_routine_and_goal(engine)
        engine.completions.upsert(routine.id, "2026-01-15", status="done")
        updated = engine.sync(date(2026, 1, 15))
        assert len(updated) == 1
        assert updated[0].met == 1

    def test_sync_not_met(self, engine):
        routine, _goal = self._setup_routine_and_goal(engine)
        engine.completions.upsert(routine.id, "2026-01-15", status="skipped")
        updated = engine.sync(date(2026, 1, 15))
        assert len(updated) == 1
        assert updated[0].met == 0

    def test_sync_no_completion(self, engine):
        _routine, _goal = self._setup_routine_and_goal(engine)
        updated = engine.sync(date(2026, 1, 15))
        assert len(updated) == 1
        assert updated[0].met == 0

    def test_sync_weekday_routine_skips_saturday(self, engine):
        """Weekday routine on Saturday — no progress entry created."""
        _routine, _goal = self._setup_routine_and_goal(engine, cadence="weekdays")
        # 2026-01-17 is Saturday
        updated = engine.sync(date(2026, 1, 17))
        assert len(updated) == 0  # no routine scheduled

    def test_sync_idempotent(self, engine):
        routine, _goal = self._setup_routine_and_goal(engine)
        engine.completions.upsert(routine.id, "2026-01-15", status="done")
        engine.sync(date(2026, 1, 15))
        u2 = engine.sync(date(2026, 1, 15))
        assert len(u2) == 1

    def test_sync_auto_achieves_count(self, engine):
        routine, goal = self._setup_routine_and_goal(engine, cadence="daily")
        goal = engine.goals.update(goal.id, target_count=2)
        # Day 1
        engine.completions.upsert(routine.id, "2026-01-14", status="done")
        engine.sync(date(2026, 1, 14))
        assert engine.goals.get(goal.id).status == "active"
        # Day 2 — should auto-achieve
        engine.completions.upsert(routine.id, "2026-01-15", status="done")
        engine.sync(date(2026, 1, 15))
        assert engine.goals.get(goal.id).status == "achieved"

    def test_sync_does_not_update_achieved_goal(self, engine):
        routine, goal = self._setup_routine_and_goal(engine)
        engine.goals.achieve(goal.id)
        engine.completions.upsert(routine.id, "2026-01-15", status="done")
        updated = engine.sync(date(2026, 1, 15))
        assert len(updated) == 0


# ── GoalEngine: linked multiple routines ──────────────────────────────


class TestGoalEngineLinkedMultipleRoutines:
    def _setup(self, engine):
        r1 = engine.routines.add("Exercise", cadence="daily")
        r2 = engine.routines.add("Read", cadence="daily")
        goal = engine.goals.add("Full day", linked_routine_ids=[r1.id, r2.id])
        return r1, r2, goal

    def test_all_done(self, engine):
        r1, r2, _goal = self._setup(engine)
        engine.completions.upsert(r1.id, "2026-01-15", status="done")
        engine.completions.upsert(r2.id, "2026-01-15", status="done")
        updated = engine.sync(date(2026, 1, 15))
        assert len(updated) == 1
        assert updated[0].met == 1

    def test_one_skipped(self, engine):
        r1, r2, _goal = self._setup(engine)
        engine.completions.upsert(r1.id, "2026-01-15", status="done")
        engine.completions.upsert(r2.id, "2026-01-15", status="skipped")
        updated = engine.sync(date(2026, 1, 15))
        assert len(updated) == 1
        assert updated[0].met == 0

    def test_mixed_schedules(self, engine):
        """One daily, one weekday — on Saturday only daily is scheduled."""
        r1 = engine.routines.add("Exercise", cadence="daily")
        r2 = engine.routines.add("Read", cadence="weekdays")
        engine.goals.add("Full day", linked_routine_ids=[r1.id, r2.id])
        # Saturday Jan 17 — only r1 scheduled
        engine.completions.upsert(r1.id, "2026-01-17", status="done")
        updated = engine.sync(date(2026, 1, 17))
        assert len(updated) == 1
        assert updated[0].met == 1  # r1 done, r2 not scheduled

    def test_neither_scheduled(self, engine):
        """Both weekday routines on Saturday — no progress entry."""
        r1 = engine.routines.add("Exercise", cadence="weekdays")
        r2 = engine.routines.add("Read", cadence="weekdays")
        engine.goals.add("Full day", linked_routine_ids=[r1.id, r2.id])
        # Saturday Jan 17
        updated = engine.sync(date(2026, 1, 17))
        assert len(updated) == 0


# ── GoalEngine: auto-achievement ──────────────────────────────────────


class TestGoalEngineAutoAchievement:
    def test_count_auto_achieve(self, engine):
        goal = engine.goals.add("5 days", target_count=5)
        for d in range(1, 6):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        assert engine.goals.get(goal.id).status == "achieved"

    def test_streak_auto_achieve(self, engine):
        goal = engine.goals.add("3 streak", target_streak=3)
        for d in range(1, 4):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        assert engine.goals.get(goal.id).status == "achieved"

    def test_activity_on_auto_achieve(self, engine):
        goal = engine.goals.add("1 day", target_count=1)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        recent = engine.activity.recent()
        assert any(e.action == "goal_auto_achieved" for e in recent)

    def test_no_auto_achieve_without_targets(self, engine):
        goal = engine.goals.add("Free goal")
        for d in range(1, 10):
            engine.check_in(goal.id, date(2026, 1, d), met=True)
        assert engine.goals.get(goal.id).status == "active"


# ── GoalEngine: reopen / mark_achieved ────────────────────────────────


class TestGoalEngineReopen:
    def test_reopen_achieved_goal(self, engine):
        goal = engine.goals.add("Done", target_count=1)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        assert engine.goals.get(goal.id).status == "achieved"
        reopened = engine.reopen(goal.id)
        assert reopened.status == "active"
        assert reopened.achieved_at is None

    def test_reopen_records_activity(self, engine):
        goal = engine.goals.add("Done", target_count=1)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        engine.reopen(goal.id)
        recent = engine.activity.recent()
        assert any(e.action == "goal_reopened" for e in recent)

    def test_reopen_preserves_progress(self, engine):
        goal = engine.goals.add("Done", target_count=1)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        engine.reopen(goal.id)
        entries = engine.progress.by_goal(goal.id)
        assert len(entries) == 1
        assert entries[0].met == 1

    def test_mark_achieved(self, engine):
        goal = engine.goals.add("Manual achieve")
        achieved = engine.mark_achieved(goal.id)
        assert achieved.status == "achieved"

    def test_mark_achieved_records_activity(self, engine):
        goal = engine.goals.add("Manual achieve")
        engine.mark_achieved(goal.id)
        recent = engine.activity.recent()
        assert any(e.action == "goal_achieved" for e in recent)


# ── GoalEngine: link / unlink routines ────────────────────────────────


class TestGoalEngineLinkUnlink:
    def test_link_routine(self, engine):
        goal = engine.goals.add("New goal")
        routine = engine.routines.add("New routine")
        updated = engine.link_routine(goal.id, routine.id)
        assert routine.id in updated.linked_routine_ids

    def test_link_routine_idempotent(self, engine):
        goal = engine.goals.add("New goal")
        routine = engine.routines.add("New routine")
        engine.link_routine(goal.id, routine.id)
        u2 = engine.link_routine(goal.id, routine.id)
        assert u2.linked_routine_ids == [routine.id]  # no duplicate

    def test_unlink_routine(self, engine):
        goal = engine.goals.add("New goal", linked_routine_ids=[1, 2])
        updated = engine.unlink_routine(goal.id, 1)
        assert 1 not in updated.linked_routine_ids
        assert 2 in updated.linked_routine_ids

    def test_link_records_activity(self, engine):
        goal = engine.goals.add("New goal")
        routine = engine.routines.add("New routine")
        engine.link_routine(goal.id, routine.id)
        recent = engine.activity.recent()
        assert any(e.action == "goal_link_routine" for e in recent)

    def test_unlink_records_activity(self, engine):
        goal = engine.goals.add("New goal", linked_routine_ids=[1])
        engine.unlink_routine(goal.id, 1)
        recent = engine.activity.recent()
        assert any(e.action == "goal_unlink_routine" for e in recent)


# ── GoalEngine: summaries ─────────────────────────────────────────────


class TestGoalEngineSummaries:
    def test_empty_summaries(self, engine):
        assert engine.summaries() == []

    def test_single_goal_summary(self, engine):
        goal = engine.goals.add("Read daily", target_count=5, target_streak=3)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        engine.check_in(goal.id, date(2026, 1, 2), met=True)
        engine.check_in(goal.id, date(2026, 1, 3), met=False)
        engine.check_in(goal.id, date(2026, 1, 4), met=True)
        summaries = engine.summaries()
        assert len(summaries) == 1
        s = summaries[0]
        assert isinstance(s, GoalSummary)
        assert s.goal_id == goal.id
        assert s.title == "Read daily"
        assert s.count == 3  # 3 met entries
        assert s.current_streak == 1  # Jan 4 only
        assert s.longest_streak == 2  # Jan 1-2
        assert s.target_count == 5
        assert s.target_streak == 3
        assert s.status == "active"

    def test_achieved_goal_summary(self, engine):
        goal = engine.goals.add("Quick goal", target_count=1)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        summaries = engine.summaries()
        assert len(summaries) == 1
        assert summaries[0].status == "achieved"


# ── GoalEngine: no activity store ─────────────────────────────────────


class TestGoalEngineNoActivity:
    def test_engine_works_without_activity(self, stores):
        engine = GoalEngine(
            goal_store=stores["goals"],
            progress_store=stores["goal_progress"],
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            activity_store=None,
        )
        goal = engine.goals.add("No activity", target_count=1)
        engine.check_in(goal.id, date(2026, 1, 1), met=True)
        assert engine.goals.get(goal.id).status == "achieved"
