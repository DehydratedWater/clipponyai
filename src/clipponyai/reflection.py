"""Periodic, tool-using reflection over grounded recent context."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from .awareness import _work_hours_status
from .brain import AWARENESS_AUDIT_ACTIONS, PROACTIVE_SOURCES
from .digest import EMPTY_DIGEST, render_activity_digest
from .routines import current_streak
from .scheduler import in_quiet_hours

log = logging.getLogger("clipponyai.reflection")

_META_LAST_RUN = "reflection_last_run"
_META_LAST_SPOKE = "reflection_last_spoke"
_REFLECTION_AUDIT_ACTIONS = {"reflection_spoke", "reflection_failed"}
_MIN_INTERVAL_SECONDS = 300

Reflect = Callable[[str], Awaitable[str | None]]
Deliver = Callable[..., Awaitable[None]]


def _now() -> datetime:
    # Naive local time, matching the shared SQLite timestamp format.
    return datetime.now()


def _parse_meta_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


class ReflectionEngine:
    """Think periodically, while stacked gates keep proactive speech sparse."""

    def __init__(
        self,
        config: Any,
        store: Any,
        observation_store: Any,
        activity_store: Any,
        *,
        reflect_fn: Reflect,
        deliver: Deliver,
        questioner: Any | None = None,
        clock: Callable[[], datetime] = _now,
        routine_engine: Any | None = None,
        goal_engine: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.observation_store = observation_store
        self.activity_store = activity_store
        self.reflect_fn = reflect_fn
        self.deliver = deliver
        self.questioner = questioner
        self.clock = clock
        self.routine_engine = routine_engine
        self.goal_engine = goal_engine
        self._task: asyncio.Task | None = None

    def _now(self) -> datetime:
        if callable(self.clock):
            return self.clock()
        return self.clock.now()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.config.reflection.enabled:
            log.info("reflection engine: not started (reflection.enabled=False)")
            return
        self._task = asyncio.create_task(self._loop())
        log.info(
            "reflection engine: started (interval=%dm)",
            self.config.reflection.interval_minutes,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("reflection engine: stopped")

    async def refresh(self) -> None:
        if not self.config.reflection.enabled:
            await self.stop()
            return
        if self._task is not None and not self._task.done():
            await self.stop()
        await self.start()

    async def _loop(self) -> None:
        while True:
            interval = max(
                _MIN_INTERVAL_SECONDS,
                self.config.reflection.interval_minutes * 60,
            )
            await asyncio.sleep(interval)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reflection tick failed")

    async def _tick(self) -> None:
        now = self._now()
        reflection = self.config.reflection

        if not reflection.enabled:
            return
        reminders = self.config.reminders
        if in_quiet_hours(
            now,
            reminders.quiet_hours_start,
            reminders.quiet_hours_end,
        ):
            return
        if self.questioner is not None and self.questioner.is_silenced(now):
            return

        last_spoke = _parse_meta_datetime(self.store.get_meta(_META_LAST_SPOKE))
        if last_spoke is not None and now - last_spoke < timedelta(
            minutes=reflection.min_gap_minutes
        ):
            return

        messages = self.store.recent_messages(10, with_source=True, with_at=True)
        nudge_cutoff = now - timedelta(minutes=reflection.quiet_after_nudge_minutes)
        if reflection.quiet_after_nudge_minutes and any(
            message.get("source") in PROACTIVE_SOURCES and message["at"] >= nudge_cutoff
            for message in messages
        ):
            return

        last_run = _parse_meta_datetime(self.store.get_meta(_META_LAST_RUN))
        observations = self._new_observations(last_run, now)
        activity = self._new_activity(last_run)
        new_messages = [
            message for message in messages if last_run is None or message["at"] > last_run
        ]
        if not observations and not activity and not new_messages:
            return

        latest_observation = self.observation_store.latest()
        if latest_observation is not None and latest_observation.category == "idle":
            return

        self.store.set_meta(_META_LAST_RUN, now.isoformat())
        context = self._build_context(now)
        try:
            text = await self.reflect_fn(context)
        except Exception as exc:
            log.exception("reflection turn failed")
            self.activity_store.record(
                "reflection_failed",
                actor="reflection",
                detail=f"scheduled trigger; {type(exc).__name__}: {exc}"[:200],
            )
            return

        if text is None:
            return
        normalized_text = text.strip()
        if not normalized_text:
            return
        if re.fullmatch(
            r"silent[.!?,;:…\s-]*",
            normalized_text,
            re.IGNORECASE,
        ):
            return
        await self.deliver(text, source="reflection")
        self.store.set_meta(_META_LAST_SPOKE, now.isoformat())
        self.activity_store.record(
            "reflection_spoke",
            actor="reflection",
            detail=f"trigger=scheduled length={len(text)}",
        )

    def _new_observations(
        self,
        last_run: datetime | None,
        now: datetime,
    ) -> list[Any]:
        cutoff = last_run or now - timedelta(hours=self.config.reflection.context_hours)
        if last_run is None:
            return self.observation_store.since(cutoff)
        return [
            observation
            for observation in self.observation_store.recent(1000)
            if observation.started_at > last_run or observation.ended_at > last_run
        ]

    def _new_activity(self, last_run: datetime | None) -> list[Any]:
        excluded = AWARENESS_AUDIT_ACTIONS | _REFLECTION_AUDIT_ACTIONS
        recent = self.activity_store.recent(15, exclude_actions=excluded)
        if last_run is None:
            return recent
        return [row for row in recent if row.at > last_run]

    def _build_context(self, now: datetime) -> str:
        sections = [
            f"It is {now:%A %H:%M}.\n{_work_hours_status(now, self.config.reminders.work_hours)}"
        ]

        if self.config.observation.enabled:
            cutoff = now - timedelta(hours=self.config.reflection.context_hours)
            observations = self.observation_store.since(cutoff)
            digest = render_activity_digest(
                observations,
                now=now,
                hours=self.config.reflection.context_hours,
            )
            if digest != EMPTY_DIGEST:
                sections.append(digest)

        tasks = self.store.overview(now)
        if "nothing tracked right now" not in tasks:
            sections.append(f"Pending tasks:\n{tasks}")

        accountability = self._accountability_summary(now)
        if accountability:
            sections.append(f"Recent routines and goals:\n{accountability}")

        activity = self.activity_store.recent(
            15,
            exclude_actions=AWARENESS_AUDIT_ACTIONS | _REFLECTION_AUDIT_ACTIONS,
        )
        if activity:
            lines = [
                f"  {row.at:%H:%M} {row.action}" + (f" — {row.detail}" if row.detail else "")
                for row in activity
            ]
            sections.append("What you have done recently:\n" + "\n".join(lines))

        return "\n\n".join(sections)

    def _accountability_summary(self, now: datetime) -> str:
        lines: list[str] = []
        if self.routine_engine is not None:
            routines = [
                routine
                for routine in self.routine_engine.routines.list_all()
                if routine.enabled and routine.archived_at is None
            ]
            for routine in routines[:5]:
                completions = self.routine_engine.completions.by_routine(routine.id)
                streak = current_streak(routine, completions, now.date())
                lines.append(
                    f"  Routine #{routine.id}: {routine.title} ({routine.cadence}, streak {streak})"
                )
        if self.goal_engine is not None:
            for goal in self.goal_engine.summaries()[:5]:
                if goal.status != "active":
                    continue
                progress = []
                if goal.target_count is not None:
                    progress.append(f"count {goal.count}/{goal.target_count}")
                if goal.target_streak is not None:
                    progress.append(f"streak {goal.current_streak}/{goal.target_streak}")
                suffix = f", {', '.join(progress)}" if progress else ""
                lines.append(f"  Goal #{goal.goal_id}: {goal.title} (active{suffix})")
        return "\n".join(lines)
