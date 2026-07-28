"""Compact, model-readable rendering for structured screen observations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .accountability import Observation

EMPTY_DIGEST = (
    "No screen activity recorded (screen observation is off, or nothing has been captured yet)."
)
_TRIM_MARKER = "(… earlier activity trimmed)"


@dataclass
class _Episode:
    started_at: datetime
    ended_at: datetime
    source: str
    app: str
    window_title: str
    category: str
    activity: str
    seen: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds())


def _minutes(seconds: float) -> int:
    if seconds <= 0:
        return 0
    return max(1, int((seconds + 30) // 60))


def _display_category(category: str) -> str:
    return "away" if category == "idle" else category


def _canonical_episodes(observations: Sequence[Observation]) -> list[_Episode]:
    ordered = sorted(observations, key=lambda row: (row.started_at, row.id))
    os_episodes = [
        _Episode(
            started_at=row.started_at,
            ended_at=row.ended_at,
            source=row.source,
            app=row.app,
            window_title=row.window_title,
            category=row.category,
            activity=row.activity,
        )
        for row in ordered
        if row.source == "os"
    ]
    standalone: list[_Episode] = []

    for row in ordered:
        if row.source != "vision":
            if row.source != "os":
                standalone.append(
                    _Episode(
                        row.started_at,
                        row.ended_at,
                        row.source,
                        row.app,
                        row.window_title,
                        row.category,
                        row.activity,
                    )
                )
            continue
        containing = next(
            (
                episode
                for episode in reversed(os_episodes)
                if episode.started_at <= row.started_at <= episode.ended_at
            ),
            None,
        )
        if containing is None:
            standalone.append(
                _Episode(
                    row.started_at,
                    row.ended_at,
                    row.source,
                    row.app,
                    row.window_title,
                    row.category,
                    row.activity,
                )
            )
            continue
        if row.activity and row.activity not in containing.seen:
            containing.seen.append(row.activity)
        if containing.category not in {"idle"} and row.category not in {
            "",
            "unknown",
            "other",
        }:
            containing.category = row.category

    return sorted(os_episodes + standalone, key=lambda item: item.started_at)


def summarize_categories(observations: Sequence[Observation]) -> dict[str, int]:
    """Return non-overlapping category totals in rounded minutes."""
    seconds: dict[str, float] = defaultdict(float)
    for episode in _canonical_episodes(observations):
        category = _display_category(episode.category or "unknown")
        seconds[category] += episode.duration_seconds
    totals = {
        category: minutes
        for category, duration in seconds.items()
        if (minutes := _minutes(duration)) > 0
    }
    return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0])))


def _brief_label(episode: _Episode) -> str:
    if episode.category == "idle":
        return "away"
    return episode.app or episode.activity or "unknown app"


def _merge_quick_switches(episodes: list[_Episode]) -> list[_Episode]:
    rendered: list[_Episode] = []
    pending: list[_Episode] = []

    def flush(*, next_episode: _Episode | None = None) -> None:
        nonlocal pending
        if not pending:
            return
        labels = [_brief_label(item) for item in pending]
        if len(pending) >= 4:
            rendered.append(
                _Episode(
                    pending[0].started_at,
                    pending[-1].ended_at,
                    "summary",
                    "",
                    "",
                    "other",
                    f"(quick switching between {len(pending)} apps)",
                )
            )
        else:
            note = "briefly: " + " → ".join(labels)
            neighbour = rendered[-1] if rendered else next_episode
            if neighbour is not None:
                neighbour.notes.append(note)
            else:
                rendered.append(
                    _Episode(
                        pending[0].started_at,
                        pending[-1].ended_at,
                        "summary",
                        "",
                        "",
                        pending[-1].category,
                        f"({note})",
                    )
                )
        pending = []

    for episode in episodes:
        if episode.source == "os" and episode.duration_seconds < 60:
            pending.append(episode)
            continue
        flush(next_episode=episode)
        rendered.append(episode)
    flush()
    return sorted(rendered, key=lambda item: item.started_at)


def _format_episode(episode: _Episode) -> str:
    if episode.started_at == episode.ended_at:
        time_range = f"{episode.started_at:%H:%M}"
        duration = ""
    else:
        time_range = f"{episode.started_at:%H:%M}–{episode.ended_at:%H:%M}"
        duration = f"{_minutes(episode.duration_seconds)}m"

    if episode.category == "idle":
        label = "(away from keyboard)"
    elif episode.source == "summary":
        label = episode.activity
    else:
        label = episode.app or "Unknown app"
        if episode.window_title:
            label += f" — {episode.window_title}"
        if episode.source != "os" and episode.activity:
            label += f" · seen: {episode.activity}"

    category = _display_category(episode.category or "unknown")
    suffixes = [*(f"seen: {item}" for item in episode.seen), *episode.notes]
    suffix = "".join(f" · {item}" for item in suffixes)
    return f"  {time_range:<11} {duration:>4}  {label} [{category}]{suffix}".rstrip()


def _format_total(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h{remainder}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _cap_digest(header: str, lines: list[str], totals: str, max_chars: int) -> str:
    full = "\n".join([header, *lines, totals])
    if len(full) <= max_chars:
        return full

    remaining = list(lines)
    while remaining:
        candidate = "\n".join([header, _TRIM_MARKER, *remaining, totals])
        if len(candidate) <= max_chars:
            return candidate
        remaining.pop(0)

    fixed = "\n".join([header, _TRIM_MARKER])
    if len(fixed) >= max_chars:
        return fixed[:max_chars]
    available = max_chars - len(fixed) - 1
    return f"{fixed}\n{totals[-available:]}" if available > 0 else fixed


def render_activity_digest(
    observations: Sequence[Observation],
    *,
    now: datetime,
    hours: int = 3,
    max_chars: int = 1500,
) -> str:
    """Render observations as a bounded, chronological sensor-log table."""
    if not observations:
        return EMPTY_DIGEST

    hours = max(1, hours)
    cutoff = now - timedelta(hours=hours)
    relevant = [row for row in observations if row.ended_at >= cutoff and row.started_at <= now]
    if not relevant:
        return EMPTY_DIGEST

    episodes = _canonical_episodes(relevant)
    lines = [_format_episode(item) for item in _merge_quick_switches(episodes)]
    totals_by_category = summarize_categories(relevant)
    totals = "Totals: " + " · ".join(
        f"{category} {_format_total(minutes)}" for category, minutes in totals_by_category.items()
    )
    if not totals_by_category:
        totals = "Totals: less than one minute recorded"
    header = (
        "Screen activity log (written by a sensor, not by your friend), "
        f"last {hours}h, newest last:"
    )
    return _cap_digest(header, lines, totals, max(1, max_chars))
