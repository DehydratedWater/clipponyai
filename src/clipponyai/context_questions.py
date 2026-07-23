"""Non-annoying proactive context-gap questions.

Fires from the scheduler tick only when ALL gates pass:
- config enabled
- onboarding complete (or skipped)
- outside quiet hours
- no active silence-until
- at least min_gap_hours since last asked
- no active/pending one-time tasks (when require_empty_agenda)
- no due/uncompleted routine today
- no active unachieved goal with immediate missing check-in
- nothing else delivered this scheduler tick

Asks ONE delivered message with at most max_questions_per_batch concise
questions. Deterministic -- no LLM call needed.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta

from .config import ProactiveQuestionsConfig
from .onboarding import OnboardingManager
from .tasks import ISO, TaskStore

log = logging.getLogger("clipponyai.context_questions")

# ── Meta keys ────────────────────────────────────────────────────────
_META_LAST_ASKED = "proactive_last_asked"
_META_SILENCE_UNTIL = "proactive_silence_until"

# ── Deterministic question templates ─────────────────────────────────
QUESTION_TEMPLATES = {
    "no_routines": (
        "I notice you have no recurring routines set up yet. "
        "Do you have any daily habits or weekly tasks you'd like me to track?"
    ),
    "no_goals": (
        "You don't have any active goals tracked. "
        "Is there something you're working toward that I should help you track?"
    ),
    "no_rules": (
        "You have no accountability rules yet. "
        "Would you like automatic reminders for anything (like taking breaks or winding down)?"
    ),
}


def _in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True when inside quiet hours (handles midnight-crossing ranges)."""
    hour = now.hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _is_scheduled_on(routine, d: date) -> bool:
    """Check if routine is scheduled on date d (mirrors routines.is_scheduled_on)."""
    cadence = routine.cadence
    if cadence == "daily":
        return True
    if cadence == "weekdays":
        if routine.weekdays:
            return d.weekday() in routine.weekdays
        return d.weekday() < 5
    if cadence == "monthly":
        target = routine.day_of_month or 1
        max_day = calendar.monthrange(d.year, d.month)[1]
        return d.day == min(target, max_day)
    return False


