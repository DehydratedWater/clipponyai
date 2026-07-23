"""Reminder scheduler: one asyncio loop, deterministic nudges, quiet hours.

Replaces fren v4's cron fleet with a single tick. Every interval it asks the
store which tasks are due a nudge, composes the escalating message from fixed
templates (no LLM — a reminder must never hallucinate), and hands it to the
delivery callback the app wired up (speech bubble + attention mode, Telegram,
…). Tasks past max_nudges are dropped with a notice instead of nagging
forever. During quiet hours nothing fires; things queue until morning.

Work-hours add a closing nudge at end-of-day listing real pending tasks,
fired once per workday (persisted via TaskStore meta table).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dt_time
from typing import Awaitable, Callable

from .config import RemindersConfig, WorkHoursConfig
from .tasks import DROP_NOTICE, TaskStore, compose_nudge

log = logging.getLogger("clipponyai.scheduler")

Deliver = Callable[[str], Awaitable[None]]


def in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True when nudges should stay silent. Handles ranges crossing midnight
    (23→8) and same-day ranges (13→15). start == end disables quiet hours."""
    hour = now.hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _parse_hhmm(s: str) -> dt_time:
    """Parse 'HH:MM' into a datetime.time."""
    h, m = s.split(":")
    return dt_time(int(h), int(m))


def in_work_hours(now: datetime, config: WorkHoursConfig) -> bool:
    """Pure function: is *now* inside the configured work-hours window?

    Checks weekday, then time-of-day against start/end.
    """
    if not config.enabled:
        return False
    if now.weekday() not in config.weekdays:
        return False
    t = now.time()
    start = _parse_hhmm(config.start)
    end = _parse_hhmm(config.end)
    if start < end:
        return start <= t < end
    # overnight window (e.g. 22:00 -> 06:00)
    return t >= start or t < end


def closing_due(now: datetime, config: WorkHoursConfig) -> bool:
    """Pure function: is it time for the closing nudge?

    Returns True when *now* is within the last check-interval window before
    work-hours end on a workday.  The caller must still check quiet-hours
    and once-per-day persistence.
    """
    if not config.enabled or not config.closing_nudge:
        return False
    if now.weekday() not in config.weekdays:
        return False
    end = _parse_hhmm(config.end)
    t = now.time()
    # fire during the end-hour (e.g. 17:00-17:59 for end=17:00)
    return t.hour == end.hour


class ReminderScheduler:
    def __init__(
        self,
        store: TaskStore,
        config: RemindersConfig,
        deliver: Deliver,
        work_hours: WorkHoursConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.deliver = deliver
        self.work_hours = work_hours or (config.work_hours if hasattr(config, "work_hours") else None)
        self._stop = asyncio.Event()

    async def tick(self, now: datetime | None = None) -> str | None:
        """One scheduling pass; returns the nudge message sent (for tests)."""
        now = now or datetime.now()
        if not self.config.enabled:
            return None
        # ── closing nudge (once per workday) ─────────────────────
        closing_msg = await self._try_closing_nudge(now)
        if closing_msg:
            return closing_msg
        # ── quiet hours always win ───────────────────────────────
        if in_quiet_hours(now, self.config.quiet_hours_start, self.config.quiet_hours_end):
            return None
        # ── suppress ordinary reminders outside work hours ───────
        wh = self.work_hours
        if wh and wh.enabled and wh.suppress_off_hours:
            if not in_work_hours(now, wh):
                # still drop exhausted tasks silently
                _, to_drop = self.store.due_for_nudge(
                    now, self.config.nudge_gaps_minutes, self.config.max_nudges,
                )
                for task in to_drop:
                    self.store.drop(task)
                    await self.deliver(DROP_NOTICE.format(t=task.title))
                return None
        # ── ordinary nudge cycle ─────────────────────────────────
        due, to_drop = self.store.due_for_nudge(
            now, self.config.nudge_gaps_minutes, self.config.max_nudges,
        )
        for task in to_drop:
            self.store.drop(task)
            await self.deliver(DROP_NOTICE.format(t=task.title))
            log.info("dropped task #%s after max nudges", task.id)
        if not due:
            return None
        message = compose_nudge(due, self.config.batch_limit)
        await self.deliver(message)
        self.store.record_nudge(due[: self.config.batch_limit], now)
        log.info("nudged %d task(s)", min(len(due), self.config.batch_limit))
        return message

    async def _try_closing_nudge(self, now: datetime) -> str | None:
        """Emit the once-per-workday closing nudge listing pending tasks.

        Quiet-hours take precedence: if closing time falls inside quiet hours
        the nudge is suppressed entirely.
        """
        wh = self.work_hours
        if not wh or not closing_due(now, wh):
            return None
        # quiet hours override closing nudge
        if in_quiet_hours(now, self.config.quiet_hours_start, self.config.quiet_hours_end):
            return None
        # once per workday — use meta table for idempotency
        day_key = f"closing_nudge_{now.strftime('%Y-%m-%d')}"
        if self.store.get_meta(day_key) == "done":
            return None
        pending = self.store.pending()
        if not pending:
            self.store.set_meta(day_key, "done")
            return None
        lines = ["🐴 End of workday — still pending:"]
        limit = self.config.batch_limit
        for t in pending[:limit]:
            lines.append(f"  • [#{t.id}] {t.title}")
        msg = "\n".join(lines)
        await self.deliver(msg)
        self.store.set_meta(day_key, "done")
        log.info("closing nudge for %d pending task(s)", len(pending))
        return msg

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.check_interval_seconds
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
