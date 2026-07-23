"""Accountability domain — routines, goals, rules, activity log, token usage.

Additive module: does NOT alter the Task dataclass or the tasks table.
Reuses the existing TaskStore SQLite connection and lock for thread safety.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .tasks import ISO, TaskStore

# ─── Dataclasses ─────────────────────────────────────────────────────


@dataclass
class Routine:
    id: int
    title: str
    notes: str
    cadence: str  # daily | weekdays | monthly
    weekdays: list[int]  # 0=Mon .. 6=Sun
    time_of_day: str | None  # "HH:MM"
    day_of_month: int | None
    deadline_time: str | None  # "HH:MM" (must be done by this time)
    priority: str  # low | medium | high
    enabled: bool
    created_at: datetime
    archived_at: datetime | None


@dataclass
class RoutineCompletion:
    id: int
    routine_id: int
    occurrence_date: str  # "YYYY-MM-DD"
    status: str  # done | skipped | missed
    at: datetime
    task_id: int | None


@dataclass
class Goal:
    id: int
    title: str
    description: str
    condition: str  # e.g. "routine_completions.done.count >= target"
    target_count: int | None
    target_streak: int | None
    linked_routine_ids: list[int]
    status: str  # active | achieved | cancelled
    created_at: datetime
    achieved_at: datetime | None


@dataclass
class GoalProgress:
    id: int
    goal_id: int
    date: str  # "YYYY-MM-DD"
    met: int  # 0 or 1
    note: str


@dataclass
class AccountabilityRule:
    id: int
    title: str
    rule_type: str  # time | screen | custom
    condition: str
    message: str
    enabled: bool
    cooldown_minutes: int
    last_fired_at: datetime | None
    created_at: datetime


@dataclass
class ActivityLog:
    id: int
    at: datetime
    actor: str
    action: str
    detail: str
    ref_type: str | None
    ref_id: str | None


@dataclass
class TokenUsage:
    id: int
    at: datetime
    lane: str
    purpose: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated: int  # 1 = cost was estimated, 0 = exact


# ─── JSON helpers ────────────────────────────────────────────────────

def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _json_loads(s: str | None) -> Any:
    if not s:
        return None
    return json.loads(s)


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime(ISO) if dt else None


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, ISO)


# ─── Schema migration ────────────────────────────────────────────────

_ACCOUNTABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    cadence TEXT NOT NULL DEFAULT 'daily',
    weekdays TEXT NOT NULL DEFAULT '[]',
    time_of_day TEXT,
    day_of_month INTEGER,
    deadline_time TEXT,
    priority TEXT NOT NULL DEFAULT 'medium',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS routine_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id INTEGER NOT NULL,
    occurrence_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',
    at TEXT NOT NULL,
    task_id INTEGER,
    UNIQUE(routine_id, occurrence_date)
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL DEFAULT '',
    target_count INTEGER,
    target_streak INTEGER,
    linked_routine_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    achieved_at TEXT
);

CREATE TABLE IF NOT EXISTS goal_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    met INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(goal_id, date)
);

CREATE TABLE IF NOT EXISTS accountability_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    rule_type TEXT NOT NULL DEFAULT 'custom',
    condition TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    cooldown_minutes INTEGER NOT NULL DEFAULT 0,
    last_fired_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ref_type TEXT,
    ref_id TEXT
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT 'chat',
    purpose TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create accountability tables if missing. Idempotent."""
    conn.executescript(_ACCOUNTABILITY_SCHEMA)
    conn.commit()


# ─── Store classes ───────────────────────────────────────────────────


