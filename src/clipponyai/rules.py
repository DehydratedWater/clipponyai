"""Deterministic accountability rule engine.

Evaluates time-based rules against a datetime, fires them respecting cooldown,
and delegates screen rules to a pluggable assessor protocol.  Custom rules
pass through for LLM/screen evaluation later.

Does NOT import the scheduler, app, or brain.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .accountability import (
    AccountabilityRule,
    AccountabilityRuleStore,
    ActivityStore,
)

# ─── Time-condition parsing (no regexes) ──────────────────────────────


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive start, exclusive end in minutes-from-midnight.

    cross_midnight=True means the window wraps past 24:00 (e.g. 22:00-06:00).
    """
    start_minutes: int
    end_minutes: int
    cross_midnight: bool = False


def _normalise_12h(hour: int, ampm: str) -> int:
    """Convert 12-hour clock to 0-23."""
    ampm = ampm.strip().upper().replace(".", "")
    if ampm in ("AM", "A"):
        return 0 if hour == 12 else hour
    if ampm in ("PM", "P"):
        return hour if hour == 12 else hour + 12
    raise ValueError(f"cannot parse am/pm: {ampm!r}")


def _find_ampm(text: str, pos: int) -> tuple[str, int]:
    """Return ('AM'|'PM') and the index after it, scanning from *pos*."""
    rest = text[pos:].lstrip()
    skip = len(text[pos:]) - len(rest)
    upper = rest.upper()
    if upper.startswith("P.M."):
        return "P.M.", pos + skip + 4
    if upper.startswith("PM"):
        return "PM", pos + skip + 2
    if upper.startswith("A.M."):
        return "A.M.", pos + skip + 4
    if upper.startswith("AM"):
        return "AM", pos + skip + 2
    raise ValueError(f"expected AM/PM at position {pos} in {text!r}")


def _parse_time_token(text: str, pos: int) -> tuple[int, int]:
    """Parse a time token starting at *pos*.

    Accepts: 'HH:MM', 'H:MM AM/PM', 'HH AM/PM'.
    Returns (minutes-from-midnight, next_position).
    """
    rest = text[pos:].lstrip()
    scan = pos + (len(text[pos:]) - len(rest))
    digits = ""
    while scan < len(text) and text[scan].isdigit():
        digits += text[scan]
        scan += 1
    if not digits:
        raise ValueError(f"expected digits at position {pos} in {text!r}")

    hour = int(digits)

    # HH:MM form
    if scan < len(text) and text[scan] == ":":
        scan += 1
        m_digits = ""
        while scan < len(text) and text[scan].isdigit():
            m_digits += text[scan]
            scan += 1
        if not m_digits:
            raise ValueError(f"expected minutes after ':' in {text!r}")
        minutes = hour * 60 + int(m_digits)
        # optional AM/PM after HH:MM
        after = text[scan:].lstrip().upper()
        if after.startswith(("A.M.", "AM", "P.M.", "PM")):
            suffix, scan = _find_ampm(text, scan)
            minutes = _normalise_12h(hour, suffix) * 60 + int(m_digits)
        return minutes, scan

    # H AM/PM form
    after = text[scan:].lstrip().upper()
    if after.startswith(("A.M.", "AM", "P.M.", "PM")):
        suffix, scan = _find_ampm(text, scan)
        return _normalise_12h(hour, suffix) * 60, scan

    return hour * 60, scan


