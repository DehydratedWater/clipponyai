"""Tests for routine recurrence, streaks, and the RoutineEngine tick.

Covers:
- validation (HH:MM, cadence)
- is_scheduled_on for daily, weekdays, monthly (incl. 31st clamp and leap years)
- occurrence_due_date
- due_at priority (time_of_day > deadline_time > 09:00)
- next_occurrence
- current_streak and longest_streak (daily, weekday, monthly)
- RoutineEngine tick: reminder, dedupe, missed-after-deadline, completion, skip
- quiet-hour suppression integration
- disabled/archived routines ignored
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dt_time

import pytest

from clipponyai.accountability import Routine, RoutineCompletion, get_stores
from clipponyai.routines import (
    RoutineEngine,
    _clamp_monthly,
    current_streak,
    due_at,
    is_scheduled_on,
    longest_streak,
    next_occurrence,
    occurrence_due_date,
    validate_cadence,
    validate_time_of_day,
)
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
def delivered():
    return []


def _make_routine(**kwargs) -> Routine:
    """Build a Routine dataclass with sensible defaults for tests."""
    defaults = dict(
        id=1,
        title="Test routine",
        notes="",
        cadence="daily",
        weekdays=[],
        time_of_day=None,
        day_of_month=None,
        deadline_time=None,
        priority="medium",
        enabled=True,
        created_at=datetime(2026, 1, 1),
        archived_at=None,
    )
    defaults.update(kwargs)
    return Routine(**defaults)


def _make_completion(routine_id: int, occurrence_date: str, status: str = "done") -> RoutineCompletion:
    return RoutineCompletion(
        id=len(_make_completion._ids) + 1,
        routine_id=routine_id,
        occurrence_date=occurrence_date,
        status=status,
        at=datetime.now(),
        task_id=None,
    )


_make_completion._ids = []  # type: ignore[attr-defined]


# ── Validation ────────────────────────────────────────────────────────


class TestValidation:
    def test_valid_times(self):
        assert validate_time_of_day("09:00") == "09:00"
        assert validate_time_of_day("0:0") == "00:00"
        assert validate_time_of_day("23:59") == "23:59"

    def test_invalid_times(self):
        with pytest.raises(ValueError, match="invalid time format"):
            validate_time_of_day("abc")
        with pytest.raises(ValueError, match="invalid time format"):
            validate_time_of_day("12:30:00")
        with pytest.raises(ValueError, match="time out of range"):
            validate_time_of_day("25:00")
        with pytest.raises(ValueError, match="time out of range"):
            validate_time_of_day("12:60")

    def test_valid_cadences(self):
        assert validate_cadence("daily") == "daily"
        assert validate_cadence("weekdays") == "weekdays"
        assert validate_cadence("monthly") == "monthly"

    def test_invalid_cadence(self):
        with pytest.raises(ValueError, match="invalid cadence"):
            validate_cadence("weekly")
        with pytest.raises(ValueError, match="invalid cadence"):
            validate_cadence("hourly")


# ── is_scheduled_on ───────────────────────────────────────────────────


class TestIsScheduledOn:
    def test_daily_every_day(self):
        r = _make_routine(cadence="daily")
        for day in range(1, 29):
            d = date(2026, 1, day)
            assert is_scheduled_on(r, d), f"should be scheduled on {d}"

    def test_weekdays_default_mon_fri(self):
        r = _make_routine(cadence="weekdays")
        # 2026-01-05 is Monday, 2026-01-10 is Saturday
        assert is_scheduled_on(r, date(2026, 1, 5))  # Mon
        assert is_scheduled_on(r, date(2026, 1, 6))  # Tue
        assert is_scheduled_on(r, date(2026, 1, 7))  # Wed
        assert is_scheduled_on(r, date(2026, 1, 8))  # Thu
        assert is_scheduled_on(r, date(2026, 1, 9))  # Fri
        assert not is_scheduled_on(r, date(2026, 1, 10))  # Sat
        assert not is_scheduled_on(r, date(2026, 1, 11))  # Sun

    def test_weekdays_custom_list(self):
        r = _make_routine(cadence="weekdays", weekdays=[0, 3])  # Mon, Thu
        assert is_scheduled_on(r, date(2026, 1, 5))  # Mon
        assert not is_scheduled_on(r, date(2026, 1, 6))  # Tue
        assert is_scheduled_on(r, date(2026, 1, 8))  # Thu
        assert not is_scheduled_on(r, date(2026, 1, 10))  # Sat

    def test_weekdays_sunday_only(self):
        r = _make_routine(cadence="weekdays", weekdays=[6])  # Sun only
        assert not is_scheduled_on(r, date(2026, 1, 5))  # Mon
        assert is_scheduled_on(r, date(2026, 1, 11))  # Sun

    def test_monthly_day_15(self):
        r = _make_routine(cadence="monthly", day_of_month=15)
        assert is_scheduled_on(r, date(2026, 1, 15))
        assert not is_scheduled_on(r, date(2026, 1, 14))
        assert not is_scheduled_on(r, date(2026, 1, 16))

    def test_monthly_31_clamped_to_feb_28(self):
        """day_of_month=31 clamps to 28 in Feb (non-leap)."""
        r = _make_routine(cadence="monthly", day_of_month=31)
        assert not is_scheduled_on(r, date(2026, 2, 27))
        assert is_scheduled_on(r, date(2026, 2, 28))

    def test_monthly_31_clamped_to_feb_29_leap(self):
        """day_of_month=31 clamps to 29 in Feb (leap year)."""
        r = _make_routine(cadence="monthly", day_of_month=31)
        assert is_scheduled_on(r, date(2028, 2, 29))
        assert not is_scheduled_on(r, date(2028, 2, 28))

    def test_monthly_30_clamped_to_feb_28(self):
        r = _make_routine(cadence="monthly", day_of_month=30)
        assert is_scheduled_on(r, date(2026, 2, 28))
        assert not is_scheduled_on(r, date(2026, 2, 27))

    def test_monthly_exactly_one_per_month(self):
        """Only one occurrence per month regardless of day_of_month."""
        r = _make_routine(cadence="monthly", day_of_month=15)
        count = 0
        for day in range(1, 29):
            if is_scheduled_on(r, date(2026, 1, day)):
                count += 1
        assert count == 1

    def test_monthly_default_day_1(self):
        """day_of_month=None defaults to 1."""
        r = _make_routine(cadence="monthly", day_of_month=None)
        assert is_scheduled_on(r, date(2026, 3, 1))
        assert not is_scheduled_on(r, date(2026, 3, 2))


# ── _clamp_monthly ────────────────────────────────────────────────────


class TestClampMonthly:
    def test_no_clamp_needed(self):
        assert _clamp_monthly(15, 2026, 1) == 15

    def test_feb_non_leap_31(self):
        assert _clamp_monthly(31, 2026, 2) == 28

    def test_feb_leap_31(self):
        assert _clamp_monthly(31, 2028, 2) == 29

    def test_april_31(self):
        assert _clamp_monthly(31, 2026, 4) == 30

    def test_jan_31(self):
        assert _clamp_monthly(31, 2026, 1) == 31


# ── occurrence_due_date ───────────────────────────────────────────────


class TestOccurrenceDueDate:
    def test_scheduled_returns_date(self):
        r = _make_routine(cadence="daily")
        d = date(2026, 1, 15)
        assert occurrence_due_date(r, d) == d

    def test_not_scheduled_returns_none(self):
        r = _make_routine(cadence="weekdays", weekdays=[0])  # Mon only
        assert occurrence_due_date(r, date(2026, 1, 10)) is None  # Sat


# ── due_at ────────────────────────────────────────────────────────────


class TestDueAt:
    def test_time_of_day_priority(self):
        r = _make_routine(time_of_day="07:30", deadline_time="09:00")
        assert due_at(r, date(2026, 1, 1)) == dt_time(7, 30)

    def test_deadline_time_fallback(self):
        r = _make_routine(time_of_day=None, deadline_time="18:00")
        assert due_at(r, date(2026, 1, 1)) == dt_time(18, 0)

    def test_default_09_00(self):
        r = _make_routine(time_of_day=None, deadline_time=None)
        assert due_at(r, date(2026, 1, 1)) == dt_time(9, 0)


# ── next_occurrence ───────────────────────────────────────────────────


class TestNextOccurrence:
    def test_daily_next_day(self):
        r = _make_routine(cadence="daily", time_of_day="08:00")
        nxt = next_occurrence(r, datetime(2026, 1, 15, 10, 0))
        assert nxt == datetime(2026, 1, 16, 8, 0)

    def test_weekdays_skips_weekend(self):
        r = _make_routine(cadence="weekdays", time_of_day="09:00")
        # Friday Jan 9 -> next is Monday Jan 12
        nxt = next_occurrence(r, datetime(2026, 1, 9, 10, 0))
        assert nxt == datetime(2026, 1, 12, 9, 0)

    def test_monthly_next_month(self):
        r = _make_routine(cadence="monthly", day_of_month=15, time_of_day="10:00")
        nxt = next_occurrence(r, datetime(2026, 1, 15, 10, 0))
        assert nxt == datetime(2026, 2, 15, 10, 0)

    def test_monthly_31_clamps_in_next_occurrence(self):
        r = _make_routine(cadence="monthly", day_of_month=31, time_of_day="12:00")
        # Jan 31 -> next is Feb 28 (non-leap)
        nxt = next_occurrence(r, datetime(2026, 1, 31, 12, 0))
        assert nxt == datetime(2026, 2, 28, 12, 0)


# ── Streak calculations ──────────────────────────────────────────────


class TestCurrentStreak:
    def _completions(self, dates: list[str], status: str = "done") -> list[RoutineCompletion]:
        return [
            RoutineCompletion(
                id=i + 1, routine_id=1, occurrence_date=d,
                status=status, at=datetime.now(), task_id=None,
            )
            for i, d in enumerate(dates)
        ]

    def test_daily_streak_3(self):
        r = _make_routine(cadence="daily")
        comps = self._completions([
            "2026-01-13", "2026-01-14", "2026-01-15",
        ])
        assert current_streak(r, comps, date(2026, 1, 15)) == 3

    def test_daily_streak_broken_by_skip(self):
        r = _make_routine(cadence="daily")
        comps = [
            RoutineCompletion(id=1, routine_id=1, occurrence_date="2026-01-13",
                              status="done", at=datetime.now(), task_id=None),
            RoutineCompletion(id=2, routine_id=1, occurrence_date="2026-01-14",
                              status="skipped", at=datetime.now(), task_id=None),
            RoutineCompletion(id=3, routine_id=1, occurrence_date="2026-01-15",
                              status="done", at=datetime.now(), task_id=None),
        ]
        assert current_streak(r, comps, date(2026, 1, 15)) == 1

    def test_daily_streak_zero_no_completions(self):
        r = _make_routine(cadence="daily")
        assert current_streak(r, [], date(2026, 1, 15)) == 0

    def test_daily_streak_missed_breaks(self):
        r = _make_routine(cadence="daily")
        comps = [
            RoutineCompletion(id=1, routine_id=1, occurrence_date="2026-01-14",
                              status="missed", at=datetime.now(), task_id=None),
            RoutineCompletion(id=2, routine_id=1, occurrence_date="2026-01-15",
                              status="done", at=datetime.now(), task_id=None),
        ]
        assert current_streak(r, comps, date(2026, 1, 15)) == 1

    def test_weekday_streak_skips_weekend(self):
        """Weekday routine: Mon-Fri streak counts only scheduled days."""
        r = _make_routine(cadence="weekdays")
        # Mon 12, Tue 13, Wed 14 are done; Fri 9 is done too
        # Sat 10, Sun 11 are not scheduled so they don't break streak
        comps = self._completions([
            "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14",
        ])
        # Wed Jan 14: going back: Wed(done), Tue(done), Mon(done),
        #   Sat(not scheduled, skip), Sun(not scheduled, skip),
        #   Fri(done) -> streak = 4
        assert current_streak(r, comps, date(2026, 1, 14)) == 4

    def test_monthly_streak(self):
        r = _make_routine(cadence="monthly", day_of_month=1)
        comps = self._completions([
            "2026-01-01", "2026-02-01", "2026-03-01",
        ])
        assert current_streak(r, comps, date(2026, 3, 1)) == 3

    def test_streak_breaks_on_gap(self):
        r = _make_routine(cadence="daily")
        comps = self._completions([
            "2026-01-10", "2026-01-11",  # done
            # 2026-01-12 missing -> break
            "2026-01-13", "2026-01-14", "2026-01-15",  # done
        ])
        assert current_streak(r, comps, date(2026, 1, 15)) == 3


class TestLongestStreak:
    def _completions(self, dates: list[str], status: str = "done") -> list[RoutineCompletion]:
        return [
            RoutineCompletion(
                id=i + 1, routine_id=1, occurrence_date=d,
                status=status, at=datetime.now(), task_id=None,
            )
            for i, d in enumerate(dates)
        ]

    def test_empty(self):
        r = _make_routine(cadence="daily")
        assert longest_streak(r, []) == 0

    def test_single_run(self):
        r = _make_routine(cadence="daily")
        comps = self._completions([
            "2026-01-10", "2026-01-11", "2026-01-12",
        ])
        assert longest_streak(r, comps) == 3

    def test_longest_is_earlier_run(self):
        r = _make_routine(cadence="daily")
        comps = [
            RoutineCompletion(id=1, routine_id=1, occurrence_date="2026-01-10",
                              status="done", at=datetime.now(), task_id=None),
            RoutineCompletion(id=2, routine_id=1, occurrence_date="2026-01-11",
                              status="done", at=datetime.now(), task_id=None),
            RoutineCompletion(id=3, routine_id=1, occurrence_date="2026-01-12",
                              status="done", at=datetime.now(), task_id=None),
            RoutineCompletion(id=4, routine_id=1, occurrence_date="2026-01-13",
                              status="skipped", at=datetime.now(), task_id=None),
            RoutineCompletion(id=5, routine_id=1, occurrence_date="2026-01-14",
                              status="done", at=datetime.now(), task_id=None),
        ]
        assert longest_streak(r, comps) == 3

    def test_weekday_longest_streak(self):
        r = _make_routine(cadence="weekdays")
        # Two weeks of Mon-Fri done, then a break
        dates = []
        for week_offset in range(3):
            base = date(2026, 1, 5) + timedelta(weeks=week_offset)
            for d in range(5):
                dates.append((base + timedelta(days=d)).isoformat())
        # Skip week 3 (Sat Jan 17 / Sun 18 not scheduled anyway)
        # But break on Mon Jan 19
        comps = self._completions(dates)
        # Add a skipped Mon to break
        comps.append(RoutineCompletion(id=len(comps) + 1, routine_id=1,
                                       occurrence_date="2026-01-19",
                                       status="skipped", at=datetime.now(), task_id=None))
        # 2 full weeks = 10 consecutive weekday 'done's
        assert longest_streak(r, comps) == 10

    def test_monthly_longest_streak(self):
        r = _make_routine(cadence="monthly", day_of_month=15)
        comps = self._completions([
            "2026-01-15", "2026-02-15", "2026-03-15",
        ])
        assert longest_streak(r, comps) == 3


# ── RoutineEngine integration tests ──────────────────────────────────


class TestRoutineEngine:
    @pytest.fixture(autouse=True)
    def _setup(self, stores, store, delivered):
        self.stores = stores
        self.store = store
        self.delivered = delivered

        async def deliver(msg):
            delivered.append(msg)

        self.engine = RoutineEngine(
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            task_store=store,
            deliver=deliver,
            activity_store=stores["activity"],
        )

    def test_reminder_fires_at_due_time(self):
        self.stores["routines"].add(
            "Morning stretch", cadence="daily", time_of_day="07:00",
        )
        now = datetime(2026, 1, 15, 7, 0)
        events = self.engine.tick(now)
        assert len(events) == 1
        assert events[0].event_type == "reminder"
        assert events[0].delivered is True
        assert "Morning stretch" in events[0].message
        assert "done" in events[0].message.lower()
        assert "skip" in events[0].message.lower()

    def test_reminder_does_not_fire_before_due_time(self):
        self.stores["routines"].add(
            "Evening run", cadence="daily", time_of_day="18:00",
        )
        now = datetime(2026, 1, 15, 10, 0)
        events = self.engine.tick(now)
        assert len(events) == 0

    def test_dedupe_same_routine_same_day(self):
        self.stores["routines"].add(
            "Stretch", cadence="daily", time_of_day="07:00",
        )
        now = datetime(2026, 1, 15, 7, 0)
        events1 = self.engine.tick(now)
        assert len(events1) == 1
        events2 = self.engine.tick(now)
        assert len(events2) == 0  # deduplicated

    def test_missed_after_deadline(self):
        r = self.stores["routines"].add(
            "Meditate", cadence="daily",
            time_of_day="06:00", deadline_time="08:00",
        )
        # 09:00 — past deadline
        now = datetime(2026, 1, 15, 9, 0)
        events = self.engine.tick(now)
        assert len(events) == 1
        assert events[0].event_type == "missed"
        # Verify completion was written
        status = self.engine.get_completion_status(r.id, "2026-01-15")
        assert status == "missed"

    def test_missed_marked_even_when_delivery_blocked(self):
        r = self.stores["routines"].add(
            "Log", cadence="daily",
            time_of_day="06:00", deadline_time="08:00",
        )
        now = datetime(2026, 1, 15, 9, 0)
        events = self.engine.tick(now, allow_delivery=False)
        assert len(events) == 1
        assert events[0].event_type == "missed"
        status = self.engine.get_completion_status(r.id, "2026-01-15")
        assert status == "missed"

    def test_complete_today(self):
        r = self.stores["routines"].add("Stretch", cadence="daily")
        now = datetime(2026, 1, 15, 10, 0)
        event = self.engine.complete_today(r.id, now)
        assert event.event_type == "completed"
        status = self.engine.get_completion_status(r.id, "2026-01-15")
        assert status == "done"

    def test_skip_today(self):
        r = self.stores["routines"].add("Stretch", cadence="daily")
        now = datetime(2026, 1, 15, 10, 0)
        event = self.engine.skip_today(r.id, now)
        assert event.event_type == "skipped"
        status = self.engine.get_completion_status(r.id, "2026-01-15")
        assert status == "skipped"

    def test_completed_routine_skipped_by_tick(self):
        r = self.stores["routines"].add("Stretch", cadence="daily", time_of_day="07:00")
        now = datetime(2026, 1, 15, 10, 0)
        self.engine.complete_today(r.id, now)
        events = self.engine.tick(now)
        assert len(events) == 0  # already completed, no reminder

    def test_disabled_routine_ignored(self):
        r = self.stores["routines"].add("Disabled", cadence="daily", time_of_day="07:00")
        self.stores["routines"].toggle(r.id)  # disable
        now = datetime(2026, 1, 15, 10, 0)
        events = self.engine.tick(now)
        assert len(events) == 0

    def test_archived_routine_ignored(self):
        r = self.stores["routines"].add("Archived", cadence="daily", time_of_day="07:00")
        self.stores["routines"].archive(r.id)
        now = datetime(2026, 1, 15, 10, 0)
        events = self.engine.tick(now)
        assert len(events) == 0

    def test_weekday_routine_not_fired_on_saturday(self):
        self.stores["routines"].add(
            "Workout", cadence="weekdays", time_of_day="07:00",
        )
        # 2026-01-17 is Saturday
        now = datetime(2026, 1, 17, 10, 0)
        events = self.engine.tick(now)
        assert len(events) == 0

    def test_monthly_routine_only_fires_on_day(self):
        self.stores["routines"].add(
            "Pay rent", cadence="monthly", day_of_month=1, time_of_day="09:00",
        )
        # Jan 15 — not the 1st
        now = datetime(2026, 1, 15, 10, 0)
        events = self.engine.tick(now)
        assert len(events) == 0
        # Jan 1 — is the 1st
        now = datetime(2026, 1, 1, 10, 0)
        events = self.engine.tick(now)
        assert len(events) == 1
        assert events[0].event_type == "reminder"

    def test_activity_recorded_on_complete(self):
        r = self.stores["routines"].add("Stretch", cadence="daily")
        now = datetime(2026, 1, 15, 10, 0)
        self.engine.complete_today(r.id, now)
        recent = self.stores["activity"].recent()
        assert any(e.action == "routine_completed" for e in recent)

    def test_activity_recorded_on_missed(self):
        self.stores["routines"].add(
            "Meditate", cadence="daily",
            time_of_day="06:00", deadline_time="08:00",
        )
        now = datetime(2026, 1, 15, 9, 0)
        self.engine.tick(now)
        recent = self.stores["activity"].recent()
        assert any(e.action == "routine_missed" for e in recent)

    async def test_tick_async_delivers(self):
        self.stores["routines"].add("Async test", cadence="daily", time_of_day="07:00")
        now = datetime(2026, 1, 15, 7, 0)
        events = await self.engine.tick_async(now)
        assert len(events) == 1
        assert len(self.delivered) == 1
        assert "Async test" in self.delivered[0]


# ── Scheduler integration tests ──────────────────────────────────────


class TestSchedulerIntegration:
    """Test RoutineEngine integration with ReminderScheduler."""

    def test_scheduler_without_routine_engine(self, store, delivered):
        """Existing behavior preserved when no RoutineEngine passed."""
        async def deliver(msg):
            delivered.append(msg)

        from clipponyai.config import RemindersConfig

        sched = ReminderScheduler(store, RemindersConfig(), deliver)
        assert sched.routine_engine is None

    async def test_scheduler_with_routine_engine(self, stores, store, delivered):
        """RoutineEngine fires alongside scheduler."""
        from clipponyai.config import RemindersConfig

        async def deliver(msg):
            delivered.append(msg)

        engine = RoutineEngine(
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            task_store=store,
            deliver=deliver,
            activity_store=stores["activity"],
        )

        sched = ReminderScheduler(
            store,
            RemindersConfig(quiet_hours_start=0, quiet_hours_end=0),  # disabled
            deliver,
            routine_engine=engine,
        )

        stores["routines"].add("Morning routine", cadence="daily", time_of_day="07:00")

        now = datetime(2026, 1, 15, 7, 0)
        result = await sched.tick(now)
        # Routine reminder should be returned
        assert result is not None
        assert "Morning routine" in result

    async def test_quiet_hours_suppress_routine_delivery(self, stores, store, delivered):
        """Quiet hours block routine reminder delivery but allow missed marking."""
        from clipponyai.config import RemindersConfig

        async def deliver(msg):
            delivered.append(msg)

        engine = RoutineEngine(
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            task_store=store,
            deliver=deliver,
        )

        sched = ReminderScheduler(
            store,
            RemindersConfig(quiet_hours_start=23, quiet_hours_end=8),
            deliver,
            routine_engine=engine,
        )

        stores["routines"].add(
            "Night routine", cadence="daily",
            time_of_day="22:00", deadline_time="23:30",
        )

        # 23:45 — in quiet hours, past deadline
        now = datetime(2026, 1, 15, 23, 45)
        await sched.tick(now)
        # No delivery during quiet hours
        assert len(delivered) == 0
        # But missed state should still be marked
        r = stores["routines"].list_all()[0]
        status = engine.get_completion_status(r.id, "2026-01-15")
        assert status == "missed"

    async def test_quiet_hours_suppress_routine_reminder_delivery(self, stores, store, delivered):
        """Routine reminder at due time during quiet hours is not delivered."""
        from clipponyai.config import RemindersConfig

        async def deliver(msg):
            delivered.append(msg)

        engine = RoutineEngine(
            routine_store=stores["routines"],
            completion_store=stores["routine_completions"],
            task_store=store,
            deliver=deliver,
        )

        sched = ReminderScheduler(
            store,
            RemindersConfig(quiet_hours_start=23, quiet_hours_end=8),
            deliver,
            routine_engine=engine,
        )

        stores["routines"].add(
            "Late routine", cadence="daily", time_of_day="23:30",
        )

        now = datetime(2026, 1, 15, 23, 30)
        await sched.tick(now)
        # In quiet hours — no delivery
        assert len(delivered) == 0
        # But the scheduler still runs (returns None for ordinary nudges)
