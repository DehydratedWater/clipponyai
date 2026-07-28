"""Proactive focus/distraction monitor — async, GUI-free, privacy-gated.

Periodically classifies the screen via the LLM's VISION lane and interrupts
for distractions (social media during work hours) or after-hours work.
Opt-in via two gates: screenshot_enabled AND awareness.enabled.

Uses the existing screenshot function and PonyBrain's VISION lane for
classification. Structured JSON output records the observed activity and the
interrupt decision. Cooldown is persisted in SQLite so alerts never repeat
within the configured window, while observations continue during cooldown.

Injectable dependencies: assessor (screen classification), clock, sleep,
and delivery callback.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .accountability import _json_dumps
from .config import Config, WorkHoursConfig
from .providers import VISION

log = logging.getLogger("clipponyai.awareness")

_META_LAST_ALERT = "awareness_last_alert"
_MIN_INTERVAL_SECONDS = 30
OBSERVATION_CATEGORIES = frozenset(
    {
        "work",
        "communication",
        "entertainment",
        "browsing",
        "learning",
        "idle",
        "other",
    }
)

# Structured output schema for the VISION lane assessment
_ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "activity": {
            "type": "string",
            "description": (
                "What is being done on screen, one short neutral phrase, e.g. "
                "'editing a Python test file' or 'scrolling a video feed'. "
                "A sensor reading, not a message to anyone."
            ),
        },
        "category": {
            "type": "string",
            "enum": [
                "work",
                "communication",
                "entertainment",
                "browsing",
                "learning",
                "idle",
                "other",
            ],
            "description": "Best single label for the activity.",
        },
        "app": {
            "type": "string",
            "description": "Name of the application in the foreground, or empty if unclear.",
        },
        "salient_text": {
            "type": "string",
            "description": (
                "Up to 200 characters of the most informative visible text — an error "
                "message, a document title, a subject line. Empty if nothing stands out."
            ),
        },
        "should_interrupt": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {
            "type": "string",
            "description": "One short sentence explaining why (or why not).",
        },
    },
    "required": ["activity", "category", "should_interrupt", "confidence", "reason"],
}

_ASSESSMENT_PROMPT_TEMPLATE = """\
You are a screen sensor. You look at a screenshot of the user's screen, report what is
on it, and decide whether it warrants interrupting them.

Current local time: {current_time}
Current work-hours status: {work_hours_status}
Focus policy (verbatim): {focus_policy}

Pending tasks overview:
{task_overview}

Always report what you see, in the activity, category, app and salient_text fields:
- activity: one short neutral phrase for what is being done.
- category: the single best label from the allowed list.
- app: the foreground application's name if you can tell.
- salient_text: the most informative text visible, or empty.
These are sensor readings. Write them as plain factual descriptions. They are stored in a
log and read by other software — they are not a message to anybody, so do not address
anyone and do not give advice in them.

Decision rules:
- The focus policy is the only reason to interrupt. Do not invent reasons of your own.
- A clause that is conditional ("during work hours", "in the evening", ...) fires only when
  the time and work-hours status above show its condition holds right now. If the condition
  does not hold, or you cannot tell, that clause does not apply — do not assume it applies.
- If no clause clearly applies to what is on screen right now, set should_interrupt to false.

Look at this screenshot. Report what you see, then based on the focus policy, the current
time, work-hours status, and pending tasks, decide whether to interrupt the user. Return
strictly as JSON."""


# ── data types ────────────────────────────────────────────────────────


@dataclass
class ScreenAssessment:
    """Parsed and validated result from a VISION lane screen classification."""

    activity: str
    category: str
    should_interrupt: bool
    confidence: float
    reason: str
    app: str = ""
    salient_text: str = ""


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

    activity = structured.get("activity")
    if not isinstance(activity, str) or not activity.strip():
        raise ValueError("activity must be a non-empty string")

    category = structured.get("category")
    if not isinstance(category, str):
        raise ValueError(f"category must be string, got {type(category).__name__}")
    if category not in OBSERVATION_CATEGORIES:
        category = "other"

    app = structured.get("app", "")
    if not isinstance(app, str):
        raise ValueError(f"app must be string, got {type(app).__name__}")

    salient_text = structured.get("salient_text", "")
    if not isinstance(salient_text, str):
        raise ValueError(f"salient_text must be string, got {type(salient_text).__name__}")

    return ScreenAssessment(
        activity=activity,
        category=category,
        should_interrupt=bool(si),
        confidence=float(conf),
        reason=reason,
        app=app[:120],
        salient_text=salient_text[:200],
    )


# ── work-hours status helper ──────────────────────────────────────────


def _work_hours_status(now: datetime, wh: WorkHoursConfig | None) -> str:
    """Return a human-readable work-hours status string for the assessment prompt.

    The "not configured" wording states outright that the condition is *unknown*.
    Saying only "not configured" let the model treat a work-hours-conditional
    policy clause as if it were unconditional and interrupt at any hour.
    """
    if wh is None or not wh.enabled:
        return (
            "Work hours are not configured, so it is NOT known whether the user is "
            "currently within their work hours. Treat this condition as unverified."
        )
    from .scheduler import in_work_hours as _in_wh

    if _in_wh(now, wh):
        return f"Currently INSIDE work hours ({wh.start}–{wh.end})."
    return f"Currently OUTSIDE work hours ({wh.start}–{wh.end})."


def _current_time(now: datetime) -> str:
    """Format the wall-clock time for the prompt, matching the when-sensor's style."""
    return f"{now:%Y-%m-%d %H:%M} ({now:%A})"