def parse_time_condition(condition: str) -> TimeWindow | None:
    """Try to parse a human time condition into a TimeWindow.

    Supports:
      - "after HH:MM" / "after H PM"
      - "before HH:MM" / "before H AM"
      - "between HH:MM and HH:MM"
      - 12-hour variants: "after 10 PM", "before 8:30 AM"

    Returns None if the pattern is not recognised (custom condition).
    """
    text = condition.strip().lower()

    # --- "between X and Y" ---
    if text.startswith("between"):
        and_pos = text.find(" and ")
        if and_pos == -1:
            return None
        first_part = condition[8:and_pos]  # keep original casing for time tokens
        second_part = condition[and_pos + 5 :]
        try:
            start, _ = _parse_time_token(first_part, 0)
            end, _ = _parse_time_token(second_part, 0)
        except ValueError:
            return None
        cross = end <= start
        return TimeWindow(start_minutes=start, end_minutes=end, cross_midnight=cross)

    # --- "after X" ---
    if text.startswith("after"):
        time_part = condition[5:]
        try:
            minutes, _ = _parse_time_token(time_part, 0)
        except ValueError:
            return None
        return TimeWindow(start_minutes=minutes, end_minutes=24 * 60)

    # --- "before X" ---
    if text.startswith("before"):
        time_part = condition[6:]
        try:
            minutes, _ = _parse_time_token(time_part, 0)
        except ValueError:
            return None
        return TimeWindow(start_minutes=0, end_minutes=minutes)

    return None


def time_in_window(dt: datetime, window: TimeWindow) -> bool:
    """Check if *dt* falls inside *window*."""
    m = dt.hour * 60 + dt.minute
    if window.cross_midnight:
        return m >= window.start_minutes or m < window.end_minutes
    return window.start_minutes <= m < window.end_minutes


# ─── Screen-assessor protocol ─────────────────────────────────────────


@dataclass(frozen=True)
class ScreenAssessment:
    """Result of a grounded screen assessment against an existing rule."""
    rule_id: int
    confidence: float  # 0.0-1.0


class ScreenAssessor(Protocol):
    """Pluggable protocol for screen-content assessment.

    Implementations must confirm a *grounded* existing rule ID and return
    a confidence score.  The engine never inspects the screen itself.
    """

    def assess(self, screen_context: str, candidate_rule_ids: list[int]) -> ScreenAssessment | None: ...  # noqa: E704


# ─── Delivery protocol ────────────────────────────────────────────────


class DeliveryFn(Protocol):
    """Async-compatible message delivery callback."""

    def __call__(self, message: str, rule_id: int) -> None | asyncio.Future[None]: ...  # noqa: E704


# ─── RuleEngine ───────────────────────────────────────────────────────


