"""Reminder scheduler: one asyncio loop, deterministic nudges, quiet hours.

Replaces fren v4's cron fleet with a single tick. Every interval it asks the
store which tasks are due a nudge, composes the escalating message from fixed
templates (no LLM — a reminder must never hallucinate), and hands it to the
delivery callback the app wired up (speech bubble + attention mode, Telegram,
…). Tasks past max_nudges are dropped with a notice instead of nagging
forever. During quiet hours nothing fires; things queue until morning.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from .config import RemindersConfig
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


class ReminderScheduler:
    def __init__(self, store: TaskStore, config: RemindersConfig, deliver: Deliver) -> None:
        self.store = store
        self.config = config
        self.deliver = deliver
        self._stop = asyncio.Event()

    async def tick(self, now: datetime | None = None) -> str | None:
        """One scheduling pass; returns the nudge message sent (for tests)."""
        now = now or datetime.now()
        if not self.config.enabled:
            return None
        if in_quiet_hours(now, self.config.quiet_hours_start, self.config.quiet_hours_end):
            return None
        due, to_drop = self.store.due_for_nudge(
            now, self.config.nudge_gaps_minutes, self.config.max_nudges
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
