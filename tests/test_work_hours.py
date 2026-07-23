"""Focused work-hours feature: config validation, meta persistence, closing nudge, quiet-hours precedence."""

from datetime import datetime, timedelta

import pytest

from clipponyai.config import Config, RemindersConfig, WorkHoursConfig
from clipponyai.scheduler import (
    ReminderScheduler,
    closing_due,
    in_work_hours,
)
from clipponyai.tasks import TaskStore

# ── helpers ──────────────────────────────────────────────────────────

# Wednesday 2026-07-22
WED = datetime(2026, 7, 22)
# Saturday 2026-07-25
SAT = datetime(2026, 7, 25)


def _wh(**overrides) -> WorkHoursConfig:
    kw = dict(enabled=True, start="09:00", end="17:00", weekdays=[0, 1, 2, 3, 4])
    kw.update(overrides)
    return WorkHoursConfig(**kw)


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def delivered():
    return []


@pytest.fixture
def wh_scheduler(store, delivered):
    wh = _wh()
    async def deliver(msg):
        delivered.append(msg)
    return ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)


# ── WorkHoursConfig validation ───────────────────────────────────────

class TestWorkHoursConfigValidation:
    def test_defaults(self):
        wh = WorkHoursConfig()
        assert wh.enabled is False
        assert wh.start == "09:00"
        assert wh.end == "17:00"
        assert wh.weekdays == [0, 1, 2, 3, 4]
        assert wh.closing_nudge is True
        assert wh.suppress_off_hours is False

    def test_valid_times(self):
        wh = WorkHoursConfig(start="08:30", end="18:00")
        assert wh.start == "08:30"
        assert wh.end == "18:00"

    def test_midnight_times(self):
        wh = WorkHoursConfig(start="00:00", end="23:59")
        assert wh.start == "00:00"
        assert wh.end == "23:59"

    def test_bad_start_format(self):
        with pytest.raises(ValueError, match="HH:MM"):
            WorkHoursConfig(start="9am")

    def test_bad_end_format(self):
        with pytest.raises(ValueError, match="HH:MM"):
            WorkHoursConfig(end="17")

    def test_hour_out_of_range(self):
        with pytest.raises(ValueError, match="HH:MM"):
            WorkHoursConfig(start="25:00")

    def test_minute_out_of_range(self):
        with pytest.raises(ValueError, match="HH:MM"):
            WorkHoursConfig(end="17:60")

    def test_weekday_out_of_range(self):
        with pytest.raises(ValueError, match="0.*6"):
            WorkHoursConfig(weekdays=[0, 7])

    def test_negative_weekday(self):
        with pytest.raises(ValueError, match="0.*6"):
            WorkHoursConfig(weekdays=[-1])

    def test_dedup_and_sort_weekdays(self):
        wh = WorkHoursConfig(enabled=True, weekdays=[4, 2, 0, 2])
        assert wh.weekdays == [0, 2, 4]

    def test_non_string_start(self):
        with pytest.raises((ValueError, TypeError)):
            WorkHoursConfig(start=900)

    def test_non_string_end(self):
        with pytest.raises((ValueError, TypeError)):
            WorkHoursConfig(end=1700)

    def test_non_integer_weekday(self):
        with pytest.raises((ValueError, TypeError)):
            WorkHoursConfig(weekdays=["mon"])

    def test_enabled_true(self):
        wh = WorkHoursConfig(enabled=True)
        assert wh.enabled is True

    def test_all_weekdays(self):
        wh = WorkHoursConfig(enabled=True, weekdays=[0, 1, 2, 3, 4, 5, 6])
        assert wh.weekdays == [0, 1, 2, 3, 4, 5, 6]


# ── in_work_hours pure function ──────────────────────────────────────

class TestInWorkHours:
    def test_disabled_returns_false(self):
        wh = _wh(enabled=False)
        assert in_work_hours(WED.replace(hour=12), wh) is False

    def test_within_hours(self):
        wh = _wh()
        assert in_work_hours(WED.replace(hour=10), wh) is True
        assert in_work_hours(WED.replace(hour=16, minute=59), wh) is True

    def test_before_start(self):
        wh = _wh()
        assert in_work_hours(WED.replace(hour=8, minute=59), wh) is False

    def test_after_end(self):
        wh = _wh()
        assert in_work_hours(WED.replace(hour=17), wh) is False

    def test_at_start_inclusive(self):
        wh = _wh()
        assert in_work_hours(WED.replace(hour=9), wh) is True

    def test_at_end_exclusive(self):
        wh = _wh()
        assert in_work_hours(WED.replace(hour=17), wh) is False

    def test_weekday_boundary_monday(self):
        wh = _wh()
        monday = datetime(2026, 7, 20, 12)  # Monday
        assert in_work_hours(monday, wh) is True

    def test_weekday_boundary_friday(self):
        wh = _wh()
        friday = datetime(2026, 7, 24, 12)  # Friday
        assert in_work_hours(friday, wh) is True

    def test_saturday_not_workday(self):
        wh = _wh()
        assert in_work_hours(SAT.replace(hour=12), wh) is False

    def test_sunday_not_workday(self):
        wh = _wh()
        sunday = datetime(2026, 7, 26, 12)
        assert in_work_hours(sunday, wh) is False

    def test_custom_weekdays_include_saturday(self):
        wh = _wh(weekdays=[0, 1, 2, 3, 4, 5])
        assert in_work_hours(SAT.replace(hour=12), wh) is True

    def test_overnight_window(self):
        wh = _wh(start="22:00", end="06:00")
        assert in_work_hours(WED.replace(hour=23), wh) is True
        assert in_work_hours(WED.replace(hour=3), wh) is True
        assert in_work_hours(WED.replace(hour=12), wh) is False
        assert in_work_hours(WED.replace(hour=21), wh) is False


