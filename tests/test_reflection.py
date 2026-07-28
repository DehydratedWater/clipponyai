"""Tests for the periodic reflection gate stack and lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from clipponyai.accountability import get_stores
from clipponyai.config import Config
from clipponyai.reflection import (
    _META_LAST_RUN,
    _META_LAST_SPOKE,
    ReflectionEngine,
)
from clipponyai.tasks import TaskStore

NOW = datetime(2026, 7, 28, 12, 0)


class FakeQuestioner:
    def __init__(self, silenced=False):
        self.silenced = silenced

    def is_silenced(self, now=None):
        return self.silenced


@pytest.fixture
def reflection_setup(tmp_path):
    store = TaskStore(tmp_path / "reflection.db")
    stores = get_stores(store)
    config = Config()
    config.reminders.quiet_hours_start = 22
    config.reminders.quiet_hours_end = 8
    delivered = []
    reflected = []
    result = {"value": "Worth saying."}

    async def reflect(context):
        reflected.append(context)
        value = result["value"]
        if isinstance(value, Exception):
            raise value
        return value

    async def deliver(text, *, source):
        delivered.append((text, source))

    def make_engine(**kwargs):
        return ReflectionEngine(
            config,
            store,
            stores["observations"],
            stores["activity"],
            reflect_fn=kwargs.pop("reflect_fn", reflect),
            deliver=kwargs.pop("deliver", deliver),
            questioner=kwargs.pop("questioner", FakeQuestioner()),
            clock=kwargs.pop("clock", lambda: NOW),
            **kwargs,
        )

    yield {
        "store": store,
        "stores": stores,
        "config": config,
        "delivered": delivered,
        "reflected": reflected,
        "result": result,
        "make_engine": make_engine,
    }
    store.close()


def add_observation(setup, *, at=NOW, category="work"):
    return setup["stores"]["observations"].record(
        started_at=at,
        ended_at=at,
        app="Editor",
        category=category,
        activity="editing tests",
        confidence=1.0,
    )


async def test_disabled_gate_skips_turn(reflection_setup):
    reflection_setup["config"].reflection.enabled = False
    add_observation(reflection_setup)

    await reflection_setup["make_engine"]()._tick()

    assert reflection_setup["reflected"] == []


async def test_quiet_hours_gate_skips_turn(reflection_setup):
    add_observation(reflection_setup)
    engine = reflection_setup["make_engine"](clock=lambda: NOW.replace(hour=23))

    await engine._tick()

    assert reflection_setup["reflected"] == []


async def test_user_mute_gate_skips_turn(reflection_setup):
    add_observation(reflection_setup)
    engine = reflection_setup["make_engine"](questioner=FakeQuestioner(silenced=True))

    await engine._tick()

    assert reflection_setup["reflected"] == []


async def test_speak_budget_gate_survives_restart(reflection_setup):
    add_observation(reflection_setup)
    first = reflection_setup["make_engine"]()
    await first._tick()
    add_observation(reflection_setup, at=NOW + timedelta(minutes=1))

    second = reflection_setup["make_engine"](clock=lambda: NOW + timedelta(minutes=1))
    await second._tick()

    assert reflection_setup["delivered"] == [("Worth saying.", "reflection")]
    assert len(reflection_setup["reflected"]) == 1


async def test_recent_nudge_gate_skips_turn(reflection_setup):
    now = datetime.now().replace(microsecond=0)
    add_observation(reflection_setup, at=now)
    reflection_setup["store"].save_message("assistant", "A reminder", source="reminder")
    engine = reflection_setup["make_engine"](clock=lambda: now)

    await engine._tick()

    assert reflection_setup["reflected"] == []


async def test_no_substance_gate_skips_turn(reflection_setup):
    await reflection_setup["make_engine"]()._tick()

    assert reflection_setup["reflected"] == []
    assert reflection_setup["store"].get_meta(_META_LAST_RUN) is None


async def test_idle_latest_observation_gate_skips_turn(reflection_setup):
    add_observation(reflection_setup, category="idle")

    await reflection_setup["make_engine"]()._tick()

    assert reflection_setup["reflected"] == []


@pytest.mark.parametrize(
    "value",
    [None, "", "SILENT", "silent.", " SILENT ", "SILENT…"],
)
async def test_silent_outputs_do_not_deliver_or_write_activity(
    reflection_setup,
    value,
):
    add_observation(reflection_setup)
    reflection_setup["result"]["value"] = value

    await reflection_setup["make_engine"]()._tick()

    assert reflection_setup["delivered"] == []
    assert reflection_setup["store"].get_meta(_META_LAST_RUN) is not None
    assert reflection_setup["store"].get_meta(_META_LAST_SPOKE) is None
    assert reflection_setup["stores"]["activity"].recent() == []


async def test_real_text_delivers_and_records_only_metadata(reflection_setup):
    add_observation(reflection_setup)

    await reflection_setup["make_engine"]()._tick()

    assert reflection_setup["delivered"] == [("Worth saying.", "reflection")]
    assert reflection_setup["store"].get_meta(_META_LAST_SPOKE) == NOW.isoformat()
    rows = reflection_setup["stores"]["activity"].recent()
    assert [row.action for row in rows] == ["reflection_spoke"]
    assert "length=13" in rows[0].detail
    assert "Worth saying" not in rows[0].detail


async def test_reflect_failure_is_recorded_and_next_tick_can_run(reflection_setup):
    add_observation(reflection_setup)
    reflection_setup["result"]["value"] = RuntimeError("provider unavailable")
    engine = reflection_setup["make_engine"]()

    await engine._tick()
    add_observation(reflection_setup, at=NOW + timedelta(minutes=1))
    reflection_setup["result"]["value"] = "Recovered."
    engine.clock = lambda: NOW + timedelta(minutes=1)
    await engine._tick()

    assert reflection_setup["delivered"] == [("Recovered.", "reflection")]
    actions = [row.action for row in reflection_setup["stores"]["activity"].recent()]
    assert actions == ["reflection_failed", "reflection_spoke"]


async def test_observation_disabled_omits_digest_but_still_reflects(reflection_setup):
    reflection_setup["config"].observation.enabled = False
    reflection_setup["stores"]["activity"].record("task_added", detail="Task #1")

    await reflection_setup["make_engine"]()._tick()

    assert len(reflection_setup["reflected"]) == 1
    assert "Screen activity log" not in reflection_setup["reflected"][0]
    assert reflection_setup["delivered"] == [("Worth saying.", "reflection")]


async def test_context_contains_grounded_sections(reflection_setup):
    reflection_setup["config"].observation.enabled = True
    add_observation(reflection_setup)
    reflection_setup["store"].add("Send report")
    reflection_setup["stores"]["activity"].record("task_added", detail="Send report")

    await reflection_setup["make_engine"]()._tick()

    context = reflection_setup["reflected"][0]
    assert "It is Tuesday 12:00." in context
    assert "Screen activity log" in context
    assert "Pending tasks:" in context
    assert "Send report" in context
    assert "What you have done recently:" in context


async def test_lifecycle_is_idempotent_refreshes_and_stops(reflection_setup):
    engine = reflection_setup["make_engine"]()

    await engine.start()
    first_task = engine._task
    await engine.start()
    assert engine._task is first_task

    reflection_setup["config"].reflection.interval_minutes = 25
    await engine.refresh()
    assert engine._task is not first_task
    assert not first_task or first_task.cancelled()

    await engine.stop()
    assert engine._task is None
