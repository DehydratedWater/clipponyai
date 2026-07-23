"""Routine recurrence semantics, streaks, and the tick engine.

Pure functions for date math and a RoutineEngine that drives reminders
through the existing scheduler delivery pipeline.  No LLM calls.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from .accountability import (
    ActivityStore,
    Routine,
    RoutineCompletion,
    RoutineCompletionStore,
    RoutineStore,
)
from .tasks import TaskStore

log = logging.getLogger("clipponyai.routines")

# ─── Validation ───────────────────────────────────────────────────────

VALID_CADENCES = {"daily", "weekdays", "monthly"}


def validate_time_of_day(t: str) -> str:
    """Validate and normalise an 'HH:MM' string. Raises ValueError."""
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time format {t!r}, expected HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time out of range {t!r}")
    return f"{h:02d}:{m:02d}"


def validate_cadence(c: str) -> str:
    """Validate cadence string. Raises ValueError."""
    if c not in VALID_CADENCES:
        raise ValueError(f"invalid cadence {c!r}, must be one of {sorted(VALID_CADENCES)}")
    return c


def _parse_hhmm(s: str) -> dt_time:
    """Parse 'HH:MM' into a datetime.time."""
    h, m = s.split(":")
    return dt_time(int(h), int(m))


# ─── Occurrence helpers ───────────────────────────────────────────────

def is_scheduled_on(routine: Routine, d: date) -> bool:
    """Return True if *routine* has an occurrence on date *d*.

    - daily: every day
    - weekdays: only Mon(0) .. Fri(4) unless overridden by routine.weekdays
    - monthly: only on the clamped day_of_month (see _clamp_monthly)
    """
    cadence = routine.cadence
    if cadence == "daily":
        return True
    if cadence == "weekdays":
        if routine.weekdays:
            return d.weekday() in routine.weekdays
        return d.weekday() < 5  # Mon-Fri default
    if cadence == "monthly":
        target = routine.day_of_month or 1
        clamped = _clamp_monthly(target, d.year, d.month)
        return d.day == clamped
    return False


def _clamp_monthly(day_of_month: int, year: int, month: int) -> int:
    """Clamp day_of_month to the final calendar day of the month.

    E.g. 31 in February -> 28 (or 29 in leap years).
    """
    max_day = calendar.monthrange(year, month)[1]
    return min(day_of_month, max_day)


def occurrence_due_date(routine: Routine, d: date) -> date | None:
    """Return *d* if the routine is scheduled on that date, else None."""
    if is_scheduled_on(routine, d):
        return d
    return None


def due_at(routine: Routine, d: date) -> dt_time:
    """Return the due time for an occurrence on date *d*.

    Priority: time_of_day > deadline_time > 09:00 default.
    """
    if routine.time_of_day:
        return _parse_hhmm(routine.time_of_day)
    if routine.deadline_time:
        return _parse_hhmm(routine.deadline_time)
    return dt_time(9, 0)


def next_occurrence(routine: Routine, from_datetime: datetime) -> datetime:
    """Find the next occurrence datetime for *routine* strictly after *from_datetime*.

    Walks forward day by day (max 400 days) and returns the first scheduled date
    combined with the due_at time.  Raises ValueError if no occurrence found within range.
    """
    start_date = from_datetime.date() + timedelta(days=1)
    limit = 400

    for offset in range(limit):
        candidate = start_date + timedelta(days=offset)
        if is_scheduled_on(routine, candidate):
            t = due_at(routine, candidate)
            return datetime(candidate.year, candidate.month, candidate.day, t.hour, t.minute)

    raise ValueError(
        f"no next occurrence found for routine '{routine.title}' "
        f"(cadence={routine.cadence}) within {limit} days"
    )


# ─── Streak calculations ─────────────────────────────────────────────

def current_streak(routine: Routine, completions: list[RoutineCompletion], today: date | None = None) -> int:
    """Calculate the current streak of consecutive completed scheduled occurrences.

    Walks backwards from *today* (or real today) through scheduled dates.
    A 'done' status continues the streak. A 'skipped', 'missed' or missing
    completion breaks it. Returns the count of consecutive 'done' occurrences.
    """
    if today is None:
        today = date.today()
    completion_map: dict[str, str] = {c.occurrence_date: c.status for c in completions}

    streak = 0
    d = today
    for _ in range(400):
        if not is_scheduled_on(routine, d):
            d -= timedelta(days=1)
            continue
        key = d.isoformat()
        status = completion_map.get(key)
        if status == "done":
            streak += 1
        else:
            break
        d -= timedelta(days=1)
    return streak


def longest_streak(routine: Routine, completions: list[RoutineCompletion]) -> int:
    """Calculate the longest streak of consecutive completed scheduled occurrences
    across all history.

    Builds a timeline of scheduled dates and tracks consecutive 'done' runs.
    """
    if not completions:
        return 0

    completion_map: dict[str, str] = {c.occurrence_date: c.status for c in completions}

    # Find date range from completions
    dates = sorted({c.occurrence_date for c in completions})
    if not dates:
        return 0

    start = date.fromisoformat(min(dates)) - timedelta(days=1)
    end = date.fromisoformat(max(dates)) + timedelta(days=1)

    best = 0
    current = 0
    d = start
    while d <= end:
        if not is_scheduled_on(routine, d):
            d += timedelta(days=1)
            continue
        key = d.isoformat()
        status = completion_map.get(key)
        if status == "done":
            current += 1
            best = max(best, current)
        else:
            current = 0
        d += timedelta(days=1)
    return best


# ─── Tick event dataclass ────────────────────────────────────────────

@dataclass
class TickEvent:
    """Structured event returned by RoutineEngine.tick()."""
    routine_id: int
    routine_title: str
    occurrence_date: str  # YYYY-MM-DD
    event_type: str  # "reminder" | "missed" | "completed" | "skipped"
    message: str = ""
    delivered: bool = False


# ─── RoutineEngine ───────────────────────────────────────────────────

class RoutineEngine:
    """Processes routine occurrences: reminders, missed marking, completion/skip.

    Uses TaskStore meta keys for deduplication (no duplicate Task rows).
    Injects deliver callable and optional ActivityStore.  No LLM calls.
    """

    def __init__(
        self,
        routine_store: RoutineStore,
        completion_store: RoutineCompletionStore,
        task_store: TaskStore,
        deliver: Callable[[str], Awaitable[None]],
        activity_store: ActivityStore | None = None,
    ) -> None:
        self.routines = routine_store
        self.completions = completion_store
        self.task_store = task_store
        self.deliver = deliver
        self.activity = activity_store

    def _reminder_message(self, routine: Routine) -> str:
        """Build the reminder message for a routine due today."""
        return (
            f"Routine reminder: '{routine.title}' is due! "
            f"Say 'done {routine.title}' to mark complete or 'skip {routine.title}' to skip."
        )

    def _get_completion_today(self, routine_id: int, today_str: str) -> RoutineCompletion | None:
        """Check if a completion already exists for routine today."""
        completions = self.completions.by_routine(routine_id)
        for c in completions:
            if c.occurrence_date == today_str:
                return c
        return None

    def get_completion_status(self, routine_id: int, occurrence_date: str) -> str | None:
        """Look up completion status for a routine on a given date."""
        completions = self.completions.by_routine(routine_id)
        for c in completions:
            if c.occurrence_date == occurrence_date:
                return c.status
        return None

    # ── tick ─────────────────────────────────────────────────────────

    def tick(
        self, now: datetime, *, allow_delivery: bool = True
    ) -> list[TickEvent]:
        """One scheduling pass for routines.

        For each active (enabled, not archived) routine due today:
        1. If due time has arrived and no completion exists, deliver reminder (once).
        2. If deadline_time has passed and still uncompleted, mark as 'missed'.

        allow_delivery controls whether messages are actually sent (e.g. quiet
        hours block delivery but missed marking still happens).

        Returns structured TickEvent list for tests.
        """
        events: list[TickEvent] = []
        today = now.date()
        today_str = today.isoformat()
        now_time = now.time()

        active_routines = [
            r for r in self.routines.list_all()
            if r.enabled and r.archived_at is None and is_scheduled_on(r, today)
        ]

        for routine in active_routines:
            rid = routine.id
            title = routine.title

            # Check if already completed or skipped today
            existing_status = self.get_completion_status(rid, today_str)
            if existing_status in ("done", "skipped"):
                continue

            # Determine due time
            due_time = due_at(routine, today)

            # Check if past deadline and should be marked missed (always, even if delivery blocked)
            if routine.deadline_time:
                deadline = _parse_hhmm(routine.deadline_time)
                if now_time >= deadline and existing_status != "missed":
                    self.completions.upsert(rid, today_str, status="missed")
                    if self.activity:
                        self.activity.record(
                            "routine_missed",
                            detail=f"Routine '{title}' missed",
                            ref_type="routine",
                            ref_id=str(rid),
                        )
                    events.append(TickEvent(
                        routine_id=rid,
                        routine_title=title,
                        occurrence_date=today_str,
                        event_type="missed",
                        message=f"Routine '{title}' was missed (past deadline {routine.deadline_time}).",
                        delivered=False,
                    ))
                    continue

            # Reminder delivery — only if at/after due time
            if now_time < due_time:
                continue

            # Deduplication via meta key
            meta_key = f"routine_notified_{rid}_{today_str}"
            if self.task_store.get_meta(meta_key) == "done":
                continue

            message = self._reminder_message(routine)

            if allow_delivery:
                self.task_store.set_meta(meta_key, "done")
                events.append(TickEvent(
                    routine_id=rid,
                    routine_title=title,
                    occurrence_date=today_str,
                    event_type="reminder",
                    message=message,
                    delivered=True,
                ))
            else:
                # Delivery blocked (e.g. quiet hours) — record event but don't deliver
                # Still set meta so we don't re-evaluate on next tick
                self.task_store.set_meta(meta_key, "done")
                events.append(TickEvent(
                    routine_id=rid,
                    routine_title=title,
                    occurrence_date=today_str,
                    event_type="reminder",
                    message=message,
                    delivered=False,
                ))

        return events

    async def tick_async(
        self, now: datetime, *, allow_delivery: bool = True
    ) -> list[TickEvent]:
        """Async wrapper around tick that delivers messages."""
        events = self.tick(now, allow_delivery=allow_delivery)
        for event in events:
            if event.event_type == "reminder" and event.delivered:
                await self.deliver(event.message)
        return events

    # ── helper methods ───────────────────────────────────────────────

    def complete_today(self, routine_id: int, now: datetime) -> TickEvent:
        """Mark a routine as done for today."""
        today_str = now.strftime("%Y-%m-%d")
        routine = self.routines.get(routine_id)
        self.completions.upsert(routine_id, today_str, status="done")

        event = TickEvent(
            routine_id=routine_id,
            routine_title=routine.title,
            occurrence_date=today_str,
            event_type="completed",
            message=f"'{routine.title}' marked done!",
            delivered=False,
        )

        if self.activity:
            self.activity.record(
                "routine_completed",
                detail=f"Routine '{routine.title}' completed",
                ref_type="routine",
                ref_id=str(routine_id),
            )

        return event

    def skip_today(self, routine_id: int, now: datetime) -> TickEvent:
        """Mark a routine as skipped for today."""
        today_str = now.strftime("%Y-%m-%d")
        routine = self.routines.get(routine_id)
        self.completions.upsert(routine_id, today_str, status="skipped")

        event = TickEvent(
            routine_id=routine_id,
            routine_title=routine.title,
            occurrence_date=today_str,
            event_type="skipped",
            message=f"'{routine.title}' skipped.",
            delivered=False,
        )

        if self.activity:
            self.activity.record(
                "routine_skipped",
                detail=f"Routine '{routine.title}' skipped",
                ref_type="routine",
                ref_id=str(routine_id),
            )

        return event