# ── closing_due pure function ────────────────────────────────────────

class TestClosingDue:
    def test_disabled(self):
        wh = _wh(enabled=False)
        assert closing_due(WED.replace(hour=17), wh) is False

    def test_closing_nudge_off(self):
        wh = _wh(closing_nudge=False)
        assert closing_due(WED.replace(hour=17), wh) is False

    def test_during_end_hour(self):
        wh = _wh()
        assert closing_due(WED.replace(hour=17, minute=0), wh) is True
        assert closing_due(WED.replace(hour=17, minute=30), wh) is True
        assert closing_due(WED.replace(hour=17, minute=59), wh) is True

    def test_before_end_hour(self):
        wh = _wh()
        assert closing_due(WED.replace(hour=16, minute=59), wh) is False

    def test_after_end_hour(self):
        wh = _wh()
        assert closing_due(WED.replace(hour=18), wh) is False

    def test_weekend_no_closing(self):
        wh = _wh()
        assert closing_due(SAT.replace(hour=17), wh) is False

    def test_custom_end_hour(self):
        wh = _wh(end="18:00")
        assert closing_due(WED.replace(hour=18), wh) is True
        assert closing_due(WED.replace(hour=17), wh) is False


# ── TaskStore meta table ─────────────────────────────────────────────

class TestMetaStore:
    def test_get_default(self, store):
        assert store.get_meta("nonexistent") is None
        assert store.get_meta("nonexistent", "fallback") == "fallback"

    def test_set_and_get(self, store):
        store.set_meta("key1", "value1")
        assert store.get_meta("key1") == "value1"

    def test_overwrite(self, store):
        store.set_meta("key1", "old")
        store.set_meta("key1", "new")
        assert store.get_meta("key1") == "new"

    def test_multiple_keys(self, store):
        store.set_meta("a", "1")
        store.set_meta("b", "2")
        assert store.get_meta("a") == "1"
        assert store.get_meta("b") == "2"

    def test_idempotent_recreate(self, tmp_path):
        """Opening a fresh connection to an existing db sees meta values."""
        path = tmp_path / "persist.db"
        s1 = TaskStore(path)
        s1.set_meta("closing_nudge_2026-07-22", "done")
        s1.close()
        s2 = TaskStore(path)
        assert s2.get_meta("closing_nudge_2026-07-22") == "done"
        s2.close()


# ── ReminderScheduler closing nudge integration ──────────────────────

