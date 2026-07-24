from datetime import datetime, timedelta

from clipponyai.tasks import (
    DROP_NOTICE, Task, compose_nudge, content_tokens, nudge_state,
)

NOW = datetime(2026, 7, 22, 14, 0)
GAPS = [30, 60, 120, 240, 360]


# ── add / dedup ───────────────────────────────────────────────────────
def test_add_and_get(store):
    task, created = store.add("email the CEO about holidays", deadline=NOW)
    assert created and task.id > 0
    assert store.get(task.id).title == "email the CEO about holidays"


def test_near_duplicate_titles_merge(store):
    first, created1 = store.add("Email CEO about Sept holiday")
    second, created2 = store.add("email the CEO about the Sept holiday")
    assert created1 and not created2
    assert second.id == first.id
    assert len(store.pending()) == 1


def test_duplicate_with_new_deadline_updates_existing(store):
    first, _ = store.add("buy oat milk")
    merged, created = store.add("buy the oat milk", deadline=NOW + timedelta(hours=2))
    assert not created and merged.id == first.id
    assert merged.deadline == NOW + timedelta(hours=2)


def test_different_tasks_do_not_merge(store):
    store.add("email CEO about holidays")
    _, created = store.add("water the plants")
    assert created
    assert len(store.pending()) == 2


def test_empty_title_rejected(store):
    import pytest

    with pytest.raises(ValueError):
        store.add("   ")


# ── resolution ────────────────────────────────────────────────────────
def test_resolve_by_id_forms(store):
    task, _ = store.add("water the plants")
    assert store.resolve(f"#{task.id}")[0].id == task.id
    assert store.resolve(str(task.id))[0].id == task.id


def test_resolve_by_text(store):
    store.add("email CEO about holidays")
    target, _ = store.add("water the plants")
    task, candidates = store.resolve("water plants")
    assert task.id == target.id and candidates == []


def test_resolve_ambiguous_returns_candidates(store):
    store.add("email CEO about holidays")
    store.add("email CEO about the offsite")
    task, candidates = store.resolve("email CEO")
    assert task is None
    assert len(candidates) == 2


def test_resolve_no_match(store):
    store.add("water the plants")
    assert store.resolve("fly to the moon") == (None, [])


def test_resolve_completed_id_is_not_pending(store):
    task, _ = store.add("water the plants")
    store.complete(task)
    assert store.resolve(f"#{task.id}") == (None, [])


# ── status changes / restore / audit ─────────────────────────────────
def test_complete_sets_timestamp_and_logs(store):
    task, _ = store.add("water the plants")
    done = store.complete(task, actor="pony")
    assert done.status == "done" and done.completed_at is not None
    log_rows = store._conn.execute(
        "SELECT old_status, new_status, actor FROM task_log WHERE task_id=? ORDER BY id",
        (task.id,),
    ).fetchall()
    assert [tuple(r) for r in log_rows] == [(None, "pending", "user"), ("pending", "done", "pony")]


def test_restore_dropped_task(store):
    task, _ = store.add("call the dentist")
    store.drop(task)
    revived = store.restore("dentist")
    assert revived is not None and revived.status == "pending"
    assert revived.nudge_count == 0


def test_restore_no_match(store):
    task, _ = store.add("call the dentist")
    store.drop(task)
    assert store.restore("completely unrelated words") is None


def test_snooze_resets_nudge_trail(store):
    task, _ = store.add("water plants", deadline=NOW - timedelta(hours=1))
    store.record_nudge([task], NOW)
    snoozed = store.snooze(store.get(task.id), NOW + timedelta(hours=3))
    assert snoozed.nudge_count == 0 and snoozed.remind_at == NOW + timedelta(hours=3)
    assert nudge_state(snoozed, NOW, GAPS, 8) == "wait"


# ── nudge cadence (pure function) ────────────────────────────────────
def _task(**kw):
    defaults = dict(id=1, title="water plants", deadline=NOW - timedelta(minutes=5))
    defaults.update(kw)
    return Task(**defaults)


def test_nudge_waits_before_due():
    assert nudge_state(_task(deadline=NOW + timedelta(hours=1)), NOW, GAPS, 8) == "wait"


def test_nudge_due_when_deadline_passed():
    assert nudge_state(_task(), NOW, GAPS, 8) == "due"


