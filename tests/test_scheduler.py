from datetime import datetime, timedelta

import pytest

from clipponyai.config import RemindersConfig
from clipponyai.scheduler import ReminderScheduler, in_quiet_hours

DAY = datetime(2026, 7, 22, 14, 0)
NIGHT = datetime(2026, 7, 22, 23, 30)


def test_quiet_hours_cross_midnight():
    assert in_quiet_hours(NIGHT, 23, 8)
    assert in_quiet_hours(datetime(2026, 7, 22, 3, 0), 23, 8)
    assert not in_quiet_hours(DAY, 23, 8)


def test_quiet_hours_same_day_range():
    assert in_quiet_hours(DAY, 13, 15)
    assert not in_quiet_hours(DAY, 15, 18)


def test_quiet_hours_disabled_when_equal():
    assert not in_quiet_hours(NIGHT, 8, 8)


@pytest.fixture
def delivered():
    return []


@pytest.fixture
def scheduler(store, delivered):
    async def deliver(msg):
        delivered.append(msg)

    return ReminderScheduler(store, RemindersConfig(), deliver)


async def test_tick_nudges_due_task(store, scheduler, delivered):
    store.add("water plants", deadline=DAY - timedelta(minutes=10))
    msg = await scheduler.tick(DAY)
    assert msg and "water plants" in msg
    assert delivered == [msg]
    assert store.pending()[0].nudge_count == 1


async def test_tick_respects_gap_between_nudges(store, scheduler, delivered):
    store.add("water plants", deadline=DAY - timedelta(minutes=10))
    await scheduler.tick(DAY)
    assert await scheduler.tick(DAY + timedelta(minutes=5)) is None  # inside 30m gap
    assert len(delivered) == 1
    assert (await scheduler.tick(DAY + timedelta(minutes=31))) is not None


async def test_tick_quiet_at_night(store, scheduler, delivered):
    store.add("water plants", deadline=NIGHT - timedelta(hours=1))
    assert await scheduler.tick(NIGHT) is None
    assert delivered == []
    # morning comes, the nudge fires
    assert (await scheduler.tick(datetime(2026, 7, 23, 8, 30))) is not None


async def test_tick_drops_exhausted_task_with_notice(store, scheduler, delivered):
    task, _ = store.add("hopeless thing", deadline=DAY - timedelta(days=3))
    with store._lock:
        store._conn.execute(
            "UPDATE tasks SET nudge_count=8, last_nudge_at=? WHERE id=?",
            ((DAY - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"), task.id),
        )
        store._conn.commit()
    await scheduler.tick(DAY)
    assert store.get(task.id).status == "dropped"
    assert any("stopped reminding" in m for m in delivered)


async def test_tick_disabled(store, delivered):
    async def deliver(msg):
        delivered.append(msg)

    sched = ReminderScheduler(store, RemindersConfig(enabled=False), deliver)
    store.add("water plants", deadline=DAY - timedelta(minutes=10))
    assert await sched.tick(DAY) is None and delivered == []


async def test_tick_nothing_due(store, scheduler, delivered):
    store.add("future", deadline=DAY + timedelta(days=1))
    assert await scheduler.tick(DAY) is None and delivered == []