class TestClosingNudgeIntegration:
    async def test_closing_nudge_fires_at_end_of_day(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("finish report", deadline=WED + timedelta(hours=2))
        msg = await sched.tick(WED.replace(hour=17, minute=5))
        assert msg is not None
        assert "End of workday" in msg
        assert "finish report" in msg

    async def test_closing_nudge_once_per_day(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("task one")
        await sched.tick(WED.replace(hour=17, minute=5))
        # second tick same day — should be suppressed
        msg2 = await sched.tick(WED.replace(hour=17, minute=30))
        assert msg2 is None
        assert len(delivered) == 1

    async def test_closing_nudge_fires_next_day(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("task one")
        await sched.tick(WED.replace(hour=17, minute=5))
        # Thursday
        thu = datetime(2026, 7, 23)
        msg2 = await sched.tick(thu.replace(hour=17, minute=5))
        assert msg2 is not None
        assert "End of workday" in msg2

    async def test_closing_nudge_no_pending_tasks(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        msg = await sched.tick(WED.replace(hour=17, minute=5))
        assert msg is None
        assert delivered == []

    async def test_closing_nudge_quiet_hours_override(self, store, delivered):
        wh = _wh(end="23:00")
        cfg = RemindersConfig(quiet_hours_start=22, quiet_hours_end=6)
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, cfg, deliver, work_hours=wh)
        store.add("task one")
        # 23:00 is closing time but also inside quiet hours (22-6)
        msg = await sched.tick(WED.replace(hour=23, minute=5))
        assert msg is None

    async def test_closing_nudge_disabled(self, store, delivered):
        wh = _wh(closing_nudge=False)
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("task one")
        msg = await sched.tick(WED.replace(hour=17, minute=5))
        assert msg is None

    async def test_closing_nudge_weekend_no_fire(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("task one")
        msg = await sched.tick(SAT.replace(hour=17, minute=5))
        assert msg is None

    async def test_closing_nudge_lists_real_pending_tasks(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("report", deadline=WED + timedelta(hours=1))
        store.add("email boss", deadline=WED + timedelta(hours=2))
        done_task, _ = store.add("already done")
        store.complete(done_task)
        msg = await sched.tick(WED.replace(hour=17, minute=5))
        assert msg is not None
        assert "report" in msg
        assert "email boss" in msg
        assert "already done" not in msg


# ── suppress_off_hours ───────────────────────────────────────────────

class TestSuppressOffHours:
    async def test_suppress_outside_work_hours(self, store, delivered):
        wh = _wh(suppress_off_hours=True)
        cfg = RemindersConfig()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, cfg, deliver, work_hours=wh)
        store.add("water plants", deadline=WED - timedelta(hours=1))
        # 20:00 is outside 09:00-17:00
        msg = await sched.tick(WED.replace(hour=20))
        assert msg is None
        assert delivered == []

    async def test_no_suppress_inside_work_hours(self, store, delivered):
        wh = _wh(suppress_off_hours=True)
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("water plants", deadline=WED - timedelta(hours=1))
        msg = await sched.tick(WED.replace(hour=12))
        assert msg is not None
        assert "water plants" in msg

    async def test_suppress_disabled_by_default(self, store, delivered):
        wh = _wh(suppress_off_hours=False)
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("water plants", deadline=WED - timedelta(hours=1))
        msg = await sched.tick(WED.replace(hour=20))
        assert msg is not None

    async def test_suppress_still_drops_exhausted_tasks(self, store, delivered):
        wh = _wh(suppress_off_hours=True)
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        task, _ = store.add("hopeless", deadline=WED - timedelta(days=3))
        with store._lock:
            store._conn.execute(
                "UPDATE tasks SET nudge_count=8, last_nudge_at=? WHERE id=?",
                ((WED - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"), task.id),
            )
            store._conn.commit()
        msg = await sched.tick(WED.replace(hour=20))
        assert msg is None  # suppressed
        assert store.get(task.id).status == "dropped"
        assert any("stopped reminding" in m for m in delivered)


# ── quiet hours precedence over everything ───────────────────────────

class TestQuietHoursPrecedence:
    async def test_quiet_hours_block_ordinary_nudges(self, store, delivered):
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver, work_hours=wh)
        store.add("water plants", deadline=WED - timedelta(hours=1))
        msg = await sched.tick(WED.replace(hour=23, minute=30))
        assert msg is None

    async def test_quiet_hours_block_closing_nudge(self, store, delivered):
        wh = _wh(end="23:00")
        cfg = RemindersConfig(quiet_hours_start=22, quiet_hours_end=6)
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, cfg, deliver, work_hours=wh)
        store.add("task one")
        msg = await sched.tick(WED.replace(hour=23, minute=5))
        assert msg is None


# ── backward compatibility ───────────────────────────────────────────

class TestBackwardCompatibility:
    async def test_scheduler_without_work_hours(self, store, delivered):
        """Existing constructor call still works (no work_hours param)."""
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(store, RemindersConfig(), deliver)
        store.add("water plants", deadline=WED - timedelta(hours=1))
        msg = await sched.tick(WED.replace(hour=12))
        assert msg is not None
        assert "water plants" in msg

    def test_reminders_config_has_work_hours_default(self):
        cfg = RemindersConfig()
        assert isinstance(cfg.work_hours, WorkHoursConfig)
        assert cfg.work_hours.enabled is False

    def test_config_load_has_work_hours(self):
        cfg = Config()
        assert isinstance(cfg.reminders.work_hours, WorkHoursConfig)


# ── end-to-end: closing nudge persistence across store reopen ────────

class TestClosingPersistence:
    async def test_meta_survives_store_reopen(self, tmp_path, delivered):
        path = tmp_path / "persist.db"
        s = TaskStore(path)
        wh = _wh()
        async def deliver(msg):
            delivered.append(msg)
        sched = ReminderScheduler(s, RemindersConfig(), deliver, work_hours=wh)
        s.add("task one")
        msg = await sched.tick(WED.replace(hour=17, minute=5))
        assert msg is not None
        # Close and reopen
        s.close()
        delivered.clear()
        s2 = TaskStore(path)
        sched2 = ReminderScheduler(s2, RemindersConfig(), deliver, work_hours=wh)
        msg2 = await sched2.tick(WED.replace(hour=17, minute=30))
        assert msg2 is None  # already fired
        s2.close()