class RuleEngine:
    """Deterministic rule evaluation and firing layer.

    Reads rules from an AccountabilityRuleStore, evaluates time conditions,
    enforces per-rule cooldown, and fires via an async-compatible delivery
    callback.  Activity is recorded through an ActivityStore.
    """

    def __init__(
        self,
        rule_store: AccountabilityRuleStore,
        activity_store: ActivityStore,
        delivery: DeliveryFn | None = None,
        screen_assessor: ScreenAssessor | None = None,
    ) -> None:
        self._rule_store = rule_store
        self._activity_store = activity_store
        self._delivery = delivery
        self._screen_assessor = screen_assessor

    # -- public query --

    def evaluate_time(self, now: datetime) -> list[AccountabilityRule]:
        """Return enabled time-rules whose condition matches *now* and are
        past their cooldown.  Does NOT fire them."""
        matching: list[AccountabilityRule] = []
        for rule in self._rule_store.list_all():
            if not rule.enabled or rule.rule_type != "time":
                continue
            window = parse_time_condition(rule.condition)
            if window is None:
                continue
            if not time_in_window(now, window):
                continue
            if self._in_cooldown(rule, now):
                continue
            matching.append(rule)
        return matching

    # -- screen evaluation (internal) --

    def evaluate_screen(self, now: datetime, screen_context: str) -> list[AccountabilityRule]:
        """Evaluate screen rules against *screen_context* via the assessor.

        Returns rules that should fire (grounded, past cooldown).
        """
        if self._screen_assessor is None:
            return []

        screen_rules = [
            r for r in self._rule_store.list_all()
            if r.enabled and r.rule_type == "screen"
        ]
        if not screen_rules:
            return []

        assessment = self._screen_assessor.assess(
            screen_context, [r.id for r in screen_rules]
        )
        if assessment is None:
            return []

        # Must be a grounded existing rule ID
        grounded = any(r.id == assessment.rule_id for r in screen_rules)
        if not grounded or assessment.confidence <= 0.0:
            return []

        try:
            target = self._rule_store.get(assessment.rule_id)
        except KeyError:
            return []

        if self._in_cooldown(target, now):
            return []

        return [target]

    # -- tick (fire) --

    def tick(
        self,
        now: datetime,
        *,
        screen_context: str | None = None,
        allow_delivery: bool = True,
    ) -> list[AccountabilityRule]:
        """Evaluate and fire rules at *now*.

        - Time rules: fired deterministically when condition matches.
        - Screen rules: only evaluated when *screen_context* is explicitly
          supplied AND a screen assessor is configured.  The assessor must
          return a grounded existing rule ID.
        - Custom rules: not auto-fired (reserved for LLM/screen pipeline).

        Returns the list of rules that were fired.
        """
        fired: list[AccountabilityRule] = []

        # Time rules — always evaluated
        for rule in self.evaluate_time(now):
            self._fire_rule(rule, now, allow_delivery=allow_delivery)
            fired.append(rule)

        # Screen rules — only when screen_context explicitly supplied
        if screen_context is not None:
            for rule in self.evaluate_screen(now, screen_context):
                self._fire_rule(rule, now, allow_delivery=allow_delivery)
                fired.append(rule)

        return fired

    # -- helpers --

    def _in_cooldown(self, rule: AccountabilityRule, now: datetime) -> bool:
        if rule.cooldown_minutes <= 0 or rule.last_fired_at is None:
            return False
        return (now - rule.last_fired_at) < timedelta(minutes=rule.cooldown_minutes)

    def _fire_rule(
        self,
        rule: AccountabilityRule,
        now: datetime,
        *,
        allow_delivery: bool = True,
    ) -> None:
        """Record a rule fire: update last_fired_at, log activity, deliver message."""
        message = rule.message or self._default_message(rule)

        # Record fire in rule store (updates last_fired_at to injected now)
        self._rule_store.record_fire_at(rule.id, now)

        # Record activity log entry
        self._activity_store.record(
            "rule_fired",
            actor="rule_engine",
            detail=message,
            ref_type="accountability_rule",
            ref_id=str(rule.id),
        )

        # Async-compatible delivery (fire-and-forget when allow_delivery)
        if allow_delivery and self._delivery is not None:
            result = self._delivery(message, rule.id)
            if isinstance(result, asyncio.Future):
                result.add_done_callback(self._log_delivery_error)

    @staticmethod
    def _default_message(rule: AccountabilityRule) -> str:
        """Generate a sensible default message from the rule title."""
        return f"Rule triggered: {rule.title}"

    @staticmethod
    def _log_delivery_error(future: asyncio.Future) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"[rule_engine] delivery error: {exc}", file=sys.stderr)


# ─── Standalone validation helpers ────────────────────────────────────


def validate_add_rule(
    title: str,
    *,
    rule_type: str = "custom",
    condition: str = "",
    cooldown_minutes: int = 0,
) -> None:
    """Validate parameters for adding a new rule.

    Raises ValueError on invalid input.
    """
    if not title.strip():
        raise ValueError("rule title must not be empty")
    if not condition.strip():
        raise ValueError("rule condition must not be empty")
    if rule_type not in ("time", "screen", "custom"):
        raise ValueError(f"invalid rule_type {rule_type!r}")
    if cooldown_minutes < 0:
        raise ValueError("cooldown_minutes must be non-negative")


def validate_update_rule(
    *,
    title: str | None = None,
    rule_type: str | None = None,
    condition: str | None = None,
    cooldown_minutes: int | None = None,
) -> None:
    """Validate parameters for updating a rule.

    Raises ValueError on invalid input.
    """
    if title is not None and not title.strip():
        raise ValueError("rule title must not be empty")
    if condition is not None and not condition.strip():
        raise ValueError("rule condition must not be empty")
    if rule_type is not None and rule_type not in ("time", "screen", "custom"):
        raise ValueError(f"invalid rule_type {rule_type!r}")
    if cooldown_minutes is not None and cooldown_minutes < 0:
        raise ValueError("cooldown_minutes must be non-negative")