class RoutineStore:
    """CRUD for routines, backed by the TaskStore connection/lock."""

    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    # -- row conversion --
    @staticmethod
    def _row(row: sqlite3.Row) -> Routine:
        return Routine(
            id=row["id"],
            title=row["title"],
            notes=row["notes"],
            cadence=row["cadence"],
            weekdays=_json_loads(row["weekdays"]) or [],
            time_of_day=row["time_of_day"],
            day_of_month=row["day_of_month"],
            deadline_time=row["deadline_time"],
            priority=row["priority"],
            enabled=bool(row["enabled"]),
            created_at=_parse(row["created_at"]),
            archived_at=_parse(row["archived_at"]),
        )

    # -- CRUD --
    def add(
        self,
        title: str,
        *,
        notes: str = "",
        cadence: str = "daily",
        weekdays: list[int] | None = None,
        time_of_day: str | None = None,
        day_of_month: int | None = None,
        deadline_time: str | None = None,
        priority: str = "medium",
        enabled: bool = True,
    ) -> Routine:
        if weekdays is None:
            weekdays = []
        with self._store._lock:
            now = datetime.now().strftime(ISO)
            cur = self._store._conn.execute(
                "INSERT INTO routines (title, notes, cadence, weekdays, time_of_day, "
                "day_of_month, deadline_time, priority, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title, notes, cadence, _json_dumps(weekdays), time_of_day,
                    day_of_month, deadline_time, priority, int(enabled), now,
                ),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM routines WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def get(self, routine_id: int) -> Routine:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM routines WHERE id=?", (routine_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no routine #{routine_id}")
            return self._row(row)

    def list_all(self, *, include_archived: bool = False) -> list[Routine]:
        with self._store._lock:
            if include_archived:
                rows = self._store._conn.execute(
                    "SELECT * FROM routines ORDER BY id"
                ).fetchall()
            else:
                rows = self._store._conn.execute(
                    "SELECT * FROM routines WHERE archived_at IS NULL ORDER BY id"
                ).fetchall()
            return [self._row(r) for r in rows]

    def update(
        self,
        routine_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        cadence: str | None = None,
        weekdays: list[int] | None = None,
        time_of_day: str | None = None,
        day_of_month: int | None = None,
        deadline_time: str | None = None,
        priority: str | None = None,
        enabled: bool | None = None,
    ) -> Routine:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM routines WHERE id=?", (routine_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no routine #{routine_id}")
            updates: list[str] = []
            vals: list = []
            if title is not None:
                updates.append("title=?")
                vals.append(title)
            if notes is not None:
                updates.append("notes=?")
                vals.append(notes)
            if cadence is not None:
                updates.append("cadence=?")
                vals.append(cadence)
            if weekdays is not None:
                updates.append("weekdays=?")
                vals.append(_json_dumps(weekdays))
            if time_of_day is not None:
                updates.append("time_of_day=?")
                vals.append(time_of_day)
            if day_of_month is not None:
                updates.append("day_of_month=?")
                vals.append(day_of_month)
            if deadline_time is not None:
                updates.append("deadline_time=?")
                vals.append(deadline_time)
            if priority is not None:
                updates.append("priority=?")
                vals.append(priority)
            if enabled is not None:
                updates.append("enabled=?")
                vals.append(int(enabled))
            if not updates:
                return self._row(row)
            vals.append(routine_id)
            self._store._conn.execute(
                f"UPDATE routines SET {', '.join(updates)} WHERE id=?", vals
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM routines WHERE id=?", (routine_id,)
                ).fetchone()
            )

    def toggle(self, routine_id: int) -> Routine:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM routines WHERE id=?", (routine_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no routine #{routine_id}")
            new_enabled = 0 if row["enabled"] else 1
            self._store._conn.execute(
                "UPDATE routines SET enabled=? WHERE id=?", (new_enabled, routine_id)
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM routines WHERE id=?", (routine_id,)
                ).fetchone()
            )

    def archive(self, routine_id: int) -> Routine:
        now = datetime.now().strftime(ISO)
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE routines SET archived_at=? WHERE id=?", (now, routine_id)
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM routines WHERE id=?", (routine_id,)
                ).fetchone()
            )

    def unarchive(self, routine_id: int) -> Routine:
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE routines SET archived_at=NULL WHERE id=?", (routine_id,)
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM routines WHERE id=?", (routine_id,)
                ).fetchone()
            )


