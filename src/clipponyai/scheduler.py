"""Reminder scheduler: one asyncio loop, deterministic nudges, quiet hours.

Replaces fren v4's cron fleet with a single tick. Every interval it asks the
store which tasks are due a nudge, composes the escalating message from fixed
templates (no LLM — a reminder must never hallucinate), and hands it to the
delivery callback the app wired up (speech bubble + attention mode, Telegram,
…). Tasks past max_nudges are dropped with a notice instead of nagging
forever. During quiet hours nothing fires; things queue until morning.

Work-hours add a closing nudge at end-of-day listing real pending tasks,
fired once per workday (persisted via TaskStore meta table).

Optional RoutineEngine integration: when a RoutineEngine is passed to the
constructor, its tick runs alongside the ordinary nudge cycle.  Quiet hours
suppress delivery but allow missed-marking state updates.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from datetime import time as dt_time
from typing import TYPE_CHECKING

from .config import RemindersConfig, WorkHoursConfig
from .tasks import DROP_NOTICE, TaskStore, compose_nudge

if TYPE_CHECKING:
    from .accountability import ActivityStore
    from .context_questions import ProactiveQuestioner
    from .goals import GoalEngine
    from .routines import RoutineEngine
    from .rules import RuleEngine

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
    """Pure function: is the workday closing reminder due?

    Returns True when the configured end time has passed (or arrived) on an
    active workday *and* today's closing meta was not yet recorded.

    Deterministic semantics:

    - Fires **once** per workday when ``now >= end_time`` on that day.
    - Does **not** fire before end time.
    - Does **not** fire on inactive (non-workday) days.
    - Does **not** fire for a previous day after midnight (the day-key in
      the meta table naturally prevents this: tomorrow's key is different,
      and ``closing_due`` for tomorrow is still False until tomorrow's end
      time arrives).

    The caller must still check quiet-hours precedence and once-per-day
    meta persistence.
    """
    if not config.enabled or not config.closing_nudge:
        return False
    if now.weekday() not in config.weekdays:
        return False
    end = _parse_hhmm(config.end)
    t = now.time()
    # fire once the configured end time has arrived or passed
    # (e.g. end=17:00 -> True at 17:00, 17:30, 18:00, … up to 23:59)
    return t >= end


class ReminderScheduler:
    def __init__(
        self,
        store: TaskStore,
        config: RemindersConfig,
        deliver: Deliver,
        work_hours: WorkHoursConfig | None = None,
        routine_engine: RoutineEngine | None = None,
        goal_engine: GoalEngine | None = None,
        rule_engine: RuleEngine | None = None,
        activity_store: ActivityStore | None = None,
        proactive_questioner: "ProactiveQuestioner | None" = None,
    ) -> None:
        self.store = store
        self.config = config
        self.deliver = deliver
        self.work_hours = work_hours or (config.work_hours if hasattr(config, "work_hours") else None)
        self.routine_engine = routine_engine
        self.goal_engine = goal_engine
        self.rule_engine = rule_engine
        self.activity_store = activity_store
        self.proactive_questioner = proactive_questioner
        self._stop = asyncio.Event()

    async def tick(self, now: datetime | None = None) -> str | None:
        """One scheduling pass; returns the nudge message sent (for tests)."""
        now = now or datetime.now()
        if not self.config.enabled:
            return None

        if self.proactive_questioner is not None:
            self.proactive_questioner.clear_tick()

        in_quiet = in_quiet_hours(
            now, self.config.quiet_hours_start, self.config.quiet_hours_end,
        )

        # ── goal sync (runs every tick regardless of quiet hours) ──
        self._try_goal_sync(now)

        # ── rule engine tick (after quiet gating, allow_delivery=False in quiet) ──
        rule_msg = await self._try_rule_tick(now, allow_delivery=not in_quiet)

        # ── routine engine tick (always runs, delivery gated by quiet hours) ──
        routine_msg = await self._try_routine_tick(now)

        # ── closing nudge (once per workday) ─────────────────────
        closing_msg = await self._try_closing_nudge(now)
        if closing_msg:
            return closing_msg

        # ── quiet hours always win for ordinary nudges ───────────
        if in_quiet:
            parts = [m for m in [rule_msg, routine_msg] if m]
            return "\n".join(parts) if parts else None

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
                parts = [m for m in [rule_msg, routine_msg] if m]
                return "\n".join(parts) if parts else None

        # ── ordinary nudge cycle ─────────────────────────────────
        due, to_drop = self.store.due_for_nudge(
            now, self.config.nudge_gaps_minutes, self.config.max_nudges,
        )
        for task in to_drop:
            self.store.drop(task)
            await self.deliver(DROP_NOTICE.format(t=task.title))
            log.info("dropped task #%s after max nudges", task.id)
        parts = [m for m in [rule_msg, routine_msg] if m]
        if not due and not parts:
            return None
        nudge_msg = None
        if due:
            nudge_msg = compose_nudge(due, self.config.batch_limit)
            await self.deliver(nudge_msg)
            self.store.record_nudge(due[: self.config.batch_limit], now)
            log.info("nudged %d task(s)", min(len(due), self.config.batch_limit))
            # Record activity for nudge delivery
            if self.activity_store is not None:
                titles = ", ".join(t.title for t in due[: self.config.batch_limit])
                self.activity_store.record(
                    "nudge_delivered", actor="scheduler",
                    detail=f"Nudge sent for: {titles}",
                )

        # Combine all messages
        all_parts = [m for m in [rule_msg, routine_msg, nudge_msg] if m]
        combined = "\n".join(all_parts) if all_parts else None

        # ── proactive questions: LAST, only if nothing else delivered ──
        if self.proactive_questioner is not None:
            if combined:
                self.proactive_questioner.mark_delivered_this_tick()
            pq_msg = await self.proactive_questioner.tick(now)
            if pq_msg:
                await self.deliver(pq_msg)
                if combined:
                    return combined + "\n" + pq_msg
                return pq_msg

        return combined

    def _try_goal_sync(self, now: datetime) -> None:
        """Run GoalEngine sync for today (always, regardless of quiet hours)."""
        if self.goal_engine is None:
            return
        try:
            self.goal_engine.sync(now.date())
        except Exception:
            log.exception("goal sync failed")

    async def _try_rule_tick(
        self, now: datetime, *, allow_delivery: bool = True
    ) -> str | None:
        """Run RuleEngine tick. Delivery gated by *allow_delivery*."""
        if self.rule_engine is None:
            return None
        try:
            fired = self.rule_engine.tick(now, allow_delivery=allow_delivery)
            if not fired:
                return None
            # Build combined message from fired rules
            messages = [r.message or f"Rule triggered: {r.title}" for r in fired]
            return "\n".join(messages)
        except Exception:
            log.exception("rule tick failed")
            return None

    async def _try_routine_tick(self, now: datetime) -> str | None:
        """Run the RoutineEngine tick if one is wired in.

        Quiet hours suppress delivery but allow missed-marking state updates.
        """
        if self.routine_engine is None:
            return None

        in_quiet = in_quiet_hours(
            now, self.config.quiet_hours_start, self.config.quiet_hours_end,
        )
        allow_delivery = not in_quiet

        events = await self.routine_engine.tick_async(now, allow_delivery=allow_delivery)
        if not events:
            return None

        # Collect messages from delivered events
        messages = [e.message for e in events if e.event_type == "reminder" and e.delivered]
        if messages:
            return "\n".join(messages)
        return None

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
