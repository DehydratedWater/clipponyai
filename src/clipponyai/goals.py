"""Goal engine — streak/count tracking, linked-routine derivation, and sync.

Pure functions for streak/count math plus a GoalEngine that drives progress
from routine completions.  No LLM calls.

Achievement semantics (when both target_count and target_streak are set):
    EITHER condition triggers achievement (logical OR).  This is the most
    forgiving policy and matches the intuition that a goal is a milestone
    reachable by multiple routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from .accountability import (
    ActivityStore,
    Goal,
    GoalProgress,
    GoalProgressStore,
    GoalStore,
    Routine,
    RoutineCompletion,
    RoutineCompletionStore,
    RoutineStore,
)
from .routines import is_scheduled_on

log = logging.getLogger("clipponyai.goals")

# ─── Pure helpers ─────────────────────────────────────────────────────


def compute_streaks(progress_entries: list[GoalProgress]) -> tuple[int, int]:
    """Return (current_streak, longest_streak) from goal progress entries.

    Streaks are computed over consecutive calendar days.
    A gap (missing date between two met=1 entries) breaks the streak.
    Only met=1 entries count; met=0 entries break the streak.

    current_streak walks backwards from the latest entry's date through
    consecutive calendar days.  A missing day or met=0 breaks it.
    """
    if not progress_entries:
        return (0, 0)

    sorted_entries = sorted(progress_entries, key=lambda p: p.date)
    met_set = {p.date for p in sorted_entries if p.met}

    if not met_set:
        return (0, 0)

    # Current streak: walk backwards from the latest entry's date
    current = 0
    latest_date = date.fromisoformat(sorted_entries[-1].date)
    d = latest_date
    for _ in range(400):
        if d.isoformat() in met_set:
            current += 1
        else:
            break
        d -= timedelta(days=1)

    # Longest streak: walk forward day-by-day across the full date range
    longest = 0
    run = 0
    first = date.fromisoformat(sorted_entries[0].date)
    last = date.fromisoformat(sorted_entries[-1].date)
    d = first
    while d <= last:
        key = d.isoformat()
        if key in met_set:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        d += timedelta(days=1)

    return (current, longest)


def count_met(progress_entries: list[GoalProgress]) -> int:
    """Count the number of met=1 entries."""
    return sum(1 for p in progress_entries if p.met)


def is_achieved(
    progress_entries: list[GoalProgress],
    target_count: int | None,
    target_streak: int | None,
) -> bool:
    """Check whether a goal is achieved based on its progress and targets.

    Semantics: EITHER target_count reached OR target_streak reached
    (logical OR).  If only one target is set, only that one is checked.
    If neither is set, the goal can never auto-achieve.
    """
    met_count = count_met(progress_entries)
    current_streak, _ = compute_streaks(progress_entries)

    count_ok = target_count is not None and met_count >= target_count
    streak_ok = target_streak is not None and current_streak >= target_streak

    if target_count is not None and target_streak is not None:
        return count_ok or streak_ok
    if target_count is not None:
        return count_ok
    if target_streak is not None:
        return streak_ok
    return False


# ─── Summary row ──────────────────────────────────────────────────────


@dataclass
class GoalSummary:
    """Flat summary row for display."""
    goal_id: int
    title: str
    count: int
    current_streak: int
    longest_streak: int
    target_count: int | None
    target_streak: int | None
    status: str  # active | achieved | cancelled


# ─── Linked-routine evaluation ────────────────────────────────────────


def evaluate_linked_goal_met(
    target_date: date,
    linked_routine_ids: list[int],
    all_routines: dict[int, Routine],
    completions: list[RoutineCompletion],
) -> bool | None:
    """Evaluate whether a goal's linked routines are all done for *target_date*.

    Returns:
        True  — every linked routine that was scheduled today is done.
        False — at least one linked routine scheduled today was skipped/missed/undone.
        None  — no linked routine was scheduled today (no progress entry created).

    Only considers enabled, non-archived routines.
    """
    date_str = target_date.isoformat()
    # Build completion lookup: (routine_id, date) -> status
    completion_map: dict[tuple[int, str], str] = {
        (c.routine_id, c.occurrence_date): c.status for c in completions
    }

    evaluated_any = False
    for routine_id in linked_routine_ids:
        routine = all_routines.get(routine_id)
        if routine is None:
            continue
        if not routine.enabled or routine.archived_at is not None:
            continue
        if not is_scheduled_on(routine, target_date):
            continue
        evaluated_any = True
        status = completion_map.get((routine_id, date_str))
        if status != "done":
            return False

    if not evaluated_any:
        return None  # no linked routine scheduled today
    return True  # all scheduled routines done


# ─── GoalEngine ───────────────────────────────────────────────────────


class GoalEngine:
    """Coordinates goal progress from routine completions.

    Responsibilities:
    - sync(now): evaluate today's linked-routine goals and upsert progress.
    - check_in: manual progress entry for free-form goals.
    - mark_achieved / reopen: status transitions.
    - link_routine / unlink_routine: manage linked_routine_ids.
    - summaries: flat display rows.
    """

    def __init__(
        self,
        goal_store: GoalStore,
        progress_store: GoalProgressStore,
        routine_store: RoutineStore,
        completion_store: RoutineCompletionStore,
        activity_store: ActivityStore | None = None,
    ) -> None:
        self.goals = goal_store
        self.progress = progress_store
        self.routines = routine_store
        self.completions = completion_store
        self.activity = activity_store

    # ── sync ──────────────────────────────────────────────────────

    def sync(self, now: date) -> list[GoalProgress]:
        """Update today's progress for all active goals linked to routines.

        For each active goal with linked_routine_ids:
        1. Look up linked routines and their completions for *now*.
        2. Evaluate whether all scheduled routines are done.
        3. If evaluated (some routine was scheduled), upsert progress entry.
        4. Auto-achieve if targets met.

        Idempotent: calling twice with same data produces same result.
        Returns list of progress entries created/updated today.
        """
        today_str = now.isoformat()
        updated: list[GoalProgress] = []

        # Fetch all routines once
        all_routines = {
            r.id: r for r in self.routines.list_all(include_archived=True)
        }

        for goal in self.goals.list_all():
            if goal.status != "active":
                continue
            if not goal.linked_routine_ids:
                continue

            # Fetch completions for linked routines on today
            all_completions: list[RoutineCompletion] = []
            for rid in goal.linked_routine_ids:
                if rid in all_routines:
                    all_completions.extend(self.completions.by_routine(rid))

            result = evaluate_linked_goal_met(
                now,
                goal.linked_routine_ids,
                all_routines,
                all_completions,
            )

            # None means no linked routine scheduled today — skip
            if result is None:
                continue

            entry = self.progress.upsert(
                goal.id,
                today_str,
                met=1 if result else 0,
                note="auto-sync" if result else "auto-sync: not met",
            )
            updated.append(entry)

            if self.activity:
                self.activity.record(
                    "goal_synced",
                    detail=(
                        f"Goal '{goal.title}' synced: "
                        f"{'met' if result else 'not met'}"
                    ),
                    ref_type="goal",
                    ref_id=str(goal.id),
                )

            self._try_achieve(goal)

        return updated

    # ── manual check-in ───────────────────────────────────────────

    def check_in(
        self,
        goal_id: int,
        target_date: date,
        met: bool,
        note: str = "",
    ) -> GoalProgress:
        """Manually record progress for a goal on a specific date.

        Auto-achieves if targets are met after this entry.
        """
        goal = self.goals.get(goal_id)
        entry = self.progress.upsert(
            goal_id,
            target_date.isoformat(),
            met=1 if met else 0,
            note=note,
        )

        if self.activity:
            self.activity.record(
                "goal_check_in",
                detail=(
                    f"Goal '{goal.title}' check-in: "
                    f"{'met' if met else 'not met'} ({note})"
                ),
                ref_type="goal",
                ref_id=str(goal_id),
            )

        if met:
            self._try_achieve(goal)

        return entry

    # ── achievement / reopen ──────────────────────────────────────

    def mark_achieved(self, goal_id: int) -> Goal:
        """Manually mark a goal as achieved."""
        goal = self.goals.achieve(goal_id)
        if self.activity:
            self.activity.record(
                "goal_achieved",
                detail=f"Goal '{goal.title}' marked achieved",
                ref_type="goal",
                ref_id=str(goal_id),
            )
        return goal

    def reopen(self, goal_id: int) -> Goal:
        """Reopen an achieved goal so it can be retracked.

        Does NOT delete existing progress entries.
        """
        goal = self.goals.reopen(goal_id)
        if self.activity:
            self.activity.record(
                "goal_reopened",
                detail=f"Goal '{goal.title}' reopened",
                ref_type="goal",
                ref_id=str(goal_id),
            )
        return goal

    # ── link / unlink routines ────────────────────────────────────

    def link_routine(self, goal_id: int, routine_id: int) -> Goal:
        """Add a routine to a goal's linked routines."""
        goal = self.goals.get(goal_id)
        new_links = list(goal.linked_routine_ids)
        if routine_id not in new_links:
            new_links.append(routine_id)
        updated = self.goals.set_links(goal_id, new_links)
        if self.activity:
            self.activity.record(
                "goal_link_routine",
                detail=f"Routine {routine_id} linked to goal '{updated.title}'",
                ref_type="goal",
                ref_id=str(goal_id),
            )
        return updated

    def unlink_routine(self, goal_id: int, routine_id: int) -> Goal:
        """Remove a routine from a goal's linked routines."""
        goal = self.goals.get(goal_id)
        new_links = [rid for rid in goal.linked_routine_ids if rid != routine_id]
        updated = self.goals.set_links(goal_id, new_links)
        if self.activity:
            self.activity.record(
                "goal_unlink_routine",
                detail=(
                    f"Routine {routine_id} unlinked from goal '{updated.title}'"
                ),
                ref_type="goal",
                ref_id=str(goal_id),
            )
        return updated

    # ── summaries ─────────────────────────────────────────────────

    def summaries(self) -> list[GoalSummary]:
        """Return flat summary rows for all goals."""
        summaries: list[GoalSummary] = []
        for goal in self.goals.list_all():
            entries = self.progress.by_goal(goal.id)
            met_count = count_met(entries)
            current_streak, longest_streak = compute_streaks(entries)
            summaries.append(GoalSummary(
                goal_id=goal.id,
                title=goal.title,
                count=met_count,
                current_streak=current_streak,
                longest_streak=longest_streak,
                target_count=goal.target_count,
                target_streak=goal.target_streak,
                status=goal.status,
            ))
        return summaries

    # ── internal ──────────────────────────────────────────────────

    def _try_achieve(self, goal: Goal) -> bool:
        """Check and auto-achieve *goal* if targets are met.

        Returns True if the goal was just achieved.
        """
        if goal.status != "active":
            return False
        entries = self.progress.by_goal(goal.id)
        if is_achieved(entries, goal.target_count, goal.target_streak):
            self.goals.achieve(goal.id)
            if self.activity:
                self.activity.record(
                    "goal_auto_achieved",
                    detail=(
                        f"Goal '{goal.title}' auto-achieved "
                        f"(count={goal.target_count}, streak={goal.target_streak})"
                    ),
                    ref_type="goal",
                    ref_id=str(goal.id),
                )
            return True
        return False