class RoutineCompletionStore:
    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    @staticmethod
    def _row(row: sqlite3.Row) -> RoutineCompletion:
        return RoutineCompletion(
            id=row["id"],
            routine_id=row["routine_id"],
            occurrence_date=row["occurrence_date"],
            status=row["status"],
            at=_parse(row["at"]),
            task_id=row["task_id"],
        )

    def upsert(
        self,
        routine_id: int,
        occurrence_date: str,
        *,
        status: str = "done",
        task_id: int | None = None,
    ) -> RoutineCompletion:
        now = datetime.now().strftime(ISO)
        with self._store._lock:
            self._store._conn.execute(
                "INSERT INTO routine_completions "
                "(routine_id, occurrence_date, status, at, task_id) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(routine_id, occurrence_date) "
                "DO UPDATE SET status=excluded.status, "
                "at=excluded.at, task_id=excluded.task_id",
                (routine_id, occurrence_date, status, now, task_id),
            )
            self._store._conn.commit()
            row = self._store._conn.execute(
                "SELECT * FROM routine_completions "
                "WHERE routine_id=? AND occurrence_date=?",
                (routine_id, occurrence_date),
            ).fetchone()
            return self._row(row)

    def by_routine(self, routine_id: int) -> list[RoutineCompletion]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM routine_completions "
                "WHERE routine_id=? "
                "ORDER BY occurrence_date DESC",
                (routine_id,),
            ).fetchall()
            return [self._row(r) for r in rows]

    def by_date_range(
        self, start_date: str, end_date: str
    ) -> list[RoutineCompletion]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM routine_completions "
                "WHERE occurrence_date >= ? AND occurrence_date <= ? "
                "ORDER BY occurrence_date DESC",
                (start_date, end_date),
            ).fetchall()
            return [self._row(r) for r in rows]


class GoalStore:
    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    @staticmethod
    def _row(row: sqlite3.Row) -> Goal:
        return Goal(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            condition=row["condition"],
            target_count=row["target_count"],
            target_streak=row["target_streak"],
            linked_routine_ids=_json_loads(row["linked_routine_ids"]) or [],
            status=row["status"],
            created_at=_parse(row["created_at"]),
            achieved_at=_parse(row["achieved_at"]),
        )

    def add(
        self,
        title: str,
        *,
        description: str = "",
        condition: str = "",
        target_count: int | None = None,
        target_streak: int | None = None,
        linked_routine_ids: list[int] | None = None,
    ) -> Goal:
        if linked_routine_ids is None:
            linked_routine_ids = []
        with self._store._lock:
            now = datetime.now().strftime(ISO)
            cur = self._store._conn.execute(
                "INSERT INTO goals (title, description, condition, target_count, "
                "target_streak, linked_routine_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    title, description, condition, target_count, target_streak,
                    _json_dumps(linked_routine_ids), now,
                ),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM goals WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def get(self, goal_id: int) -> Goal:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no goal #{goal_id}")
            return self._row(row)

    def list_all(self) -> list[Goal]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM goals ORDER BY id"
            ).fetchall()
            return [self._row(r) for r in rows]

    def update(
        self,
        goal_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        condition: str | None = None,
        target_count: int | None = None,
        target_streak: int | None = None,
        linked_routine_ids: list[int] | None = None,
    ) -> Goal:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no goal #{goal_id}")
            updates: list[str] = []
            vals: list = []
            if title is not None:
                updates.append("title=?")
                vals.append(title)
            if description is not None:
                updates.append("description=?")
                vals.append(description)
            if condition is not None:
                updates.append("condition=?")
                vals.append(condition)
            if target_count is not None:
                updates.append("target_count=?")
                vals.append(target_count)
            if target_streak is not None:
                updates.append("target_streak=?")
                vals.append(target_streak)
            if linked_routine_ids is not None:
                updates.append("linked_routine_ids=?")
                vals.append(_json_dumps(linked_routine_ids))
            if not updates:
                return self._row(row)
            vals.append(goal_id)
            self._store._conn.execute(
                f"UPDATE goals SET {', '.join(updates)} WHERE id=?", vals
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM goals WHERE id=?", (goal_id,)
                ).fetchone()
            )

    def achieve(self, goal_id: int) -> Goal:
        now = datetime.now().strftime(ISO)
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE goals SET status='achieved', achieved_at=? WHERE id=?",
                (now, goal_id),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM goals WHERE id=?", (goal_id,)
                ).fetchone()
            )

    def reopen(self, goal_id: int) -> Goal:
        """Reset an achieved goal back to active so it can be retracked."""
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE goals SET status='active', achieved_at=NULL WHERE id=?",
                (goal_id,),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM goals WHERE id=?", (goal_id,)
                ).fetchone()
            )

    def set_links(self, goal_id: int, linked_routine_ids: list[int]) -> Goal:
        """Replace the linked routine IDs for a goal."""
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no goal #{goal_id}")
            self._store._conn.execute(
                "UPDATE goals SET linked_routine_ids=? WHERE id=?",
                (_json_dumps(linked_routine_ids), goal_id),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM goals WHERE id=?", (goal_id,)
                ).fetchone()
            )

    def cancel(self, goal_id: int) -> Goal:
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE goals SET status='cancelled' WHERE id=?", (goal_id,)
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM goals WHERE id=?", (goal_id,)
                ).fetchone()
            )


