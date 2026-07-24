"""Tests for the accountability store (routines, goals, rules, activity, tokens).

Does not exercise scheduler/brain/GUI — pure store layer only.
"""

from __future__ import annotations

import pytest

from clipponyai.accountability import get_stores
from clipponyai.tasks import TaskStore


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "acct.db")
    yield s
    s.close()


@pytest.fixture
def stores(store):
    return get_stores(store)


# ── Schema & idempotency ──────────────────────────────────────────────


def test_fresh_schema_creates_all_tables(store):
    """get_stores() creates all accountability tables on a fresh db."""
    get_stores(store)
    tables = [
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    for expected in (
        "routines", "routine_completions", "goals", "goal_progress",
        "accountability_rules", "activity_log", "token_usage",
    ):
        assert expected in tables, f"missing table {expected}"


def test_reopen_idempotent(tmp_path):
    """Opening a second TaskStore on the same file sees all tables."""
    path = tmp_path / "acct.db"
    s1 = TaskStore(path)
    get_stores(s1)
    s1.close()

    s2 = TaskStore(path)
    stores2 = get_stores(s2)
    # write through first store, read through second
    r = stores2["routines"].add("Morning stretch")
    assert r.title == "Morning stretch"
    s2.close()


# ── Routine CRUD + JSON round-trip ────────────────────────────────────


def test_routine_add(stores):
    r = stores["routines"].add(
        "Morning stretch", cadence="weekdays",
        weekdays=[0, 1, 2, 3, 4], time_of_day="07:00",
    )
    assert r.title == "Morning stretch"
    assert r.cadence == "weekdays"
    assert r.weekdays == [0, 1, 2, 3, 4]
    assert r.time_of_day == "07:00"
    assert r.enabled is True


def test_routine_get(stores):
    r = stores["routines"].add("Run", cadence="daily")
    fetched = stores["routines"].get(r.id)
    assert fetched.id == r.id
    assert fetched.title == "Run"


def test_routine_json_roundtrip(stores):
    """weekdays must survive JSON serialization round-trip."""
    r = stores["routines"].add("Weekend chores", weekdays=[5, 6], cadence="daily")
    fetched = stores["routines"].get(r.id)
    assert fetched.weekdays == [5, 6]


def test_routine_list_all(stores):
    stores["routines"].add("A")
    stores["routines"].add("B")
    all_r = stores["routines"].list_all()
    assert len(all_r) == 2


def test_routine_update(stores):
    r = stores["routines"].add("Old title")
    updated = stores["routines"].update(r.id, title="New title", priority="high")
    assert updated.title == "New title"
    assert updated.priority == "high"


def test_routine_toggle(stores):
    r = stores["routines"].add("Toggle me")
    assert r.enabled
    toggled = stores["routines"].toggle(r.id)
    assert toggled.enabled is False
    toggled2 = stores["routines"].toggle(r.id)
    assert toggled2.enabled is True


def test_routine_archive_unarchive(stores):
    r = stores["routines"].add("Archive me")
    archived = stores["routines"].archive(r.id)
    assert archived.archived_at is not None
    assert stores["routines"].list_all() == []  # archived excluded
    assert len(stores["routines"].list_all(include_archived=True)) == 1
    unarchived = stores["routines"].unarchive(r.id)
    assert unarchived.archived_at is None
    assert len(stores["routines"].list_all()) == 1


def test_routine_get_missing(stores):
    with pytest.raises(KeyError):
        stores["routines"].get(9999)


# ── RoutineCompletion uniqueness / upsert ─────────────────────────────


def test_completion_upsert_insert(stores):
    r = stores["routines"].add("Stretch")
    c = stores["routine_completions"].upsert(r.id, "2026-01-01", status="done")
    assert c.status == "done"
    assert c.routine_id == r.id


def test_completion_upsert_update(stores):
    r = stores["routines"].add("Stretch")
    c1 = stores["routine_completions"].upsert(r.id, "2026-01-01", status="done")
    c2 = stores["routine_completions"].upsert(r.id, "2026-01-01", status="skipped")
    assert c2.status == "skipped"
    # same row id (upsert)
    assert c2.id == c1.id


def test_completion_by_routine(stores):
    r = stores["routines"].add("Stretch")
    stores["routine_completions"].upsert(r.id, "2026-01-01", status="done")
    stores["routine_completions"].upsert(r.id, "2026-01-02", status="done")
    completions = stores["routine_completions"].by_routine(r.id)
    assert len(completions) == 2
    assert completions[0].occurrence_date == "2026-01-02"  # desc order


def test_completion_by_date_range(stores):
    r = stores["routines"].add("Stretch")
    stores["routine_completions"].upsert(r.id, "2026-01-01", status="done")
    stores["routine_completions"].upsert(r.id, "2026-01-05", status="done")
    stores["routine_completions"].upsert(r.id, "2026-01-10", status="done")
    in_range = stores["routine_completions"].by_date_range("2026-01-01", "2026-01-07")
    assert len(in_range) == 2


# ── Goal CRUD / progress ──────────────────────────────────────────────


def test_goal_add(stores):
    g = stores["goals"].add("Read 10 books", target_count=10, linked_routine_ids=[1, 2])
    assert g.title == "Read 10 books"
    assert g.target_count == 10
    assert g.linked_routine_ids == [1, 2]
    assert g.status == "active"


def test_goal_json_roundtrip(stores):
    g = stores["goals"].add("Streak goal", linked_routine_ids=[3, 4, 5])
    fetched = stores["goals"].get(g.id)
    assert fetched.linked_routine_ids == [3, 4, 5]


def test_goal_achieve(stores):
    g = stores["goals"].add("Finish project")
    achieved = stores["goals"].achieve(g.id)
    assert achieved.status == "achieved"
    assert achieved.achieved_at is not None


def test_goal_cancel(stores):
    g = stores["goals"].add("Old goal")
    cancelled = stores["goals"].cancel(g.id)
    assert cancelled.status == "cancelled"


def test_goal_update(stores):
    g = stores["goals"].add("Update me")
    updated = stores["goals"].update(g.id, title="Updated title", target_count=5)
    assert updated.title == "Updated title"
    assert updated.target_count == 5


def test_goal_progress_upsert(stores):
    g = stores["goals"].add("Daily reading")
    p = stores["goal_progress"].upsert(g.id, "2026-01-01", met=1, note="ch 1")
    assert p.met == 1
    assert p.note == "ch 1"


def test_goal_progress_upsert_overwrite(stores):
    g = stores["goals"].add("Daily reading")
    p1 = stores["goal_progress"].upsert(g.id, "2026-01-01", met=0, note="missed")
    p2 = stores["goal_progress"].upsert(g.id, "2026-01-01", met=1, note="actually did it")
    assert p2.met == 1
    assert p2.id == p1.id


def test_goal_progress_by_goal(stores):
    g = stores["goals"].add("Daily reading")
    stores["goal_progress"].upsert(g.id, "2026-01-01", met=1)
    stores["goal_progress"].upsert(g.id, "2026-01-02", met=0)
    prog = stores["goal_progress"].by_goal(g.id)
    assert len(prog) == 2
    assert prog[0].date == "2026-01-02"


# ── AccountabilityRule CRUD / toggle ─────────────────────────────────


def test_rule_add(stores):
    rule = stores["rules"].add(
        "No phone after 11", rule_type="time",
        condition="hour > 23", message="Put the phone away!",
        cooldown_minutes=60,
    )
    assert rule.title == "No phone after 11"
    assert rule.rule_type == "time"
    assert rule.enabled is True
    assert rule.cooldown_minutes == 60


def test_rule_toggle(stores):
    rule = stores["rules"].add("Toggle rule")
    assert rule.enabled
    toggled = stores["rules"].toggle(rule.id)
    assert toggled.enabled is False
    toggled2 = stores["rules"].toggle(rule.id)
    assert toggled2.enabled is True


def test_rule_record_fire(stores):
    rule = stores["rules"].add("Fire me")
    assert rule.last_fired_at is None
    fired = stores["rules"].record_fire(rule.id)
    assert fired.last_fired_at is not None


def test_rule_update(stores):
    rule = stores["rules"].add("Update rule")
    updated = stores["rules"].update(rule.id, title="New rule name", cooldown_minutes=120)
    assert updated.title == "New rule name"
    assert updated.cooldown_minutes == 120


def test_rule_list_all(stores):
    stores["rules"].add("Rule A")
    stores["rules"].add("Rule B")
    assert len(stores["rules"].list_all()) == 2


# ── Activity log 200 retention ────────────────────────────────────────


def test_activity_record(stores):
    entry = stores["activity"].record("routine_completed", actor="user", detail="morning stretch")
    assert entry.action == "routine_completed"
    assert entry.actor == "user"


def test_activity_recent(stores):
    stores["activity"].record("action_a")
    stores["activity"].record("action_b")
    recent = stores["activity"].recent()
    assert len(recent) == 2
    assert recent[0].action == "action_a"
    assert recent[1].action == "action_b"


def test_activity_recent_can_exclude_actions(stores):
    stores["activity"].record("routine_completed", detail="morning stretch")
    stores["activity"].record("screen_assessed", detail="reason=The user is on Reddit")
    stores["activity"].record("task_added", detail="taxes")
    recent = stores["activity"].recent(exclude_actions={"screen_assessed"})
    assert [e.action for e in recent] == ["routine_completed", "task_added"]


def test_activity_200_retention(stores):
    """Insert 210 entries and verify exactly 200 remain (oldest pruned)."""
    for i in range(210):
        stores["activity"].record(f"action_{i}")
    recent = stores["activity"].recent(limit=300)
    assert len(recent) == 200
    # oldest should be action_10 (0-9 pruned)
    assert recent[0].action == "action_10"
    assert recent[-1].action == "action_209"


def test_activity_prune_exact_200(stores):
    """Insert exactly 200 — nothing pruned."""
    for i in range(200):
        stores["activity"].record(f"act_{i}")
    recent = stores["activity"].recent(limit=300)
    assert len(recent) == 200
    assert recent[0].action == "act_0"


# ── Token usage summary ───────────────────────────────────────────────


def test_token_record(stores):
    t = stores["token_usage"].record(
        lane="chat", purpose="user_query",
        provider="openai", model="gpt-4",
        prompt_tokens=100, completion_tokens=200,
    )
    assert t.total_tokens == 300
    assert t.lane == "chat"


def test_token_recent(stores):
    stores["token_usage"].record(lane="chat", prompt_tokens=10)
    stores["token_usage"].record(lane="sensor", prompt_tokens=20)
    recent = stores["token_usage"].recent()
    assert len(recent) == 2


def test_token_summary_all(stores):
    stores["token_usage"].record(lane="chat", prompt_tokens=100, completion_tokens=50)
    stores["token_usage"].record(lane="chat", prompt_tokens=200, completion_tokens=100)
    stores["token_usage"].record(lane="sensor", prompt_tokens=30, completion_tokens=10)
    summary = stores["token_usage"].summary("all")
    by_lane = {s["lane"]: s for s in summary}
    assert by_lane["chat"]["total_tokens"] == 450
    assert by_lane["chat"]["count"] == 2
    assert by_lane["sensor"]["total_tokens"] == 40
    assert by_lane["sensor"]["count"] == 1


def test_token_summary_today(stores):
    stores["token_usage"].record(lane="chat", prompt_tokens=50, completion_tokens=25)
    summary = stores["token_usage"].summary("today")
    assert len(summary) >= 1
    chat_row = [s for s in summary if s["lane"] == "chat"]
    assert len(chat_row) == 1
    assert chat_row[0]["total_tokens"] == 75


def test_token_summary_empty(stores):
    summary = stores["token_usage"].summary("all")
    assert summary == []


def test_token_summary_7d(stores):
    stores["token_usage"].record(lane="chat", prompt_tokens=10, completion_tokens=5)
    summary = stores["token_usage"].summary("7d")
    assert len(summary) >= 1
