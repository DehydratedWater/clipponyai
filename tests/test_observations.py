"""Tests for the structured screen-observation store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from clipponyai.accountability import _ensure_columns, get_stores
from clipponyai.tasks import TaskStore


@pytest.fixture
def store(tmp_path):
    task_store = TaskStore(tmp_path / "observations.db")
    yield task_store
    task_store.close()


@pytest.fixture
def observations(store):
    return get_stores(store)["observations"]


def test_record_roundtrips_every_field(observations):
    started = datetime(2026, 7, 28, 9, 30, 0)
    ended = started + timedelta(minutes=7)

    recorded = observations.record(
        started_at=started,
        ended_at=ended,
        source="vision",
        app="Terminal",
        window_title="pytest",
        category="work",
        activity="debugging a failing test",
        detail="assert expected == actual",
        idle_seconds=12,
        confidence=0.91,
        payload='{"screen":1}',
    )

    assert recorded.id > 0
    assert recorded.started_at == started
    assert recorded.ended_at == ended
    assert recorded.source == "vision"
    assert recorded.app == "Terminal"
    assert recorded.window_title == "pytest"
    assert recorded.category == "work"
    assert recorded.activity == "debugging a failing test"
    assert recorded.detail == "assert expected == actual"
    assert recorded.idle_seconds == 12
    assert recorded.confidence == pytest.approx(0.91)
    assert recorded.payload == '{"screen":1}'
    assert recorded.duration_seconds == 420


def test_extend_updates_end_without_creating_row(observations):
    started = datetime(2026, 7, 28, 10, 0, 0)
    recorded = observations.record(started_at=started, ended_at=started)

    observations.extend(recorded.id, started + timedelta(seconds=45))

    assert observations.count() == 1
    assert observations.latest().ended_at == started + timedelta(seconds=45)


def test_latest_optionally_filters_source(observations):
    at = datetime(2026, 7, 28, 10, 0, 0)
    os_row = observations.record(started_at=at, ended_at=at, source="os")
    vision_row = observations.record(
        started_at=at + timedelta(seconds=1),
        ended_at=at + timedelta(seconds=1),
        source="vision",
    )

    assert observations.latest().id == vision_row.id
    assert observations.latest(source="os").id == os_row.id
    assert observations.latest(source="missing") is None


def test_since_includes_boundary_and_returns_oldest_first(observations):
    cutoff = datetime(2026, 7, 28, 12, 0, 0)
    observations.record(
        started_at=cutoff - timedelta(seconds=1),
        ended_at=cutoff - timedelta(seconds=1),
    )
    boundary = observations.record(started_at=cutoff, ended_at=cutoff, source="os")
    later = observations.record(
        started_at=cutoff + timedelta(seconds=1),
        ended_at=cutoff + timedelta(seconds=1),
        source="vision",
    )

    assert [row.id for row in observations.since(cutoff)] == [boundary.id, later.id]
    assert [row.id for row in observations.since(cutoff, sources=("vision",))] == [later.id]


def test_recent_returns_selected_newest_rows_oldest_first(observations):
    at = datetime(2026, 7, 28, 13, 0, 0)
    rows = [
        observations.record(
            started_at=at + timedelta(seconds=index),
            ended_at=at + timedelta(seconds=index),
            source="vision" if index % 2 else "os",
        )
        for index in range(5)
    ]

    assert [row.id for row in observations.recent(3)] == [
        rows[2].id,
        rows[3].id,
        rows[4].id,
    ]
    assert [row.id for row in observations.recent(sources=("vision",))] == [
        rows[1].id,
        rows[3].id,
    ]


def test_age_pruning_runs_on_sweep(observations):
    now = datetime(2026, 7, 28, 14, 0, 0)
    old = now - timedelta(days=20)
    for _ in range(observations._PRUNE_EVERY - 1):
        observations.record(started_at=old, ended_at=old)

    current = observations.record(started_at=now, ended_at=now)

    assert observations.count() == 1
    assert observations.latest().id == current.id


def test_row_cap_is_enforced_at_each_sweep(observations):
    observations.max_rows = 200
    at = datetime(2026, 7, 28, 15, 0, 0)
    for index in range(observations.max_rows + 60):
        sample_at = at + timedelta(seconds=index)
        observations.record(started_at=sample_at, ended_at=sample_at)

    assert observations.count() == observations.max_rows + 10
    assert observations.recent(1)[0].started_at == at + timedelta(seconds=259)


def test_ensure_columns_preserves_existing_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO sample (value) VALUES ('kept')")

    _ensure_columns(conn, "sample", {"extra": "TEXT NOT NULL DEFAULT 'new'"})

    columns = {row[1] for row in conn.execute("PRAGMA table_info(sample)")}
    assert "extra" in columns
    assert conn.execute("SELECT value, extra FROM sample").fetchone() == ("kept", "new")
    conn.close()
