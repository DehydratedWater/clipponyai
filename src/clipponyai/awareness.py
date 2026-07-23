"""Proactive focus/distraction monitor — async, GUI-free, privacy-gated.

Periodically classifies the screen via the LLM's VISION lane and interrupts
for distractions (social media during work hours) or after-hours work.
Opt-in via two gates: screenshot_enabled AND awareness.enabled.

Uses the existing screenshot function and PonyBrain's VISION lane for
classification.  Structured JSON output (should_interrupt, confidence, reason)
with strict parsing/validation.  Cooldown persisted in SQLite meta table so
alerts never repeat within the cooldown window and survive restarts.

Injectable dependencies: assessor (screen classification), clock, sleep,
and delivery callback.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import Config, WorkHoursConfig
from .providers import VISION

log = logging.getLogger("clipponyai.awareness")

_META_LAST_ALERT = "awareness_last_alert"
_MIN_INTERVAL_SECONDS = 30

# Structured output schema for the VISION lane assessment
_ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_interrupt": {
            "type": "boolean",
            "description": "True if the assistant should interrupt the user right now",
        },
        "confidence": {
            "type": "number",
            "description": "How confident the assessment is (0.0 to 1.0)",
        },
        "reason": {
            "type": "string",
            "description": "One short sentence explaining why (or why not)",
        },
    },
    "required": ["should_interrupt", "confidence", "reason"],
}

_ASSESSMENT_PROMPT_TEMPLATE = """\
You look at a screenshot of the user's screen and decide whether to interrupt them.

Current work-hours status: {work_hours_status}
Focus policy (verbatim): {focus_policy}

Pending tasks overview:
{task_overview}

