from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from clipponyai import observer
from clipponyai.accountability import get_stores
from clipponyai.config import Config
from clipponyai.observer import ObservationRecorder
from clipponyai.screen_context import ForegroundContext
from clipponyai.tasks import TaskStore


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 7, 28, 8, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class ContextSequence:
    def __init__(self, *contexts):
        self.contexts = list(contexts)
        self.capture_window_titles = []

    def __call__(self, *, capture_window_titles):
        self.capture_window_titles.append(capture_window_titles)
        return self.contexts.pop(0)


def context(
    app="Editor",
    *,
    title="notes.md",
    idle_seconds=0.0,
):
    return ForegroundContext(
        app=app,
        bundle_id=f"example.{app.lower()}",
        window_title=title,
        idle_seconds=idle_seconds,
    )


@pytest.fixture
def observation_store(tmp_path):
    task_store = TaskStore(tmp_path / "observer.db")
    yield get_stores(task_store)["observations"]
    task_store.close()


@pytest.fixture
def config():
    value = Config()
    value.observation.enabled = True
    return value


async def test_gate_off_records_nothing_and_closes_open_episode(config, observation_store):
    clock = FakeClock()
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context()),
        clock=clock,
    )
    await recorder._tick()
    clock.advance(20)
    config.observation.enabled = False

    await recorder._tick()

    assert observation_store.count() == 1
    assert observation_store.latest().ended_at == clock.now
    assert recorder._current is None


async def test_same_foreground_extends_one_episode(config, observation_store):
    clock = FakeClock()
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context(), context()),
        clock=clock,
    )
    await recorder._tick()
    clock.advance(15)
    await recorder._tick()

    assert observation_store.count() == 1
    assert observation_store.latest().ended_at == clock.now


async def test_foreground_change_starts_second_episode(config, observation_store):
    clock = FakeClock()
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context(), context("Browser", title="Docs")),
        clock=clock,
    )
    await recorder._tick()
    first_end = observation_store.latest().ended_at
    clock.advance(15)
    await recorder._tick()

    rows = observation_store.recent(10)
    assert len(rows) == 2
    assert rows[0].ended_at == first_end
    assert rows[1].app == "Browser"


async def test_idle_context_keeps_application(config, observation_store):
    config.observation.idle_threshold_seconds = 30
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context(idle_seconds=30)),
        clock=FakeClock(),
    )

    await recorder._tick()

    row = observation_store.latest()
    assert row.category == "idle"
    assert row.app == "Editor"


async def test_return_after_idle_gap_starts_new_episode(config, observation_store):
    config.observation.idle_threshold_seconds = 30
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(
            context(),
            context(idle_seconds=30),
            context(),
        ),
        clock=FakeClock(),
    )

    await recorder._tick()
    await recorder._tick()
    await recorder._tick()

    assert [row.category for row in observation_store.recent(10)] == [
        "unknown",
        "idle",
        "unknown",
    ]


async def test_missing_context_leaves_current_episode_untouched(config, observation_store):
    clock = FakeClock()
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context(), None),
        clock=clock,
    )
    await recorder._tick()
    first = observation_store.latest()
    clock.advance(15)

    await recorder._tick()

    assert observation_store.count() == 1
    assert observation_store.latest().ended_at == first.ended_at
    assert recorder._current is not None


async def test_redaction_is_applied_before_persisting(config, observation_store):
    config.observation.redact_patterns = [r"\d+"]
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context(title="Account 1234")),
        clock=FakeClock(),
    )

    await recorder._tick()

    assert observation_store.latest().window_title == "Account ***"


async def test_titles_are_not_read_or_persisted_when_disabled(config, observation_store):
    config.observation.capture_window_titles = False
    contexts = ContextSequence(context(title="secret"))
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=contexts,
        clock=FakeClock(),
    )

    await recorder._tick()

    assert contexts.capture_window_titles == [False]
    assert observation_store.latest().window_title == ""


async def test_stop_closes_open_episode(config, observation_store):
    clock = FakeClock()
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context()),
        clock=clock,
    )
    await recorder._tick()
    clock.advance(23)

    await recorder.stop()

    assert observation_store.latest().ended_at == clock.now
    assert recorder._current is None


async def test_start_is_idempotent(config, observation_store):
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(),
        clock=FakeClock(),
    )

    await recorder.start()
    task = recorder._task
    await recorder.start()

    assert recorder._task is task
    await recorder.stop()


async def test_loop_rereads_sample_interval_after_refresh(monkeypatch, config, observation_store):
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(),
        clock=FakeClock(),
    )
    seen_timeouts = []

    async def fake_wait_for(waiter, *, timeout):
        waiter.close()
        seen_timeouts.append(timeout)
        if len(seen_timeouts) == 1:
            raise TimeoutError
        recorder._stop.set()
        return True

    async def change_interval():
        config.observation.sample_seconds = 5
        await recorder.refresh()

    monkeypatch.setattr(observer.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(recorder, "_tick", change_interval)

    await recorder.start()
    await recorder._task

    assert seen_timeouts == [15, 5]
    await recorder.stop()


def test_default_clock_is_naive_local_like_the_rest_of_the_stack():
    """The OS spine must share one timeline with awareness and the stores.

    A tz-aware UTC clock here is silently dropped by the shared SQLite timestamp
    format, offsetting OS episodes from vision rows by the local UTC offset. That
    breaks the digest's containment fold and hides OS rows from reflection.
    """
    from clipponyai.awareness import _RealClock

    sampled = observer._now()
    assert sampled.tzinfo is None
    assert abs((sampled - _RealClock().now()).total_seconds()) < 5


@pytest.mark.asyncio
async def test_default_clock_records_episodes_on_the_local_timeline(config, observation_store):
    """An episode recorded with the production clock must read back as 'now'."""
    recorder = ObservationRecorder(
        config,
        observation_store,
        context_fn=ContextSequence(context()),
    )

    await recorder._tick()

    recorded = observation_store.latest()
    assert recorded is not None
    assert recorded.source == "os"
    assert recorded.started_at.tzinfo is None
    drift = abs((recorded.started_at - datetime.now()).total_seconds())
    assert drift < 60, f"OS episode is {drift:.0f}s off the local timeline"