def test_nudge_escalating_gaps():
    task = _task(nudge_count=1, last_nudge_at=NOW - timedelta(minutes=29))
    assert nudge_state(task, NOW, GAPS, 8) == "wait"  # 30m gap after 1st ping
    task = _task(nudge_count=1, last_nudge_at=NOW - timedelta(minutes=31))
    assert nudge_state(task, NOW, GAPS, 8) == "due"
    task = _task(nudge_count=3, last_nudge_at=NOW - timedelta(minutes=119))
    assert nudge_state(task, NOW, GAPS, 8) == "wait"  # 120m gap after 3rd
    task = _task(nudge_count=5, last_nudge_at=NOW - timedelta(minutes=361))
    assert nudge_state(task, NOW, GAPS, 8) == "due"  # last gap repeats


def test_nudge_drop_after_max():
    task = _task(nudge_count=8, last_nudge_at=NOW - timedelta(hours=10))
    assert nudge_state(task, NOW, GAPS, 8) == "drop"


def test_remind_at_takes_priority_over_deadline():
    task = _task(deadline=NOW - timedelta(hours=2), remind_at=NOW + timedelta(hours=1))
    assert nudge_state(task, NOW, GAPS, 8) == "wait"


def test_done_task_never_nudges():
    assert nudge_state(_task(status="done"), NOW, GAPS, 8) == "wait"


def test_undated_task_never_nudges():
    assert nudge_state(_task(deadline=None), NOW, GAPS, 8) == "wait"


def test_due_for_nudge_store_roundtrip(store):
    overdue, _ = store.add("water plants", deadline=NOW - timedelta(minutes=10))
    store.add("future thing", deadline=NOW + timedelta(days=1))
    tired, _ = store.add("old thing", deadline=NOW - timedelta(days=2))
    with store._lock:
        store._conn.execute(
            "UPDATE tasks SET nudge_count=8, last_nudge_at=? WHERE id=?",
            ((NOW - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"), tired.id),
        )
        store._conn.commit()
    due, to_drop = store.due_for_nudge(NOW, GAPS, 8)
    assert [t.id for t in due] == [overdue.id]
    assert [t.id for t in to_drop] == [tired.id]


# ── nudge messages ────────────────────────────────────────────────────
def test_compose_nudge_escalates_and_batches():
    tasks = [
        _task(id=1, title="water plants", nudge_count=0),
        _task(id=2, title="call dentist", nudge_count=2),
        _task(id=3, title="pay rent", nudge_count=7),
        _task(id=4, title="extra one"),
    ]
    msg = compose_nudge(tasks, batch_limit=3)
    assert "water plants" in msg and "did it happen?" in msg
    assert "ping #3 on \"call dentist\"" in msg
    assert "ping #8" in msg  # last template repeats with real count
    assert "extra one" not in msg and "+1 more" in msg


def test_drop_notice_mentions_restore():
    assert "restore" in DROP_NOTICE.format(t="water plants")


# ── overview ──────────────────────────────────────────────────────────
def test_overview_sections_verbatim(store):
    store.add("overdue thing", deadline=NOW - timedelta(hours=3))
    store.add("today thing", deadline=NOW + timedelta(hours=2))
    store.add("this week thing", deadline=NOW + timedelta(days=3))
    store.add("far future thing", deadline=NOW + timedelta(days=30))
    store.add("someday thing")
    dead, _ = store.add("dead thing")
    store.drop(dead)
    text = store.overview(NOW)
    assert "🔴 Overdue" in text and "overdue thing" in text
    assert "📌 Today" in text and "today thing" in text
    assert "📅 Upcoming (7d)" in text and "this week thing" in text
    assert "📆 Later" in text and "far future thing" in text
    assert "🗂 No deadline" in text and "someday thing" in text
    assert "🪦" in text and "dead thing" in text


def test_overview_empty(store):
    assert "nothing tracked" in store.overview(NOW)


# ── message history ───────────────────────────────────────────────────
def test_message_history_roundtrip(store):
    store.save_message("user", "hello", "desktop")
    store.save_message("assistant", "hi!", "desktop")
    store.save_message("user", "from phone", "telegram")
    assert store.recent_messages(2) == [
        {"role": "assistant", "content": "hi!"},
        {"role": "user", "content": "from phone"},
    ]


def test_message_history_can_carry_the_source(store):
    store.save_message("assistant", "still pending: taxes", "reminder")
    store.save_message("user", "done", "desktop")
    assert store.recent_messages(2, with_source=True) == [
        {"role": "assistant", "content": "still pending: taxes", "source": "reminder"},
        {"role": "user", "content": "done", "source": "desktop"},
    ]


def test_content_tokens_drop_stopwords():
    assert content_tokens("email the CEO about the holidays") == {"email", "ceo", "holidays"}