Look at this screenshot. Based on the focus policy, work-hours status, and pending tasks,
decide whether to interrupt the user. Return strictly as JSON."""


# ── data types ────────────────────────────────────────────────────────


@dataclass
class ScreenAssessment:
    """Parsed and validated result from a VISION lane screen classification."""

    should_interrupt: bool
    confidence: float
    reason: str


# ── assessment parsing / validation ───────────────────────────────────


def parse_assessment(result: Any) -> ScreenAssessment:
    """Parse and validate structured JSON output from the VISION lane.

    Mechanical JSON/type/range validation only — no regex, no semantic
    interpretation.  Raises ValueError on any structural problem.
    """
    structured = result.structured
    if not isinstance(structured, dict):
        raise ValueError(f"expected dict, got {type(structured).__name__}")

    # Type checks
    si = structured.get("should_interrupt")
    if not isinstance(si, bool):
        raise ValueError(f"should_interrupt must be bool, got {type(si).__name__}")

    conf = structured.get("confidence")
    if not isinstance(conf, (int, float)):
        raise ValueError(f"confidence must be number, got {type(conf).__name__}")
    if not (0.0 <= conf <= 1.0):
        raise ValueError(f"confidence out of range: {conf}")

    reason = structured.get("reason")
    if not isinstance(reason, str):
        raise ValueError(f"reason must be string, got {type(reason).__name__}")

    return ScreenAssessment(
        should_interrupt=bool(si),
        confidence=float(conf),
        reason=reason,
    )


# ── work-hours status helper ──────────────────────────────────────────


def _work_hours_status(now: datetime, wh: WorkHoursConfig | None) -> str:
    """Return a human-readable work-hours status string for the assessment prompt."""
    if wh is None or not wh.enabled:
        return "Work hours not configured."
    from .scheduler import in_work_hours as _in_wh

    if _in_wh(now, wh):
        return f"Currently inside work hours ({wh.start}–{wh.end})."
    return f"Currently outside work hours ({wh.start}–{wh.end})."


# ── VISION lane assessor ──────────────────────────────────────────────


class PonyBrainAssessor:
    """Uses PonyBrain's VISION lane to classify the screen."""

    def __init__(self, brain: "PonyBrain") -> None:  # noqa: F821 – circular
        self._brain = brain

    def assess(
        self,
        screenshot_bytes: bytes,
        *,
        work_hours_status: str,
        task_overview: str,
        focus_policy: str,
    ) -> ScreenAssessment:
        b64 = base64.b64encode(screenshot_bytes).decode()
        prompt = _ASSESSMENT_PROMPT_TEMPLATE.format(
            work_hours_status=work_hours_status,
            task_overview=task_overview or "(none)",
            focus_policy=focus_policy,
        )
        result = self._brain._run(
            self._brain._spec(VISION),
            [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            output_schema=_ASSESSMENT_SCHEMA,
        )
        return parse_assessment(result)


# ── monitor ───────────────────────────────────────────────────────────


class AwarenessMonitor:
    """Async monitor loop: screenshot -> classify -> maybe interrupt.

    Lifecycle:
    - start() launches the background loop
    - stop() cancels it cleanly

    Privacy gates (all must be True for scanning to proceed):
    1. config.screenshot_enabled  (user allowed screen access)
    2. config.awareness.enabled   (user enabled proactive awareness)

    If screenshot_fn is None (headless), the monitor runs but never scans.
    If either gate is off, the monitor sleeps through each cycle silently.
    """

    def __init__(
        self,
        config: Config,
        screenshot_fn: Callable[[], bytes | None] | None,
        assessor: Any,  # ScreenAssessor protocol
        store: Any,  # TaskStore – meta table access
        deliver: Any,  # Delivery: Callable[[str], Awaitable[None]]
        clock: Any | None = None,  # Clock protocol
    ) -> None:
        self.config = config
        self.screenshot_fn = screenshot_fn
        self.assessor = assessor
        self.store = store
        self.deliver = deliver
        self.clock = clock if clock is not None else _RealClock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Launch the background monitor loop when both privacy gates are open."""
        if self._task is not None and not self._task.done():
            return
        if not self.config.awareness.enabled:
            log.info("awareness monitor: not started (awareness.enabled=False)")
            return
        if not self.config.screenshot_enabled:
            log.info("awareness monitor: not started (screenshot_enabled=False)")
            return
        if self.screenshot_fn is None:
            log.info("awareness monitor: not started (no screenshot function — headless)")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        log.info(
            "awareness monitor: started (interval=%ds, cooldown=%dm)",
            self.config.awareness.interval_seconds,
            self.config.awareness.cooldown_minutes,
        )

    async def refresh(self) -> None:
        """Apply live privacy-setting changes without spawning duplicate loops."""
        enabled = (
            self.config.awareness.enabled
            and self.config.screenshot_enabled
            and self.screenshot_fn is not None
        )
        if enabled:
            await self.start()
        else:
            await self.stop()

    async def stop(self) -> None:
        """Cancel the background loop."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("awareness monitor: stopped")

    async def _loop(self) -> None:
        # Do not capture immediately at startup. Waiting one configured interval
        # is less surprising for an explicitly privacy-sensitive feature.
        while not self._stop.is_set():
            interval = max(_MIN_INTERVAL_SECONDS, self.config.awareness.interval_seconds)
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
                log.exception("awareness monitor tick failed")

    async def _tick(self) -> None:
        """One classification cycle."""
        now = self.clock.now()

        # Double-check gates each cycle (config may have changed)
        if not self.config.awareness.enabled or not self.config.screenshot_enabled:
            return

        # Cooldown check (persisted across restarts)
        if self._in_cooldown(now):
            return

        # Capture and model inference may block; keep both off the GUI event loop.
        png = await asyncio.to_thread(self.screenshot_fn)
        if not png:
            log.debug("awareness: screenshot failed, skipping")
            return

        # Build context
        wh = self.config.reminders.work_hours
        work_status = _work_hours_status(now, wh)
        task_overview = self.store.overview(now)
        focus_policy = self.config.awareness.focus_policy

        # Classify
        try:
            assessment = await asyncio.to_thread(
                self.assessor.assess,
                png,
                work_hours_status=work_status,
                task_overview=task_overview,
                focus_policy=focus_policy,
            )
        except Exception:
            log.exception("awareness: screen assessment failed")
            return

        # Confidence gate
        if assessment.confidence < self.config.awareness.minimum_confidence:
            log.debug(
                "awareness: confidence %.2f below threshold %.2f, skipping",
                assessment.confidence,
                self.config.awareness.minimum_confidence,
            )
            return

        # Interrupt decision
        if not assessment.should_interrupt:
            return

        # Deliver nudge
        message = f"\U0001f434 {assessment.reason}"
        log.info(
            "awareness: interrupting — %s (confidence=%.2f)",
            assessment.reason,
            assessment.confidence,
        )
        await self.deliver(message)

        # Record cooldown timestamp
        self.store.set_meta(_META_LAST_ALERT, str(now.timestamp()))

    def _in_cooldown(self, now: datetime) -> bool:
        """Check whether the last alert is still within the cooldown window."""
        raw = self.store.get_meta(_META_LAST_ALERT)
        if raw is None:
            return False
        try:
            last_epoch = float(raw)
        except (ValueError, TypeError):
            return False
        last_alert = datetime.fromtimestamp(last_epoch)
        cooldown = timedelta(minutes=self.config.awareness.cooldown_minutes)
        return now - last_alert < cooldown


# ── real clock ────────────────────────────────────────────────────────


class _RealClock:
    def now(self) -> datetime:
        return datetime.now()
