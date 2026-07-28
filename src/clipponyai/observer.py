"""Continuous foreground observation recording with episode coalescing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .accountability import Observation
from .screen_context import ForegroundContext, foreground_context, redact_title

log = logging.getLogger("clipponyai.observer")

_MIN_SAMPLE_SECONDS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ObservationRecorder:
    """Sample foreground metadata and persist changes as observation episodes."""

    def __init__(
        self,
        config: Any,
        observation_store: Any,
        *,
        context_fn: Callable[..., ForegroundContext | None] = foreground_context,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
        self.observation_store = observation_store
        self._context_fn = context_fn
        self._clock = clock
        self._current: Observation | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start one recorder loop when observation is enabled."""
        if self._task is not None and not self._task.done():
            return
        if not self.config.observation.enabled:
            log.info("observation recorder: not started (observation.enabled=False)")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        log.info(
            "observation recorder: started (interval=%ds)",
            self.config.observation.sample_seconds,
        )

    async def refresh(self) -> None:
        """Apply a live observation setting change."""
        if self.config.observation.enabled:
            await self.start()
        else:
            await self.stop()

    async def stop(self) -> None:
        """Close the current episode and stop the recorder promptly."""
        self._stop.set()
        self._close_current(self._clock())
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("observation recorder: stopped")

    async def _loop(self) -> None:
        # Waiting first avoids collecting privacy-sensitive metadata at boot.
        while not self._stop.is_set():
            interval = max(
                _MIN_SAMPLE_SECONDS,
                self.config.observation.sample_seconds,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                continue
            except TimeoutError:
                pass
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("observation recorder tick failed")

    async def _tick(self, now: datetime | None = None) -> None:
        """Record or extend one foreground episode."""
        now = now or self._clock()
        observation = self.config.observation
        if not observation.enabled:
            self._close_current(now)
            return

        context = self._context_fn(capture_window_titles=observation.capture_window_titles)
        if context is None:
            return

        title = (
            redact_title(context.window_title, observation.redact_patterns)
            if observation.capture_window_titles
            else ""
        )
        category = (
            "idle" if context.idle_seconds >= observation.idle_threshold_seconds else "unknown"
        )
        key = (context.app, title, category)
        current_key = None
        if self._current is not None:
            current_key = (
                self._current.app,
                self._current.window_title,
                self._current.category,
            )
        if current_key == key:
            self.observation_store.extend(self._current.id, now)
            self._current = replace(self._current, ended_at=now)
            return

        self._current = self.observation_store.record(
            started_at=now,
            ended_at=now,
            source="os",
            app=context.app,
            window_title=title,
            category=category,
            idle_seconds=int(context.idle_seconds),
        )

    def _close_current(self, now: datetime) -> None:
        if self._current is None:
            return
        self.observation_store.extend(self._current.id, now)
        self._current = None
