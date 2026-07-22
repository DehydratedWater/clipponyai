"""Tasks, reminders and the nudge engine (SQLite, no LLM anywhere near it).

Distilled from the fren v4 orchestrator's commitment tracker, with its
hard-won rules kept intact:

- near-duplicate pending titles collapse onto the existing task (token-set
  Jaccard >= 0.75) instead of piling up;
- tasks are resolved by TITLE text, never by model-invented ids — ambiguity
  returns candidates so the caller can ask, and ids shown in listings work
  too;
- nudges escalate on a fixed cadence (gaps after the nth ping), cap at
  max_nudges, then the task is dropped with a "say restore" notice instead of
  nagging forever;
- "I did it" adjudication lives in the brain as a small LLM sensor (language
  agnostic), but every completion it proposes is grounded here against real
  rows before anything changes;
- every status change is recorded in a task_log audit table;
- listings are rendered verbatim from the database — if the list is long,
  the list IS long.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ISO = "%Y-%m-%d %H:%M:%S"

_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "at", "with",
    "my", "me", "i", "it", "this", "that", "some", "about", "up", "out", "do",
    "go", "get", "być", "się", "na", "do", "w", "z", "i", "o", "że", "to",
}

# escalating nudge templates, indexed by ping number (last repeats)
NUDGE_TEMPLATES = (
    '⏰ "{t}" — did it happen? tell me "done", or give me a time and I\'ll come back then.',
    'still open: "{t}". "done" closes it, "skip it" drops it, a time snoozes it — otherwise I keep pinging.',
    'ping #3 on "{t}" — not letting this one slide. "done" / "skip it" / a new time.',
    '"{t}" is still hanging (ping #{n}). say the word: "done", "skip it", or when.',
)
DROP_NOTICE = '⚰️ I\'ve stopped reminding you about "{t}". say "restore {t}" if it\'s still alive.'


def content_tokens(text: str) -> set[str]:
    """Mechanical token grounding (NOT language classification — that's the
    LLM sensor's job). Used only to check that a proposed match shares real
    words with a real row, and to collapse near-duplicate titles."""
    words = re.findall(r"[\w']+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


_tokens = content_tokens  # internal alias


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime(ISO) if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.strptime(s, ISO) if s else None


@dataclass
class Task:
    id: int
    title: str
    notes: str = ""
    status: str = "pending"  # pending | done | dropped | cancelled
    priority: str = "medium"  # low | medium | high
    source: str = "user"  # user | commitment (auto-detected promise)
    deadline: datetime | None = None
    remind_at: datetime | None = None  # first nudge time; falls back to deadline
    created_at: datetime | None = None
    completed_at: datetime | None = None
    nudge_count: int = 0
    last_nudge_at: datetime | None = None

    @property
    def due_at(self) -> datetime | None:
        return self.remind_at or self.deadline

    def describe(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        bits = [f"[#{self.id}] {self.title}"]
        if self.deadline:
            delta = self.deadline - now
            if delta.total_seconds() < 0:
                ago = -delta
                over = f"{int(ago.total_seconds() // 3600)}h" if ago >= timedelta(hours=1) \
                    else f"{int(ago.total_seconds() // 60)}m"
                bits.append(f"due {self.deadline.strftime('%a %H:%M')} ({over} overdue)")
            else:
                bits.append(f"due {self.deadline.strftime('%a %d %b %H:%M')}")
        if self.priority == "high":
            bits.append("❗high")
        if self.source == "commitment":
            bits.append("(you said you would)")
        return " — ".join(bits)


def nudge_state(task: Task, now: datetime, gaps_minutes: list[int], max_nudges: int) -> str:
    """'wait' | 'due' | 'drop' — pure function so the cadence is testable."""
    if task.status != "pending" or task.due_at is None:
        return "wait"
    if now < task.due_at:
        return "wait"
    if task.nudge_count >= max_nudges:
        return "drop"
    if task.nudge_count == 0 or task.last_nudge_at is None:
        return "due"
    gap_idx = min(task.nudge_count - 1, len(gaps_minutes) - 1)
    gap = timedelta(minutes=gaps_minutes[gap_idx])
    return "due" if now >= task.last_nudge_at + gap else "wait"


def compose_nudge(tasks: list[Task], batch_limit: int = 3) -> str:
    """One nudge message covering up to batch_limit due tasks, escalating."""
    lines = []
    for task in tasks[:batch_limit]:
        n = task.nudge_count + 1
        template = NUDGE_TEMPLATES[min(n - 1, len(NUDGE_TEMPLATES) - 1)]
        lines.append(template.format(t=task.title, n=n))
    extra = len(tasks) - batch_limit
    if extra > 0:
        lines.append(f"(+{extra} more waiting — ask for your tasks)")
    return "\n".join(lines)


class TaskStore:
    """SQLite-backed task store. Thread-safe; safe for asyncio + Qt threads."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                priority TEXT NOT NULL DEFAULT 'medium',
                source TEXT NOT NULL DEFAULT 'user',
                deadline TEXT,
                remind_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                nudge_count INTEGER NOT NULL DEFAULT 0,
                last_nudge_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'user',
                note TEXT NOT NULL DEFAULT '',
                at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'desktop',
                at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── internals ────────────────────────────────────────────────────
    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], title=row["title"], notes=row["notes"],
            status=row["status"], priority=row["priority"], source=row["source"],
            deadline=_parse_dt(row["deadline"]), remind_at=_parse_dt(row["remind_at"]),
            created_at=_parse_dt(row["created_at"]), completed_at=_parse_dt(row["completed_at"]),
            nudge_count=row["nudge_count"], last_nudge_at=_parse_dt(row["last_nudge_at"]),
        )

    def _log(self, task_id: int, old: str | None, new: str, actor: str, note: str = "") -> None:
        self._conn.execute(
            "INSERT INTO task_log (task_id, old_status, new_status, actor, note, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, old, new, actor, note, datetime.now().strftime(ISO)),
        )

    # ── CRUD ─────────────────────────────────────────────────────────
    def add(
        self,
        title: str,
        *,
        notes: str = "",
        deadline: datetime | None = None,
        remind_at: datetime | None = None,
        priority: str = "medium",
        source: str = "user",
        actor: str = "user",
    ) -> tuple[Task, bool]:
        """Add a task; near-duplicate pending titles merge onto the existing
        row (returning it with created=False) instead of piling up."""
        title = " ".join(title.split())
        if not title:
            raise ValueError("task title must not be empty")
        with self._lock:
            new_tokens = _tokens(title)
            for existing in self._pending_locked():
                if _jaccard(new_tokens, _tokens(existing.title)) >= 0.75:
                    if deadline and existing.deadline != deadline:
                        self._conn.execute(
                            "UPDATE tasks SET deadline=?, remind_at=?, nudge_count=0, "
                            "last_nudge_at=NULL WHERE id=?",
                            (_fmt_dt(deadline), _fmt_dt(remind_at), existing.id),
                        )
                        self._conn.commit()
                        return self._get_locked(existing.id), False
                    return existing, False
            now = datetime.now().strftime(ISO)
            cur = self._conn.execute(
                "INSERT INTO tasks (title, notes, priority, source, deadline, remind_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, notes, priority, source, _fmt_dt(deadline), _fmt_dt(remind_at), now),
            )
            self._log(cur.lastrowid, None, "pending", actor)
            self._conn.commit()
            return self._get_locked(cur.lastrowid), True

    def _get_locked(self, task_id: int) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"no task #{task_id}")
        return self._row_to_task(row)

    def get(self, task_id: int) -> Task:
        with self._lock:
            return self._get_locked(task_id)

    def _pending_locked(self) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status='pending' ORDER BY deadline IS NULL, deadline, id"
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def pending(self) -> list[Task]:
        with self._lock:
            return self._pending_locked()

    def by_status(self, status: str, limit: int = 50) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    # ── resolution by text (never trust invented ids) ────────────────
    def resolve(self, ref: str) -> tuple[Task | None, list[Task]]:
        """Resolve '#12', '12' or free text to a unique pending task.

        Returns (task, []) on a unique match, (None, candidates) when
        ambiguous, (None, []) when nothing matches.
        """
        ref = ref.strip()
        if m := re.match(r"^#?(\d+)$", ref):
            try:
                task = self.get(int(m.group(1)))
                return (task, []) if task.status == "pending" else (None, [])
            except KeyError:
                return None, []
        query_tokens = _tokens(ref)
        if not query_tokens:
            return None, []
        matches = []
        for task in self.pending():
            title_tokens = _tokens(task.title)
            coverage = len(query_tokens & title_tokens) / len(query_tokens)
            if coverage >= 0.6 and len(query_tokens & title_tokens) >= 1:
                matches.append((coverage, task))
        matches.sort(key=lambda pair: -pair[0])
        if len(matches) == 1 or (len(matches) > 1 and matches[0][0] - matches[1][0] > 0.25):
            return matches[0][1], []
        return None, [t for _, t in matches]

    # ── status changes ───────────────────────────────────────────────
    def _set_status(self, task: Task, status: str, actor: str, note: str = "") -> Task:
        with self._lock:
            done_at = datetime.now().strftime(ISO) if status == "done" else None
            self._conn.execute(
                "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
                (status, done_at, task.id),
            )
            self._log(task.id, task.status, status, actor, note)
            self._conn.commit()
            return self._get_locked(task.id)

    def complete(self, task: Task, actor: str = "user") -> Task:
        return self._set_status(task, "done", actor)

    def cancel(self, task: Task, actor: str = "user", note: str = "") -> Task:
        return self._set_status(task, "cancelled", actor, note)

    def drop(self, task: Task, actor: str = "scheduler") -> Task:
        return self._set_status(task, "dropped", actor, "gave up after max nudges")

    def restore(self, ref: str, actor: str = "user") -> Task | None:
        """Bring back the most recently dropped/cancelled task matching ref."""
        query_tokens = _tokens(ref)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('dropped','cancelled') ORDER BY id DESC"
            ).fetchall()
        for row in rows:
            task = self._row_to_task(row)
            if not query_tokens or _jaccard(query_tokens, _tokens(task.title)) >= 0.4:
                with self._lock:
                    self._conn.execute(
                        "UPDATE tasks SET status='pending', nudge_count=0, last_nudge_at=NULL, "
                        "completed_at=NULL WHERE id=?",
                        (task.id,),
                    )
                    self._log(task.id, task.status, "pending", actor, "restored")
                    self._conn.commit()
                    return self._get_locked(task.id)
        return None

    def snooze(self, task: Task, until: datetime, actor: str = "user") -> Task:
        """Move the reminder and reset the nudge trail (a new time is a fresh start)."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET remind_at=?, nudge_count=0, last_nudge_at=NULL WHERE id=?",
                (_fmt_dt(until), task.id),
            )
            self._log(task.id, task.status, task.status, actor, f"snoozed to {until.strftime(ISO)}")
            self._conn.commit()
            return self._get_locked(task.id)

    def set_deadline(self, task: Task, deadline: datetime | None, actor: str = "user") -> Task:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET deadline=?, nudge_count=0, last_nudge_at=NULL WHERE id=?",
                (_fmt_dt(deadline), task.id),
            )
            self._conn.commit()
            return self._get_locked(task.id)

    # ── nudging ──────────────────────────────────────────────────────
    def due_for_nudge(self, now: datetime, gaps_minutes: list[int], max_nudges: int
                      ) -> tuple[list[Task], list[Task]]:
        """(due, to_drop) — tasks whose nudge is due, and ones past max nudges."""
        due, to_drop = [], []
        for task in self.pending():
            state = nudge_state(task, now, gaps_minutes, max_nudges)
            if state == "due":
                due.append(task)
            elif state == "drop":
                to_drop.append(task)
        return due, to_drop

    def record_nudge(self, tasks: list[Task], now: datetime | None = None) -> None:
        now_s = (now or datetime.now()).strftime(ISO)
        with self._lock:
            for task in tasks:
                self._conn.execute(
                    "UPDATE tasks SET nudge_count=nudge_count+1, last_nudge_at=? WHERE id=?",
                    (now_s, task.id),
                )
            self._conn.commit()

    # ── verbatim overview (no LLM allowed near this) ─────────────────
    def overview(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        today_end = now.replace(hour=23, minute=59, second=59)
        week_end = today_end + timedelta(days=7)
        overdue, today, upcoming, later, undated = [], [], [], [], []
        for task in self.pending():
            if task.deadline is None:
                undated.append(task)
            elif task.deadline < now:
                overdue.append(task)
            elif task.deadline <= today_end:
                today.append(task)
            elif task.deadline <= week_end:
                upcoming.append(task)
            else:
                later.append(task)
        dropped = self.by_status("dropped", limit=5)

        sections = [
            ("🔴 Overdue", overdue), ("📌 Today", today), ("📅 Upcoming (7d)", upcoming),
            ("📆 Later", later), ("🗂 No deadline", undated),
        ]
        out = []
        for header, tasks in sections:
            if tasks:
                out.append(header)
                out.extend(f"  • {t.describe(now)}" for t in tasks)
        if dropped:
            out.append("🪦 Recently dropped (say \"restore …\" to revive)")
            out.extend(f"  • [#{t.id}] {t.title}" for t in dropped)
        return "\n".join(out) if out else "✨ nothing tracked right now — enjoy the calm!"

    # ── conversation history persistence ─────────────────────────────
    def save_message(self, role: str, content: str, source: str = "desktop") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (role, content, source, at) VALUES (?, ?, ?, ?)",
                (role, content, source, datetime.now().strftime(ISO)),
            )
            self._conn.commit()

    def recent_messages(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