# ── VISION lane assessor ──────────────────────────────────────────────


class PonyBrainAssessor:
    """Uses PonyBrain's VISION lane to classify the screen."""

    def __init__(self, brain: PonyBrain) -> None:  # noqa: F821 – circular
        self._brain = brain

    def assess(
        self,
        screenshot_bytes: bytes,
        *,
        current_time: str,
        work_hours_status: str,
        task_overview: str,
        focus_policy: str,
    ) -> ScreenAssessment:
        b64 = base64.b64encode(screenshot_bytes).decode()
        prompt = _ASSESSMENT_PROMPT_TEMPLATE.format(
            current_time=current_time,
            work_hours_status=work_hours_status,
            task_overview=task_overview or "(none)",
            focus_policy=focus_policy,
        )
        result = self._brain._run(
            self._brain._spec(VISION),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
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
        activity_store: Any | None = None,  # ActivityStore for logging
        observation_store: Any | None = None,  # ObservationStore for sensor readings
    ) -> None:
        self.config = config
        self.screenshot_fn = screenshot_fn
        self.assessor = assessor
        self.store = store
        self.deliver = deliver
        self.clock = clock if clock is not None else _RealClock()
        self.activity_store = activity_store
        self.observation_store = observation_store
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

        # Double-check gates each cycle (config may have changed).
        # When gates are off we sleep silently — no activity log entry.
        if not self.config.awareness.enabled or not self.config.screenshot_enabled:
            return

        # Capture and model inference may block; keep both off the GUI event loop.
        png = await asyncio.to_thread(self.screenshot_fn)
        if not png:
            log.debug("awareness: screenshot failed, skipping")
            self._record_failure("screenshot_failed", "screenshot capture returned empty")
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
                current_time=_current_time(now),
                work_hours_status=work_status,
                task_overview=task_overview,
                focus_policy=focus_policy,
            )
        except Exception as exc:
            log.exception("awareness: screen assessment failed")
            self._record_failure(self._safe_error_class(exc), str(exc))
            return

        # Cooldown suppresses speech, not perception.
        if self._in_cooldown(now):
            self._record_assessment(assessment, now=now, intervened=False)
            return

        should_intervene = (
            assessment.should_interrupt
            and assessment.confidence >= self.config.awareness.minimum_confidence
        )

        if not should_intervene:
            self._record_assessment(assessment, now=now, intervened=False)
            if assessment.confidence < self.config.awareness.minimum_confidence:
                log.debug(
                    "awareness: confidence %.2f below threshold %.2f, skipping",
                    assessment.confidence,
                    self.config.awareness.minimum_confidence,
                )
            return

        # Deliver nudge, then record that an intervention really occurred.
        message = f"\U0001f434 {assessment.reason}"
        log.info(
            "awareness: interrupting — %s (confidence=%.2f)",
            assessment.reason,
            assessment.confidence,
        )
        await self.deliver(message)
        self._record_assessment(assessment, now=now, intervened=True)

        # Record cooldown timestamp
        self.store.set_meta(_META_LAST_ALERT, str(now.timestamp()))

        # Record intervention activity (separate from the assessment entry above)
        if self.activity_store is not None:
            self.activity_store.record(
                "awareness_intervention",
                actor="awareness",
                detail=f"Screen intervention: {assessment.reason}",
            )

    # ── audit helpers ────────────────────────────────────────────────

    def _record_assessment(
        self, assessment: ScreenAssessment, *, now: datetime, intervened: bool
    ) -> None:
        """Record structured sensor output without screenshot data."""
        if self.observation_store is None:
            return
        self.observation_store.record(
            started_at=now,
            ended_at=now,
            source="vision",
            app=assessment.app,
            category=assessment.category,
            activity=assessment.activity,
            detail=assessment.salient_text,
            confidence=assessment.confidence,
            payload=_json_dumps(
                {
                    "should_interrupt": assessment.should_interrupt,
                    "reason": assessment.reason,
                    "intervened": intervened,
                }
            ),
        )

    def _record_failure(self, error_class: str, error_message: str) -> None:
        """Log a screen_assessment_failed activity entry with safe error info."""
        if self.activity_store is None:
            return
        detail = f"error={error_class}, message={error_message[:120]}"
        self.activity_store.record("screen_assessment_failed", actor="awareness", detail=detail)

    @staticmethod
    def _safe_error_class(exc: Exception) -> str:
        """Return the exception class name (no stack trace, no secrets)."""
        return type(exc).__name__

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