class GoalProgressStore:
    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    @staticmethod
    def _row(row: sqlite3.Row) -> GoalProgress:
        return GoalProgress(
            id=row["id"],
            goal_id=row["goal_id"],
            date=row["date"],
            met=row["met"],
            note=row["note"],
        )

    def upsert(
        self,
        goal_id: int,
        date: str,
        *,
        met: int = 1,
        note: str = "",
    ) -> GoalProgress:
        with self._store._lock:
            self._store._conn.execute(
                "INSERT INTO goal_progress (goal_id, date, met, note) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(goal_id, date) "
                "DO UPDATE SET met=excluded.met, note=excluded.note",
                (goal_id, date, met, note),
            )
            self._store._conn.commit()
            row = self._store._conn.execute(
                "SELECT * FROM goal_progress WHERE goal_id=? AND date=?",
                (goal_id, date),
            ).fetchone()
            return self._row(row)

    def by_goal(self, goal_id: int) -> list[GoalProgress]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM goal_progress WHERE goal_id=? ORDER BY date DESC",
                (goal_id,),
            ).fetchall()
            return [self._row(r) for r in rows]


class AccountabilityRuleStore:
    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    @staticmethod
    def _row(row: sqlite3.Row) -> AccountabilityRule:
        return AccountabilityRule(
            id=row["id"],
            title=row["title"],
            rule_type=row["rule_type"],
            condition=row["condition"],
            message=row["message"],
            enabled=bool(row["enabled"]),
            cooldown_minutes=row["cooldown_minutes"],
            last_fired_at=_parse(row["last_fired_at"]),
            created_at=_parse(row["created_at"]),
        )

    def add(
        self,
        title: str,
        *,
        rule_type: str = "custom",
        condition: str = "",
        message: str = "",
        cooldown_minutes: int = 0,
        enabled: bool = True,
    ) -> AccountabilityRule:
        with self._store._lock:
            now = datetime.now().strftime(ISO)
            cur = self._store._conn.execute(
                "INSERT INTO accountability_rules (title, rule_type, condition, message, "
                "enabled, cooldown_minutes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, rule_type, condition, message, int(enabled), cooldown_minutes, now),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM accountability_rules WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def get(self, rule_id: int) -> AccountabilityRule:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM accountability_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no rule #{rule_id}")
            return self._row(row)

    def list_all(self) -> list[AccountabilityRule]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM accountability_rules ORDER BY id"
            ).fetchall()
            return [self._row(r) for r in rows]

    def update(
        self,
        rule_id: int,
        *,
        title: str | None = None,
        rule_type: str | None = None,
        condition: str | None = None,
        message: str | None = None,
        cooldown_minutes: int | None = None,
    ) -> AccountabilityRule:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM accountability_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no rule #{rule_id}")
            updates: list[str] = []
            vals: list = []
            if title is not None:
                updates.append("title=?")
                vals.append(title)
            if rule_type is not None:
                updates.append("rule_type=?")
                vals.append(rule_type)
            if condition is not None:
                updates.append("condition=?")
                vals.append(condition)
            if message is not None:
                updates.append("message=?")
                vals.append(message)
            if cooldown_minutes is not None:
                updates.append("cooldown_minutes=?")
                vals.append(cooldown_minutes)
            if not updates:
                return self._row(row)
            vals.append(rule_id)
            self._store._conn.execute(
                f"UPDATE accountability_rules SET {', '.join(updates)} WHERE id=?", vals
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM accountability_rules WHERE id=?", (rule_id,)
                ).fetchone()
            )

    def toggle(self, rule_id: int) -> AccountabilityRule:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT * FROM accountability_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no rule #{rule_id}")
            new_enabled = 0 if row["enabled"] else 1
            self._store._conn.execute(
                "UPDATE accountability_rules SET enabled=? WHERE id=?",
                (new_enabled, rule_id),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM accountability_rules WHERE id=?", (rule_id,)
                ).fetchone()
            )

    def record_fire(self, rule_id: int) -> AccountabilityRule:
        now = datetime.now().strftime(ISO)
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE accountability_rules SET last_fired_at=? WHERE id=?",
                (now, rule_id),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM accountability_rules WHERE id=?", (rule_id,)
                ).fetchone()
            )


