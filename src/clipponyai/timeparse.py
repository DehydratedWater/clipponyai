"""Offline fallback time parser (English only, deterministic).

The PRIMARY time parser is a small fast LLM call (`PonyBrain.parse_when`) —
language-agnostic and far more flexible. This module exists so reminders keep
working when the provider is unreachable, and as a stable base for tests.
Returns None when nothing parses, so callers can ask instead of guessing.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_REL_RE = re.compile(
    r"^in\s+(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$"
)
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2}))?$")

_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def _clock(m: re.Match) -> tuple[int, int] | None:
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _at_clock(base: datetime, hour: int, minute: int) -> datetime:
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """Parse a human time phrase into an absolute local datetime.

    Supports: "in 20m / 2h / 3 days", "tomorrow [at 10[:30] [pm]]",
    "today at 17", "at 17:30" / "5pm" (next occurrence), weekday names
    ("friday [at 9]"), "tonight", "noon", "midnight", "next week",
    and ISO "2026-08-01 [14:30]".
    """
    now = now or datetime.now()
    t = " ".join(text.strip().lower().split())
    if not t:
        return None
    t = re.sub(r"^(remind me|by|on|around)\s+", "", t)

    if m := _ISO_RE.match(t):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = int(m.group(4) or 9), int(m.group(5) or 0)
        try:
            return datetime(y, mo, d, h, mi)
        except ValueError:
            return None

    if m := _REL_RE.match(t):
        minutes = float(m.group(1)) * _UNIT_MINUTES[m.group(2)[0]]
        return now + timedelta(minutes=minutes)

    if t in {"now", "asap"}:
        return now
    if t == "noon":
        target = _at_clock(now, 12, 0)
        return target if target > now else target + timedelta(days=1)
    if t == "midnight":
        return _at_clock(now, 0, 0) + timedelta(days=1)
    if t in {"tonight", "this evening"}:
        target = _at_clock(now, 20, 0)
        return target if target > now else target + timedelta(minutes=30)
    if t in {"next week", "in a week"}:
        return _at_clock(now + timedelta(days=7), 9, 0)
    if t in {"tomorrow morning"}:
        return _at_clock(now + timedelta(days=1), 9, 0)
    if t in {"tomorrow evening", "tomorrow night"}:
        return _at_clock(now + timedelta(days=1), 20, 0)

    if m := re.match(r"^tomorrow(?:\s+at\s+(.+))?$", t):
        base = now + timedelta(days=1)
        if m.group(1) and (cm := _CLOCK_RE.match(m.group(1))) and (hm := _clock(cm)):
            return _at_clock(base, *hm)
        return _at_clock(base, 9, 0)

    if m := re.match(r"^today(?:\s+at\s+(.+))?$", t):
        if m.group(1) and (cm := _CLOCK_RE.match(m.group(1))) and (hm := _clock(cm)):
            return _at_clock(now, *hm)
        return None

    if m := re.match(rf"^(?:next\s+)?({'|'.join(_WEEKDAYS)})(?:\s+at\s+(.+))?$", t):
        days_ahead = (_WEEKDAYS[m.group(1)] - now.weekday()) % 7 or 7
        base = now + timedelta(days=days_ahead)
        if m.group(2) and (cm := _CLOCK_RE.match(m.group(2))) and (hm := _clock(cm)):
            return _at_clock(base, *hm)
        return _at_clock(base, 9, 0)

    if m := re.match(r"^at\s+(.+)$", t):
        t = m.group(1)
    if (cm := _CLOCK_RE.match(t)) and (hm := _clock(cm)) is not None:
        target = _at_clock(now, *hm)
        return target if target > now else target + timedelta(days=1)

    return None
