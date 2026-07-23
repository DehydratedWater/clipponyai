"""First-run onboarding manager — persisted via TaskStore meta keys only.

Collects initial context through the chat interface (no GUI wizard).
States: new, in_progress, completed, skipped.

Uses TaskStore meta keys:
  onboarding_status        — new | in_progress | completed | skipped
  onboarding_started_at    — ISO timestamp when begin() was called
  onboarding_last_prompt   — last prompt text delivered
  onboarding_collected     — JSON list of collected category booleans
"""

from __future__ import annotations

import json
from datetime import datetime

from .tasks import ISO, TaskStore

# ── Meta key constants ──────────────────────────────────────────────
_META_STATUS = "onboarding_status"
_META_STARTED_AT = "onboarding_started_at"
_META_LAST_PROMPT = "onboarding_last_prompt"
_META_COLLECTED = "onboarding_collected"

# Categories we try to collect during onboarding
ALL_CATEGORIES = [
    "name_style",   # preferred name / communication style
    "work_hours",   # typical work hours
    "routines",     # daily/weekly/monthly responsibilities
    "goals",        # active goals or projects
    "rules",        # accountability boundaries/preferences
]

_CATEGORY_ALIASES = {
    "recurring_tasks": "routines",
    "current_goals": "goals",
    "accountability": "rules",
}

# Friendly initial prompt — a concise batch of 4-5 questions
INITIAL_PROMPT = (
    "Hi! I'm your new desktop assistant. Before we get started, "
    "I'd love to know a few things so I can help you better. "
    "You can answer all of these, just some, or skip entirely:\n\n"
    "1. What should I call you, and do you prefer gentle nudges or direct reminders?\n"
    "2. What are your typical work hours?\n"
    "3. Do you have any recurring responsibilities (daily habits, weekly meetings, monthly tasks)? "
    "I can set them up as routines.\n"
    "4. What goals or projects are you working on right now?\n"
    "5. Any boundaries for accountability — times or topics I should stay quiet about?\n\n"
    "Answer whatever feels useful, and say 'that's enough' when you're done setting up."
)


class OnboardingManager:
    """Manage first-run onboarding state via TaskStore meta keys.

    No LLM calls — pure state machine with persisted meta.
    """

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    # ── state queries ───────────────────────────────────────────────

    def status(self) -> str:
        """Return current onboarding status string."""
        return self.store.get_meta(_META_STATUS, "new") or "new"

    def is_new(self) -> bool:
        return self.status() == "new"

    def is_in_progress(self) -> bool:
        return self.status() == "in_progress"

    def is_done(self) -> bool:
        """True if completed or skipped."""
        return self.status() in ("completed", "skipped")

    def started_at(self) -> datetime | None:
        raw = self.store.get_meta(_META_STARTED_AT)
        if raw is None:
            return None
        return datetime.strptime(raw, ISO)

    def last_prompt(self) -> str | None:
        return self.store.get_meta(_META_LAST_PROMPT)

    # ── state transitions ───────────────────────────────────────────

    def begin(self) -> str:
        """Transition to in_progress. Returns the initial prompt text.

        Idempotent: does not override completed or skipped states.
        """
        if self.status() in ("completed", "skipped"):
            return INITIAL_PROMPT
        now = datetime.now().strftime(ISO)
        self.store.set_meta(_META_STATUS, "in_progress")
        self.store.set_meta(_META_STARTED_AT, now)
        self.store.set_meta(_META_LAST_PROMPT, INITIAL_PROMPT)
        self.store.set_meta(_META_COLLECTED, json.dumps([]))
        return INITIAL_PROMPT

    def complete(self) -> None:
        """Mark onboarding as completed. No-op if it has not begun."""
        if self.status() != "in_progress":
            return
        self.store.set_meta(_META_STATUS, "completed")

    def skip(self) -> None:
        """Mark onboarding as skipped. No-op if already done."""
        if self.status() in ("completed", "skipped"):
            return
        self.store.set_meta(_META_STATUS, "skipped")

    def reset(self) -> None:
        """Reset to new state (allow re-onboarding)."""
        self.store.set_meta(_META_STATUS, "new")
        self.store.set_meta(_META_STARTED_AT, "")
        self.store.set_meta(_META_LAST_PROMPT, "")
        self.store.set_meta(_META_COLLECTED, "")

    # ── prompt tracking ─────────────────────────────────────────────

    def record_prompt(self, prompt: str) -> None:
        """Record that a prompt was delivered (for first-launch-once logic)."""
        self.store.set_meta(_META_LAST_PROMPT, prompt)

    def prompt_was_delivered(self) -> bool:
        """True if the initial prompt was already delivered."""
        return bool(self.store.get_meta(_META_LAST_PROMPT))

    # ── collected categories ────────────────────────────────────────

    def get_collected(self) -> list[str]:
        """Return list of collected category names."""
        raw = self.store.get_meta(_META_COLLECTED)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    def mark_collected(self, category: str) -> None:
        """Record that a category was collected during onboarding."""
        category = _CATEGORY_ALIASES.get(category, category)
        if category not in ALL_CATEGORIES:
            return
        collected = self.get_collected()
        if category not in collected:
            collected.append(category)
            self.store.set_meta(_META_COLLECTED, json.dumps(collected))

    def missing_categories(self) -> list[str]:
        """Return categories not yet collected."""
        collected = set(self.get_collected())
        return [c for c in ALL_CATEGORIES if c not in collected]

    # ── context note for PonyBrain grounding ────────────────────────

    def context_note(self) -> str | None:
        """Return a grounding note for the system prompt when onboarding is active.

        Returns None when onboarding is not in progress.
        """
        if not self.is_in_progress():
            return None

        missing = self.missing_categories()
        parts = [
            "[onboarding context — the user is setting up for the first time.]",
        ]

        if missing:
            parts.append(
                f"Still need: {', '.join(missing)}. "
                "Ask only the missing items in a small batch (2-3 at most)."
            )
        else:
            parts.append("All initial information collected.")

        parts.append(
            "Extract their answers into existing tools: "
            "add_routine for recurring items, add_goal for goals, "
            "add_rule for boundaries, add_task for one-time items. "
            "When the user says setup is done or you have enough info, "
            "call complete_onboarding."
        )

        return "\n".join(parts)