class ActivityStore:
    """Activity log with hard 200-entry retention."""

    MAX_ENTRIES = 200

    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    @staticmethod
    def _row(row: sqlite3.Row) -> ActivityLog:
        return ActivityLog(
            id=row["id"],
            at=_parse(row["at"]),
            actor=row["actor"],
            action=row["action"],
            detail=row["detail"],
            ref_type=row["ref_type"],
            ref_id=row["ref_id"],
        )

    def record(
        self,
        action: str,
        *,
        actor: str = "system",
        detail: str = "",
        ref_type: str | None = None,
        ref_id: str | None = None,
    ) -> ActivityLog:
        now = datetime.now().strftime(ISO)
        with self._store._lock:
            cur = self._store._conn.execute(
                "INSERT INTO activity_log (at, actor, action, detail, ref_type, ref_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, actor, action, detail, ref_type, ref_id),
            )
            self._prune_locked()
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM activity_log WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def recent(self, limit: int = 50) -> list[ActivityLog]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def _prune_locked(self) -> None:
        """Delete oldest rows beyond MAX_ENTRIES. Must hold lock."""
        excess = (
            self._store._conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
            - self.MAX_ENTRIES
        )
        if excess > 0:
            self._store._conn.execute(
                "DELETE FROM activity_log WHERE id IN "
                "(SELECT id FROM activity_log ORDER BY id ASC LIMIT ?)",
                (excess,),
            )


class TokenUsageStore:
    def __init__(self, task_store: TaskStore) -> None:
        self._store = task_store

    @staticmethod
    def _row(row: sqlite3.Row) -> TokenUsage:
        return TokenUsage(
            id=row["id"],
            at=_parse(row["at"]),
            lane=row["lane"],
            purpose=row["purpose"],
            provider=row["provider"],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            estimated=bool(row["estimated"]),
        )

    def record(
        self,
        *,
        lane: str = "chat",
        purpose: str = "",
        provider: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        estimated: bool = False,
    ) -> TokenUsage:
        total = prompt_tokens + completion_tokens
        now = datetime.now().strftime(ISO)
        with self._store._lock:
            cur = self._store._conn.execute(
                "INSERT INTO token_usage (at, lane, purpose, provider, model, "
                "prompt_tokens, completion_tokens, total_tokens, estimated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, lane, purpose, provider, model,
                    prompt_tokens, completion_tokens, total, int(estimated),
                ),
            )
            self._store._conn.commit()
            return self._row(
                self._store._conn.execute(
                    "SELECT * FROM token_usage WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def recent(self, limit: int = 50) -> list[TokenUsage]:
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM token_usage ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def summary(
        self,
        period: str = "all",
    ) -> list[dict]:
        """Return token usage totals grouped by lane.

        period: 'today', '7d', or 'all'.
        Each dict: {lane, prompt_tokens, completion_tokens, total_tokens, count}.
        """
        if period == "today":
            today = datetime.now().strftime("%Y-%m-%d")
            where = "WHERE date(at) = ?"
            params = (today,)
        elif period == "7d":
            seven = (datetime.now().strftime("%Y-%m-%d"))
            where = "WHERE at >= datetime(?, '-7 days')"
            params = (seven,)
        else:
            where = ""
            params = ()

        with self._store._lock:
            rows = self._store._conn.execute(
                f"SELECT lane, "
                f"SUM(prompt_tokens) AS prompt_tokens, "
                f"SUM(completion_tokens) AS completion_tokens, "
                f"SUM(total_tokens) AS total_tokens, "
                f"COUNT(*) AS count "
                f"FROM token_usage {where} "
                f"GROUP BY lane ORDER BY total_tokens DESC",
                params,
            ).fetchall()
        return [
            {
                "lane": r["lane"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "total_tokens": r["total_tokens"],
                "count": r["count"],
            }
            for r in rows
        ]


# ─── Convenience factory ─────────────────────────────────────────────

def get_stores(task_store: TaskStore) -> dict[str, Any]:
    """Ensure schema and return all accountability store instances."""
    with task_store._lock:
        _ensure_schema(task_store._conn)
    return {
        "routines": RoutineStore(task_store),
        "routine_completions": RoutineCompletionStore(task_store),
        "goals": GoalStore(task_store),
        "goal_progress": GoalProgressStore(task_store),
        "rules": AccountabilityRuleStore(task_store),
        "activity": ActivityStore(task_store),
        "token_usage": TokenUsageStore(task_store),
    }