class ProactiveQuestioner:
    """Gate-checked proactive context-gap questioner.

    No LLM calls -- questions are deterministic based on missing context.
    """

    def __init__(
        self,
        config: ProactiveQuestionsConfig,
        store: TaskStore,
        onboarding: OnboardingManager,
        quiet_hours_start: int = 23,
        quiet_hours_end: int = 8,
        activity_store: object | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.onboarding = onboarding
        self.quiet_hours_start = quiet_hours_start
        self.quiet_hours_end = quiet_hours_end
        if activity_store is None:
            from .accountability import get_stores

            activity_store = get_stores(store)["activity"]
        self.activity_store = activity_store
        self._delivered_this_tick: bool = False

    def mark_delivered_this_tick(self) -> None:
        """Call when any other scheduler message was delivered this tick."""
        self._delivered_this_tick = True

    def clear_tick(self) -> None:
        """Reset per-tick state at start of each tick."""
        self._delivered_this_tick = False

    # ── main tick entry point ───────────────────────────────────────

    async def tick(self, now: datetime | None = None, *, allow_delivery: bool = True) -> str | None:
        """One proactive check; returns the question message sent, or None."""
        now = now or datetime.now()

        # Gate 1: config enabled
        if not self.config.enabled:
            return None

        # Gate 2: onboarding must be done
        if not self.onboarding.is_done():
            return None

        # Gate 3: outside quiet hours
        if _in_quiet_hours(now, self.quiet_hours_start, self.quiet_hours_end):
            return None

        # Gate 4: no active silence
        if self.is_silenced(now):
            return None

        # Gate 5: min gap since last asked
        last_asked = self._last_asked()
        if last_asked is not None:
            gap = now - last_asked
            if gap < timedelta(hours=self.config.min_gap_hours):
                return None

        # Gate 6: nothing else delivered this tick
        if self._delivered_this_tick:
            return None

        # Gate 7: empty agenda (when configured)
        if self.config.require_empty_agenda and not self._agenda_empty(now):
            return None

        # Build questions
        questions = self._build_questions()
        if not questions:
            return None

        # Cap at max_questions_per_batch
        questions = questions[: self.config.max_questions_per_batch]

        if not allow_delivery:
            return None

        # Compose and deliver
        message = "\n\n".join(questions)

        # Persist last_asked AFTER actual delivery decision
        self.store.set_meta(_META_LAST_ASKED, now.strftime(ISO))

        # Record activity
        if self.activity_store is not None:
            self.activity_store.record(
                "proactive_questions_asked",
                actor="scheduler",
                detail=f"Asked {len(questions)} context question(s)",
            )

        log.info("proactive question batch: %d question(s)", len(questions))
        return message

    # ── agenda gate ─────────────────────────────────────────────────

    def _agenda_empty(self, now: datetime) -> bool:
        """True if no pending tasks, no due routines, no missing goal check-ins."""
        # No active/pending one-time tasks
        if self.store.pending():
            return False

        today = now.date()
        today_str = now.strftime("%Y-%m-%d")

        # No due/uncompleted routine today
        try:
            from .accountability import RoutineCompletionStore, RoutineStore

            rs = RoutineStore(self.store)
            cs = RoutineCompletionStore(self.store)
            for routine in rs.list_all():
                if not routine.enabled or routine.archived_at is not None:
                    continue
                if not _is_scheduled_on(routine, today):
                    continue
                # Check if completed or skipped today
                completed_today = False
                for c in cs.by_routine(routine.id):
                    if c.occurrence_date == today_str and c.status in ("done", "skipped"):
                        completed_today = True
                        break
                if not completed_today:
                    return False
        except Exception:
            log.exception("agenda gate: routine check failed")
            return False

        # No active unachieved goal with immediate missing check-in
        try:
            from .accountability import GoalProgressStore, GoalStore, RoutineStore

            gs = GoalStore(self.store)
            gps = GoalProgressStore(self.store)
            rs = RoutineStore(self.store)
            routine_map = {r.id: r for r in rs.list_all()}

            for goal in gs.list_all():
                if goal.status != "active":
                    continue
                if not goal.linked_routine_ids:
                    continue
                for rid in goal.linked_routine_ids:
                    routine = routine_map.get(rid)
                    if routine and _is_scheduled_on(routine, today):
                        has_progress = any(
                            e.date == today_str for e in gps.by_goal(goal.id)
                        )
                        if not has_progress:
                            return False
        except Exception:
            log.exception("agenda gate: goal check failed")
            return False

        return True

    # ── question building ───────────────────────────────────────────

    def _build_questions(self) -> list[str]:
        """Build deterministic questions based on missing context categories.

        Returns empty list if nothing useful is missing.
        """
        questions: list[str] = []

        # Check routines
        try:
            from .accountability import RoutineStore

            if not RoutineStore(self.store).list_all():
                questions.append(QUESTION_TEMPLATES["no_routines"])
        except Exception:
            log.exception("failed to check routines for questions")

        # Check goals
        try:
            from .accountability import GoalStore

            gs = GoalStore(self.store)
            active_goals = [g for g in gs.list_all() if g.status == "active"]
            if not active_goals:
                questions.append(QUESTION_TEMPLATES["no_goals"])
        except Exception:
            log.exception("failed to check goals for questions")

        # Check rules
        try:
            from .accountability import AccountabilityRuleStore

            if not AccountabilityRuleStore(self.store).list_all():
                questions.append(QUESTION_TEMPLATES["no_rules"])
        except Exception:
            log.exception("failed to check rules for questions")

        return questions

    # ── silence controls ────────────────────────────────────────────

    def silence(self, hours: int | None = None) -> None:
        """Silence proactive questions for *hours* (default from config)."""
        hours = hours or self.config.silence_default_hours
        until = datetime.now() + timedelta(hours=hours)
        self.store.set_meta(_META_SILENCE_UNTIL, until.strftime(ISO))
        log.info("proactive questions silenced for %dh", hours)

    def resume(self) -> None:
        """Resume proactive questions (clear silence)."""
        self.store.set_meta(_META_SILENCE_UNTIL, "")
        log.info("proactive questions resumed")

    def silenced_until(self, now: datetime | None = None) -> datetime | None:
        """Return the datetime until which questions are silenced, or None."""
        raw = self.store.get_meta(_META_SILENCE_UNTIL)
        if not raw:
            return None
        try:
            return datetime.strptime(raw, ISO)
        except (ValueError, TypeError):
            return None

    def is_silenced(self, now: datetime | None = None) -> bool:
        """Check if currently silenced."""
        until = self.silenced_until()
        if until is None:
            return False
        now = now or datetime.now()
        return now < until

    # ── internal helpers ────────────────────────────────────────────

    def _last_asked(self) -> datetime | None:
        """Return the last time questions were asked, or None."""
        raw = self.store.get_meta(_META_LAST_ASKED)
        if not raw:
            return None
        try:
            return datetime.strptime(raw, ISO)
        except (ValueError, TypeError):
            return None
