"""The pony's brain: open-agent-compiler interactive tier + grounded sensors.

Fast-slow architecture (the framework's canonical split, in-process):
- every chat turn runs on the provider's FAST model with the task tools;
- the ``deep_think`` tool escalates a hard question to the SLOW model;
- the ``look_at_screen`` tool captures the screen (only when enabled in
  config) and asks the VISION model what's there.

Language understanding is never regex (fren v4 rule): anything that needs to
understand a phrase — "did they say something got finished?", "what time is
'jutro wieczorem'?", "is this a promise?" — is a SMALL FAST LLM CALL with a
deliberately tiny context, so it stays cheap, accurate and language-agnostic.
Regex/tokens are used only for mechanical grounding: parsing "#12", checking
that a sensor's proposed match actually shares words with a real database
row, collapsing duplicate titles. LLM output is never ground truth — every
state change goes through the store against real rows, and listings shown to
the user come verbatim from the database.

Conversation history is plain text persisted in the task store, shared by
every surface (desktop bubble, Telegram, …) — it is ONE friend on ONE
ongoing conversation, whatever door they come in through.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from open_agent_compiler import (
    AgentDefinition,
    AgentHeader,
    ToolDefinition,
    ToolDefinitionHeader,
)
from open_agent_compiler.interactive import build_interactive_spec, run_interactive
from open_agent_compiler.interactive.spec import ToolSpec

from .accountability import ActivityStore, get_stores
from .characters import build_system_prompt, get_character
from .config import Config
from .goals import GoalEngine
from .logwatch import read_recent_logs
from .providers import FAST, SLOW, VISION, make_live_profile
from .routines import RoutineEngine, current_streak
from .rules import RuleEngine
from .tasks import TaskStore, content_tokens
from .timeparse import parse_when as parse_when_offline
from .token_capture import (
    RawResponseOpenAICompatClient,
    TokenCallback,
    TokenCaptureClient,
    lane_from_agent_id,
    purpose_from_agent_id,
)

log = logging.getLogger("clipponyai.brain")

# words hinting an auto-detected commitment is a quick errand (short TTL)
_MICRO_HINTS = {
    "eat", "drink", "shower", "walk", "meds", "medication", "pill", "call",
    "text", "email", "reply", "wash", "dishes", "trash", "laundry", "water",
}
_COMMITMENT_TTL_MICRO = timedelta(minutes=90)
_COMMITMENT_TTL = timedelta(hours=36)

# Direct commands to the pony are requests, not promises by the user.  The
# LLM sensor is instructed about this too, but this small deterministic guard
# prevents structured planner commands from becoming duplicate one-time tasks.
_ASSISTANT_COMMAND_PREFIXES = (
    "add ", "create ", "delete ", "edit ", "list ", "make ", "remind ",
    "restore ", "schedule ", "set up ", "show ", "snooze ", "track ",
)


def _is_assistant_command(text: str) -> bool:
    normalized = " ".join(text.casefold().strip().split())
    return normalized.startswith(_ASSISTANT_COMMAND_PREFIXES)


# Proactive messages — reminder nudges, awareness observations — are the pony
# speaking into the one shared conversation, so they land in the chat lane's
# history as ordinary assistant turns. Unbounded, they take the window over: an
# awareness nudge every cooldown fills it within hours, and the fast model then
# continues the pattern it sees most instead of answering — replying to "what
# sport have I been doing?" with another screen observation. Keep only the last
# few so "done!" still answers the nudge the pony actually sent, and drop the
# rest. Measured on the local fast lane against a 40-message history that was
# 21 nudges: parroted 11/15 unfiltered, 0/8 at this cap, 3/8 at three.
PROACTIVE_SOURCES = frozenset({"reminder"})
PROACTIVE_HISTORY_LIMIT = 2

# The same text reaches the chat lane a second way: the awareness lane writes
# an audit row for every screen assessment — dozens an hour, each one a
# third-person observation ("The user is browsing Reddit, which falls under
# …") — so recent_activity hands the model a wall of them and the real rows it
# was asked about are nowhere in the answer. That log is the Activity panel's
# bookkeeping, and the panel still shows every row.
AWARENESS_AUDIT_ACTIONS = frozenset({
    "screen_assessed", "screen_assessment_failed", "awareness_intervention",
})


def chat_history(
    messages: list[dict], keep_proactive: int = PROACTIVE_HISTORY_LIMIT
) -> list[dict]:
    """Model-facing history: every real exchange, only the last few nudges.

    Takes `recent_messages(..., with_source=True)` rows and returns plain
    role/content dicts — the API rejects anything else.
    """
    proactive = [
        i for i, m in enumerate(messages) if m.get("source") in PROACTIVE_SOURCES
    ]
    dropped = set(proactive[: max(0, len(proactive) - keep_proactive)])
    return [
        {"role": m["role"], "content": m["content"]}
        for i, m in enumerate(messages)
        if i not in dropped
    ]

# ── sensor schemas & prompts (small fast calls, tiny context) ─────────
_SENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "done_task_ids": {
            "type": "array", "items": {"type": "integer"},
            "description": "ids from the pending list the user clearly reports as finished",
        },
        "maybe_done_task_ids": {
            "type": "array", "items": {"type": "integer"},
            "description": "ids that MIGHT be what the user finished, but it is unclear",
        },
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "the user's own future action, short imperative"},
                    "when": {"type": "string",
                             "description": "time phrase if stated, else empty"},
                },
                "required": ["text"],
            },
        },
    },
    "required": ["done_task_ids", "maybe_done_task_ids", "commitments"],
}

_SENSE_PROMPT = """\
You read ONE chat message from the user (any language) against their pending
task list and report, strictly as JSON:
- done_task_ids: tasks the message clearly reports as already finished.
  Negations ("didn't do it yet"), intentions and questions are NOT done.
- maybe_done_task_ids: plausibly finished but ambiguous — never guess into
  done_task_ids.
- commitments: the user's OWN new future actions stated in passing ("I'll
  call mom later", "muszę wysłać maila"). Exclude: negations, questions,
  hypotheticals, past events, other people's actions, actions already in the
  pending list, and EVERY command/request TO the assistant. Examples that MUST
  return no commitment: "remind me to X", "set up a daily X routine", "create
  a goal", "add a rule", "schedule X", "track X", "list my tasks". Those are
  assistant jobs handled by tools, not promises by the user. Keep each under
  8 words, empty list if none.
"""

_WHEN_SCHEMA = {
    "type": "object",
    "properties": {
        "datetime": {
            "type": "string",
            "description": "resolved local time as YYYY-MM-DD HH:MM, or \"\" if the "
                           "phrase does not describe a time",
        }
    },
    "required": ["datetime"],
}

_WHEN_PROMPT = """\
You convert one human time phrase (any language: "in 2h", "jutro wieczorem",
"friday 9am") into an absolute local datetime, given the current time.
Conventions: morning=09:00, afternoon=15:00, evening=20:00, night=22:00;
bare weekday/date=09:00. Answer strictly as JSON.
"""


def _ref_schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {
        "ref": {
            "type": "string",
            "description": "task reference: the [#id] from a listing, or enough of the title",
        }
    }
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": ["ref"]}


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="add_task",
        description=(
            "Track a task or reminder for the user. Only for future actions THE USER "
            "intends to take — never for things you should do, or events that already "
            "happened. Pass times as the user said them ('in 2h', 'tomorrow at 10')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "short imperative title"},
                "when": {"type": "string", "description": "deadline/reminder time phrase, optional"},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                "notes": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    ToolSpec(
        name="list_tasks",
        description="Current task overview, rendered verbatim from the database. "
                    "Show it to the user as-is; never summarize items away.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(name="complete_task", description="Mark a task done.", input_schema=_ref_schema()),
    ToolSpec(
        name="snooze_task",
        description="Move a task's reminder to a new time (resets the nudge trail).",
        input_schema=_ref_schema(
            {"until": {"type": "string", "description": "time phrase, e.g. 'tomorrow at 9'"}}
        ),
    ),
    ToolSpec(name="cancel_task", description="Cancel a task the user no longer wants.",
             input_schema=_ref_schema()),
    ToolSpec(name="restore_task", description="Revive a dropped or cancelled task.",
             input_schema=_ref_schema()),
    ToolSpec(
        name="look_at_screen",
        description=(
            "Take a screenshot and describe what the user is looking at. Only works "
            "when the user enabled screen peeking in settings; never pretend you saw "
            "the screen if this tool returns an error."
        ),
        input_schema={
            "type": "object",
            "properties": {"question": {
                "type": "string",
                "description": "what to look for / answer about the screen",
            }},
        },
    ),
    ToolSpec(
        name="deep_think",
        description=(
            "Escalate a genuinely hard question (planning, analysis, anything "
            "needing real reasoning) to a stronger, slower model. Relay the answer "
            "in your own voice afterwards."
        ),
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    ),
    ToolSpec(
        name="recent_logs",
        description=(
            "Read the recent tail of configured local log files and answer a "
            "question about them. Only works when log watching is enabled in "
            "settings; returns an error if disabled. Use this when the user asks "
            "what happened in a service log, whether something errored, or to "
            "summarize recent log activity."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "what to find or summarize in the logs",
                },
            },
            "required": ["question"],
        },
    ),
    # ── Routine tools ──────────────────────────────────────────
    ToolSpec(
        name="add_routine",
        description=(
            "Create a recurring routine (daily habit, weekly meeting, monthly task). "
            "Unlike add_task which is one-time, routines repeat on a schedule. "
            "Cadence: 'daily', 'weekdays', or 'monthly'. Weekdays are 0=Mon..6=Sun. "
            "time_of_day is when the reminder fires. deadline_time is the hard cutoff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "short title for the routine"},
                "cadence": {"type": "string", "enum": ["daily", "weekdays", "monthly"],
                            "description": "how often it repeats"},
                "weekdays": {"type": "array", "items": {"type": "integer"},
                             "description": "0=Mon..6=Sun, used with 'weekdays' cadence"},
                "time_of_day": {"type": "string",
                                "description": "reminder time as HH:MM"},
                "day_of_month": {"type": "integer",
                                 "description": "which day for monthly (1-31)"},
                "deadline_time": {"type": "string",
                                  "description": "hard deadline as HH:MM"},
                "notes": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    ToolSpec(
        name="list_routines",
        description="Show all active routines with their schedule, status, and streak.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="edit_routine",
        description="Edit an existing routine's details.",
        input_schema={
            "type": "object",
            "properties": {
                "routine_id": {"type": "integer", "description": "routine id from list_routines"},
                "title": {"type": "string"},
                "cadence": {"type": "string", "enum": ["daily", "weekdays", "monthly"]},
                "weekdays": {"type": "array", "items": {"type": "integer"}},
                "time_of_day": {"type": "string"},
                "day_of_month": {"type": "integer"},
                "deadline_time": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["routine_id"],
        },
    ),
    ToolSpec(
        name="complete_routine",
        description="Mark today's occurrence of a routine as done.",
        input_schema={
            "type": "object",
            "properties": {
                "routine_id": {"type": "integer", "description": "routine id"},
            },
            "required": ["routine_id"],
        },
    ),
    ToolSpec(
        name="skip_routine",
        description="Skip today's occurrence of a routine (counts as skipped for streak).",
        input_schema={
            "type": "object",
            "properties": {
                "routine_id": {"type": "integer", "description": "routine id"},
            },
            "required": ["routine_id"],
        },
    ),
    ToolSpec(
        name="archive_routine",
        description="Archive a routine (hide from active list, keeps history).",
        input_schema={
            "type": "object",
            "properties": {
                "routine_id": {"type": "integer", "description": "routine id"},
            },
            "required": ["routine_id"],
        },
    ),
    # ── Goal tools ─────────────────────────────────────────────
    ToolSpec(
        name="add_goal",
        description=(
            "Create a goal (milestone to track over time). Goals can track a count "
            "of days met, a streak of consecutive days, or both. Optionally link "
            "to routines so progress auto-syncs when those routines are completed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "short title"},
                "description": {"type": "string"},
                "condition": {"type": "string",
                              "description": "goal condition description"},
                "target_count": {"type": "integer",
                                 "description": "total days needed to achieve"},
                "target_streak": {"type": "integer",
                                  "description": "consecutive days needed"},
                "linked_routine_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "routine ids to auto-sync from",
                },
            },
            "required": ["title"],
        },
    ),
    ToolSpec(
        name="list_goals",
        description="Show all goals with progress (count, streak, status).",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="check_in_goal",
        description="Manually record progress for a goal (met or not met for today).",
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer", "description": "goal id"},
                "met": {"type": "boolean", "description": "did you meet the goal today?"},
                "note": {"type": "string", "description": "optional note"},
            },
            "required": ["goal_id", "met"],
        },
    ),
    ToolSpec(
        name="link_routine_to_goal",
        description="Link a routine to a goal so progress auto-syncs.",
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
                "routine_id": {"type": "integer"},
            },
            "required": ["goal_id", "routine_id"],
        },
    ),
    ToolSpec(
        name="achieve_goal",
        description="Manually mark a goal as achieved.",
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
            },
            "required": ["goal_id"],
        },
    ),
    ToolSpec(
        name="reopen_goal",
        description="Reopen an achieved goal so it can be retracked.",
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
            },
            "required": ["goal_id"],
        },
    ),
    # ── Rule tools ─────────────────────────────────────────────
    ToolSpec(
        name="add_rule",
        description=(
            "Create an accountability rule (automatic nudge based on time or custom condition). "
            "Time rules use conditions like 'after 22:00' or 'between 09:00 and 17:00'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "short title"},
                "rule_type": {"type": "string", "enum": ["time", "screen", "custom"],
                              "description": "rule category"},
                "condition": {"type": "string",
                              "description": "e.g. 'after 22:00', 'before 08:30', 'between 09:00 and 17:00'"},
                "message": {"type": "string", "description": "what to say when triggered"},
                "cooldown_minutes": {"type": "integer",
                                     "description": "minutes before it can fire again"},
            },
            "required": ["title", "rule_type", "condition"],
        },
    ),
    ToolSpec(
        name="list_rules",
        description="Show all accountability rules with status and cooldown.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="edit_rule",
        description="Edit an existing accountability rule.",
        input_schema={
            "type": "object",
            "properties": {
                "rule_id": {"type": "integer", "description": "rule id"},
                "title": {"type": "string"},
                "condition": {"type": "string"},
                "message": {"type": "string"},
                "cooldown_minutes": {"type": "integer"},
            },
            "required": ["rule_id"],
        },
    ),
    ToolSpec(
        name="toggle_rule",
        description="Enable or disable a rule.",
        input_schema={
            "type": "object",
            "properties": {
                "rule_id": {"type": "integer"},
            },
            "required": ["rule_id"],
        },
    ),
    ToolSpec(
        name="delete_rule",
        description="Permanently delete a rule.",
        input_schema={
            "type": "object",
            "properties": {
                "rule_id": {"type": "integer"},
            },
            "required": ["rule_id"],
        },
    ),
    # ── Activity & token tools ─────────────────────────────────
    ToolSpec(
        name="recent_activity",
        description="Show recent activity log (routine completions, goal check-ins, rule fires, etc.).",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "how many entries (default 20)"},
            },
        },
    ),
    ToolSpec(
        name="token_usage",
        description="Show token usage summary grouped by lane (chat, sensor, slow, vision).",
        input_schema={
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "7d", "all"],
                           "description": "time window (default 'all')"},
            },
        },
    ),
    # ── Onboarding tools ───────────────────────────────────────
    ToolSpec(
        name="onboarding_status",
        description="Check the current onboarding status (new, in_progress, completed, skipped).",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="complete_onboarding",
        description="Mark first-run onboarding as complete. Call this when the user says setup is done or you have collected enough initial information.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="skip_onboarding",
        description="Skip first-run onboarding. Call this if the user explicitly says they want to skip setup.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="restart_onboarding",
        description="Restart first-run onboarding from the beginning. Use only if the user explicitly asks to redo setup.",
        input_schema={"type": "object", "properties": {}},
    ),
    # ── Proactive question tools ───────────────────────────────
    ToolSpec(
        name="silence_proactive_questions",
        description="Silence proactive context questions for a number of hours. Use when the user says 'don't bother me' or similar.",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {"type": "integer",
                           "description": "hours to stay quiet (default 24)"},
            },
        },
    ),
    ToolSpec(
        name="resume_proactive_questions",
        description="Resume proactive context questions after they were silenced.",
        input_schema={"type": "object", "properties": {}},
    ),
    # ── External service tools ─────────────────────────────────
    ToolSpec(
        name="mcp_status",
        description=(
            "List the external MCP services currently connected, their tools, "
            "and any connection errors."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    # ── Agent Skills tools ─────────────────────────────────────
    ToolSpec(
        name="activate_skill",
        description=(
            "Load the full instructions of an available skill. Use when the "
            "user's request matches a skill's description."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "available skill name"},
            },
            "required": ["name"],
        },
    ),
    ToolSpec(
        name="read_skill_file",
        description=(
            "Read a reference file bundled with an activated skill "
            "(relative path from the skill directory)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "activated skill name"},
                "path": {"type": "string", "description": "relative bundled file path"},
            },
            "required": ["skill", "path"],
        },
    ),
]


def _tool_definitions() -> list[ToolDefinition]:
    """Header-only mirrors of TOOL_SPECS so the rendered prompt names them."""
    return [
        ToolDefinition(header=ToolDefinitionHeader(
            name=ts.name,
            description=ts.description,
            usage_explanation_short=ts.name.replace("_", " "),
            usage_explanation_long=ts.description,
            rules=[],
        ))
        for ts in TOOL_SPECS
    ]


class PonyBrain:
    """One brain, many faces. Owns specs, history, tools, sensors and guards."""

    def __init__(
        self,
        config: Config,
        store: TaskStore,
        screenshot_fn: Callable[[], bytes | None] | None = None,
        log_fn: Callable[[], str] | None = None,
        client_factory: Callable[[Any], Any] | None = None,  # tests inject fakes
        token_callback: TokenCallback | None = None,
        # Optional injected accountability stores / engines (app.Core wires these).
        # When None, PonyBrain creates its own from the TaskStore (headless/CLI).
        accountability_stores: dict[str, Any] | None = None,
        routine_engine: RoutineEngine | None = None,
        goal_engine: GoalEngine | None = None,
        rule_engine: RuleEngine | None = None,
        activity_store: ActivityStore | None = None,
        mcp_manager: Any | None = None,
        skills_library: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.screenshot_fn = screenshot_fn
        self.log_fn = log_fn
        self.client_factory = client_factory
        self.token_callback = token_callback
        self.character_slug = config.ui.character
        self.provider_name = config.llm.active
        self._specs: dict[tuple, Any] = {}
        self._turn_lock = asyncio.Lock()

        # Accountability stores — injected or self-created
        self._acct_stores = accountability_stores
        self._routine_engine = routine_engine
        self._goal_engine = goal_engine
        self._rule_engine = rule_engine
        self._activity_store = activity_store
        self._mcp = mcp_manager
        self._skills = skills_library

    # ── lazy store access ────────────────────────────────────────────
    @property
    def _stores(self) -> dict[str, Any]:
        if self._acct_stores is None:
            self._acct_stores = get_stores(self.store)
        return self._acct_stores

    @property
    def _activity(self) -> ActivityStore | None:
        if self._activity_store is not None:
            return self._activity_store
        return self._stores.get("activity")

    @property
    def _routine_engine(self) -> RoutineEngine | None:
        return getattr(self, "_routine_engine_val", None)

    @_routine_engine.setter
    def _routine_engine(self, val: RoutineEngine | None) -> None:
        self._routine_engine_val = val

    @property
    def _goal_engine(self) -> GoalEngine | None:
        return getattr(self, "_goal_engine_val", None)

    @_goal_engine.setter
    def _goal_engine(self, val: GoalEngine | None) -> None:
        self._goal_engine_val = val

    @property
    def _rule_engine(self) -> RuleEngine | None:
        return getattr(self, "_rule_engine_val", None)

    @_rule_engine.setter
    def _rule_engine(self, val: RuleEngine | None) -> None:
        self._rule_engine_val = val

    # ── switching ────────────────────────────────────────────────────
    def set_character(self, slug: str) -> None:
        self.character_slug = slug
        self._specs.clear()

    def set_provider(self, name: str) -> None:
        if name not in self.config.llm.providers:
            raise KeyError(f"unknown provider {name!r}")
        self.provider_name = name
        self._specs.clear()

    def provider_names(self) -> list[str]:
        return sorted(self.config.llm.providers)

    # ── specs ────────────────────────────────────────────────────────
    def _spec(self, kind: str):
        key = (kind, self.provider_name, self.character_slug)
        if key not in self._specs:
            provider_cfg = self.config.llm.providers[self.provider_name]
            character = get_character(self.character_slug)
            if kind == FAST:
                agent = AgentDefinition(
                    header=AgentHeader(
                        agent_id="pony", name=character.name,
                        description="desktop pony assistant, fast chat front-end",
                    ),
                    usage_explanation_short="pony chat",
                    usage_explanation_long="Chats with the user and manages their tasks.",
                    system_prompt=build_system_prompt(character),
                    extra_tools=_tool_definitions(),
                )
                spec = build_interactive_spec(
                    agent=agent,
                    live_profile=make_live_profile(self.provider_name, provider_cfg, FAST),
                )
                # swap in hand-authored tool schemas (header-only tools derive none)
                spec = spec.model_copy(update={"tools": tuple(TOOL_SPECS)})
            else:
                prompts = {
                    SLOW: "You are a careful, thorough analyst. Answer completely "
                          "and concretely; structure long answers.",
                    VISION: "You describe screenshots accurately and concisely. "
                            "Focus on what the user asked about; transcribe "
                            "relevant text exactly.",
                }
                agent = AgentDefinition(
                    header=AgentHeader(
                        agent_id=f"pony-{kind}", name=f"pony-{kind}",
                        description=f"{kind} lane",
                    ),
                    usage_explanation_short=kind,
                    usage_explanation_long=f"{kind} lane model",
                    system_prompt=prompts[kind],
                )
                spec = build_interactive_spec(
                    agent=agent,
                    live_profile=make_live_profile(self.provider_name, provider_cfg, kind),
                )
            self._specs[key] = spec
        spec = self._specs[key]
        if kind == FAST and self._mcp is not None:
            spec = spec.model_copy(update={
                "tools": tuple(TOOL_SPECS) + tuple(self._mcp_tool_specs()),
            })
        return spec

    def _mcp_tool_specs(self) -> list[ToolSpec]:
        """Return the current MCP tool snapshot in the runner's native format."""
        if self._mcp is None:
            return []
        return [
            ToolSpec(
                name=info.namespaced_name,
                description=f"[{info.server}] {info.description}".rstrip(),
                input_schema=info.input_schema,
            )
            for info in self._mcp.tools()
        ]

    def _mcp_context_note(self) -> str:
        """Build a compact per-turn summary of connected external services."""
        if self._mcp is None:
            return ""

        instructions = self._mcp.instructions()
        servers = set(instructions)
        servers.update(info.server for info in self._mcp.tools())

        status_fn = getattr(self._mcp, "status", None)
        if callable(status_fn):
            for name, state in status_fn().items():
                status = getattr(state.status, "value", state.status)
                if status == "connected":
                    servers.add(name)

        if not servers:
            return ""
        lines = [
            "## Connected external services",
            "You have extra tools from user-configured services (names start with mcp__).",
        ]
        lines.extend(
            f"[{server}] {instructions.get(server) or '(no description provided)'}"
            for server in sorted(servers)
        )
        return "\n".join(lines)

    def _sensor_spec(self, system_prompt: str, agent_id: str):
        """A fast-model spec with a minimal prompt — the 'small fast call'."""
        return self._spec(FAST).model_copy(update={
            "system_prompt": system_prompt, "agent_id": agent_id, "tools": ()},
        )

    def _run(self, spec, user_input, *, tool_runner=None, history=None,
             output_schema: dict | None = None, max_tool_rounds: int | None = None):
        if output_schema is not None:
            spec = spec.model_copy(update={"output_schema": output_schema})
        kwargs: dict[str, Any] = {
            "tool_runner": tool_runner,
            "history": history,
            "max_tool_rounds": max_tool_rounds or self.config.llm.max_tool_rounds,
        }
        if self.client_factory is not None:
            raw_client = self.client_factory(spec)
        else:
            raw_client = RawResponseOpenAICompatClient.from_spec(spec)
        # Wrap with token capture if a callback is registered
        if self.token_callback is not None:
            raw_client = TokenCaptureClient(
                raw_client,
                callback=self.token_callback,
                lane=lane_from_agent_id(spec.agent_id),
                purpose=purpose_from_agent_id(spec.agent_id),
                provider=self.provider_name,
                model=spec.model_id,
            )
        kwargs["client"] = raw_client
        return run_interactive(spec, user_input, **kwargs)

    # ── small fast call: time grounding ──────────────────────────────
    def parse_when(self, phrase: str, now: datetime | None = None) -> datetime | None:
        """Ground a human time phrase (any language) into a datetime via a
        tiny fast-model call; falls back to the offline parser if the call
        fails (no network, provider down)."""
        phrase = phrase.strip()
        if not phrase:
            return None
        now = now or datetime.now()
        try:
            result = self._run(
                self._sensor_spec(_WHEN_PROMPT, "when-sensor"),
                f"Current time: {now:%Y-%m-%d %H:%M} ({now:%A})\nPhrase: {phrase}",
                output_schema=_WHEN_SCHEMA,
            )
            raw = (result.structured or {}).get("datetime", "").strip()
            if not raw:
                return None
            return datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except Exception:
            log.warning("when-sensor failed for %r, using offline parser", phrase)
            return parse_when_offline(phrase, now)

    # ── the main turn ────────────────────────────────────────────────
    async def respond(self, text: str, source: str = "desktop") -> str:
        async with self._turn_lock:
            return await asyncio.to_thread(self._respond_sync, text, source)

    def _respond_sync(self, text: str, source: str) -> str:
        self.store.save_message("user", text, source)
        guard_notes = self._sense_and_ground(text)
        user_turn = text
        if guard_notes:
            user_turn = (
                f"{text}\n\n[system note — real database state, trust this over "
                f"your own assumptions:\n" + "\n".join(guard_notes) + "]"
            )
        history = chat_history(
            self.store.recent_messages(
                self.config.llm.history_limit, with_source=True
            )[:-1]
        )
        # Inject onboarding context note into system prompt when active
        spec = self._spec(FAST)
        onboarding_note = self._onboarding_context_note()
        if onboarding_note:
            spec = spec.model_copy(update={
                "system_prompt": spec.system_prompt + "\n\n" + onboarding_note,
            })
        mcp_note = self._mcp_context_note()
        if mcp_note:
            spec = spec.model_copy(update={
                "system_prompt": spec.system_prompt + "\n\n" + mcp_note,
            })
        if self._skills is not None:
            skills_catalog = self._skills.catalog()
            if skills_catalog:
                spec = spec.model_copy(update={
                    "system_prompt": spec.system_prompt + "\n\n" + skills_catalog,
                })
        result = self._run(
            spec, user_turn,
            tool_runner=self._tool_runner, history=history,
        )
        reply = result.output_text.strip() or "…*ears droop* something went wrong in my head."
        if result.error:
            log.warning("turn ended with error: %s", result.error)
        self.store.save_message("assistant", reply, source)
        return reply

    # ── the per-message sensor (one small fast call) ─────────────────
    def _sense_and_ground(self, text: str) -> list[str]:
        """Run the combined message sensor, then apply ONLY what grounds
        against real rows. Returns notes describing what actually changed,
        which the chat model must treat as ground truth."""
        pending = self.store.pending()
        if not pending and not self.config.auto_track_commitments:
            return []
        listing = "\n".join(f"[{t.id}] {t.title}" for t in pending[:20])
        try:
            result = self._run(
                self._sensor_spec(_SENSE_PROMPT, "message-sensor"),
                f"Pending tasks:\n{listing or '(none)'}\n\nUser message: {text}",
                output_schema=_SENSE_SCHEMA,
            )
            sense = result.structured or {}
        except Exception:
            log.exception("message sensor failed — continuing without guards")
            return []

        notes: list[str] = []
        by_id = {t.id: t for t in pending}
        msg_tokens = content_tokens(text)

        # completions: id must be real + share words with the message (or be
        # the only pending task) — sensor output is never ground truth alone
        maybe: list = []
        for task_id in sense.get("done_task_ids", []):
            task = by_id.get(task_id)
            if task is None:
                continue
            grounded = bool(content_tokens(task.title) & msg_tokens) or len(pending) == 1
            if grounded:
                done = self.store.complete(task, actor="sensor")
                notes.append(f"already marked done in the database: {done.describe()}")
            else:
                maybe.append(task)
        for task_id in sense.get("maybe_done_task_ids", []):
            if (task := by_id.get(task_id)) is not None and task.status == "pending":
                maybe.append(task)
        if maybe and not any(n.startswith("already marked done") for n in notes):
            options = "; ".join(t.describe() for t in maybe[:4])
            notes.append(
                f"the message may mean one of these got finished: {options} — ask "
                f"which, do NOT guess, and do NOT claim anything was completed."
            )

        # commitments: auto-track passing promises, grounded by token overlap.
        # Never turn a direct planner command into a duplicate one-time task.
        if self.config.auto_track_commitments and not _is_assistant_command(text):
            now = datetime.now()
            for item in sense.get("commitments", [])[:3]:
                title = str(item.get("text", "")).strip()
                if not title or not (content_tokens(title) & msg_tokens):
                    continue  # ungrounded sensor invention — skip
                when = None
                if when_raw := str(item.get("when", "") or "").strip():
                    when = self.parse_when(when_raw, now)
                if when is None:
                    micro = bool(content_tokens(title) & _MICRO_HINTS)
                    when = now + (_COMMITMENT_TTL_MICRO if micro else _COMMITMENT_TTL)
                task, created = self.store.add(
                    title, deadline=when, source="commitment", actor="sensor",
                )
                if created:
                    notes.append(
                        f"noticed a promise and started tracking it: {task.describe()} "
                        f"— mention this briefly so they know you'll remind them."
                    )
        return notes

    # ── tools ────────────────────────────────────────────────────────
    def _tool_runner(self, name: str, args: dict) -> str:
        try:
            if name.startswith("mcp__") and self._mcp is not None:
                return self._mcp.call(name, args)
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"ERROR: unknown tool {name}"
            return handler(args)
        except Exception as e:  # tool errors go back to the model, never raise
            log.exception("tool %s failed", name)
            return f"ERROR: {e}"

    def _tool_mcp_status(self, args: dict) -> str:
        if self._mcp is None:
            return "No external services configured."
        manager_config = getattr(self._mcp, "config", None)
        if manager_config is not None and not manager_config.enabled:
            return "No external services configured."

        states = self._mcp.status()
        if not states:
            return "No external services configured."

        tools_by_server: dict[str, list[str]] = {}
        for tool in self._mcp.tools():
            tools_by_server.setdefault(tool.server, []).append(tool.namespaced_name)

        lines = []
        for name, state in sorted(states.items()):
            raw_status = state.status
            status = getattr(raw_status, "value", str(raw_status)).upper()
            tool_names = sorted(tools_by_server.get(name, ()))
            if status == "CONNECTED":
                tool_label = "tool" if len(tool_names) == 1 else "tools"
                details = ", ".join(tool_names) if tool_names else "no tools"
                lines.append(
                    f"{name}: CONNECTED ({len(tool_names)} {tool_label}) — {details}"
                )
            elif state.last_error:
                lines.append(f"{name}: {status} — {state.last_error}")
            else:
                lines.append(f"{name}: {status}")
        return "\n".join(lines)

    def _tool_activate_skill(self, args: dict) -> str:
        if self._skills is None:
            return "ERROR: skills are not configured"
        name = str(args.get("name", "")).strip()
        if not name:
            return "ERROR: skill name is required"
        try:
            return self._skills.load(name)
        except Exception as exc:
            return f"ERROR: {exc}"

    def _tool_read_skill_file(self, args: dict) -> str:
        if self._skills is None:
            return "ERROR: skills are not configured"
        skill = str(args.get("skill", "")).strip()
        path = str(args.get("path", "")).strip()
        if not skill:
            return "ERROR: skill name is required"
        if not path:
            return "ERROR: skill file path is required"
        try:
            return self._skills.read_file(skill, path)
        except Exception as exc:
            return f"ERROR: {exc}"

    # ── one-time task tools (with activity recording) ────────────────

    def _tool_add_task(self, args: dict) -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "ERROR: title is required"
        deadline = None
        if when_raw := str(args.get("when", "")).strip():
            deadline = self.parse_when(when_raw)
            if deadline is None:
                return (f"ERROR: could not understand the time {when_raw!r} — ask the "
                        f"user to clarify when they mean")
        task, created = self.store.add(
            title,
            notes=str(args.get("notes", "")),
            deadline=deadline,
            priority=str(args.get("priority", "medium")),
            actor="pony",
        )
        if created and self._activity is not None:
            self._activity.record(
                "task_added", actor="pony",
                detail=f"Task '{task.title}' added (#{task.id})",
                ref_type="task", ref_id=str(task.id),
            )
        if not created:
            return f"already tracking that one: {task.describe()}"
        return f"added {task.describe()}"

    def _tool_list_tasks(self, args: dict) -> str:
        return self.store.overview()

    def _resolve_or_explain(self, args: dict) -> tuple[Any, str | None]:
        ref = str(args.get("ref", "")).strip()
        if not ref:
            return None, "ERROR: ref is required"
        task, candidates = self.store.resolve(ref)
        if task is None:
            if candidates:
                options = "; ".join(t.describe() for t in candidates[:4])
                return None, f"ambiguous — which one: {options}? ask the user."
            return None, f"no pending task matches {ref!r}"
        return task, None

    def _tool_complete_task(self, args: dict) -> str:
        task, err = self._resolve_or_explain(args)
        if err:
            return err
        done = self.store.complete(task, actor="pony")
        if self._activity is not None:
            self._activity.record(
                "task_completed", actor="pony",
                detail=f"Task '{task.title}' completed",
                ref_type="task", ref_id=str(task.id),
            )
        return f"done ✅ {done.describe()}"

    def _tool_cancel_task(self, args: dict) -> str:
        task, err = self._resolve_or_explain(args)
        if err:
            return err
        cancelled = self.store.cancel(task, actor="pony")
        if self._activity is not None:
            self._activity.record(
                "task_cancelled", actor="pony",
                detail=f"Task '{task.title}' cancelled",
                ref_type="task", ref_id=str(task.id),
            )
        return f"cancelled: {cancelled.describe()}"

    def _tool_snooze_task(self, args: dict) -> str:
        task, err = self._resolve_or_explain(args)
        if err:
            return err
        until = self.parse_when(str(args.get("until", "")))
        if until is None:
            return "ERROR: could not understand the time — ask the user when to come back"
        snoozed = self.store.snooze(task, until)
        if self._activity is not None:
            self._activity.record(
                "task_snoozed", actor="pony",
                detail=f"Task '{task.title}' snoozed to {until:%Y-%m-%d %H:%M}",
                ref_type="task", ref_id=str(task.id),
            )
        return f"snoozed until {until:%a %d %b %H:%M}: {snoozed.describe()}"

    def _tool_restore_task(self, args: dict) -> str:
        ref = str(args.get("ref", "")).strip()
        task = self.store.restore(ref)
        if task is not None and self._activity is not None:
            self._activity.record(
                "task_restored", actor="pony",
                detail=f"Task '{task.title}' restored",
                ref_type="task", ref_id=str(task.id),
            )
        return f"restored: {task.describe()}" if task else f"no dropped task matches {ref!r}"

    # ── routine tools ────────────────────────────────────────────────

    def _tool_add_routine(self, args: dict) -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "ERROR: title is required"
        routine = self._stores["routines"].add(
            title,
            notes=str(args.get("notes", "")),
            cadence=str(args.get("cadence", "daily")),
            weekdays=args.get("weekdays") or [],
            time_of_day=str(args.get("time_of_day", "")) or None,
            day_of_month=args.get("day_of_month"),
            deadline_time=str(args.get("deadline_time", "")) or None,
        )
        if self._activity is not None:
            self._activity.record(
                "routine_added", actor="pony",
                detail=f"Routine '{routine.title}' added (#{routine.id})",
                ref_type="routine", ref_id=str(routine.id),
            )
        return f"added routine #{routine.id}: {routine.title} ({routine.cadence})"

    def _tool_list_routines(self, args: dict) -> str:
        routines = self._stores["routines"].list_all()
        if not routines:
            return "No routines set up yet."
        lines = ["Active routines:"]
        today = date.today()
        comp_store = self._stores["routine_completions"]
        for r in routines:
            comps = comp_store.by_routine(r.id)
            streak = current_streak(r, comps, today)
            status = "enabled" if r.enabled else "DISABLED"
            sched = ""
            if r.cadence == "daily":
                sched = "daily"
            elif r.cadence == "weekdays":
                wd = r.weekdays or list(range(5))
                day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                sched = ", ".join(day_names[d] for d in wd)
            elif r.cadence == "monthly":
                sched = f"day {r.day_of_month or 1}"
            time_info = ""
            if r.time_of_day:
                time_info = f" @ {r.time_of_day}"
            if r.deadline_time:
                time_info += f" (deadline {r.deadline_time})"
            lines.append(
                f"  • [#{r.id}] {r.title} — {r.cadence} ({sched}){time_info} "
                f"[{status}] streak={streak}"
            )
        return "\n".join(lines)

    def _tool_edit_routine(self, args: dict) -> str:
        rid = args.get("routine_id")
        if rid is None:
            return "ERROR: routine_id is required"
        try:
            updated = self._stores["routines"].update(
                rid,
                title=str(args.get("title", "")).strip() or None,
                cadence=str(args.get("cadence", "")).strip() or None,
                weekdays=args.get("weekdays"),
                time_of_day=str(args.get("time_of_day", "")).strip() or None,
                day_of_month=args.get("day_of_month"),
                deadline_time=str(args.get("deadline_time", "")).strip() or None,
                notes=str(args.get("notes", "")) or None,
            )
        except KeyError:
            return f"no routine #{rid}"
        if self._activity is not None:
            self._activity.record(
                "routine_edited", actor="pony",
                detail=f"Routine '{updated.title}' edited",
                ref_type="routine", ref_id=str(updated.id),
            )
        return f"updated routine #{updated.id}: {updated.title}"

    def _tool_complete_routine(self, args: dict) -> str:
        rid = args.get("routine_id")
        if rid is None:
            return "ERROR: routine_id is required"
        try:
            self._stores["routines"].get(rid)
        except KeyError:
            return f"no routine #{rid}"
        engine = self._routine_engine
        if engine is None:
            return f"ERROR: routine engine not available (routine #{rid})"
        event = engine.complete_today(rid, datetime.now())
        return event.message

    def _tool_skip_routine(self, args: dict) -> str:
        rid = args.get("routine_id")
        if rid is None:
            return "ERROR: routine_id is required"
        try:
            self._stores["routines"].get(rid)
        except KeyError:
            return f"no routine #{rid}"
        engine = self._routine_engine
        if engine is None:
            return f"ERROR: routine engine not available (routine #{rid})"
        event = engine.skip_today(rid, datetime.now())
        return event.message

    def _tool_archive_routine(self, args: dict) -> str:
        rid = args.get("routine_id")
        if rid is None:
            return "ERROR: routine_id is required"
        try:
            self._stores["routines"].get(rid)
        except KeyError:
            return f"no routine #{rid}"
        archived = self._stores["routines"].archive(rid)
        if self._activity is not None:
            self._activity.record(
                "routine_archived", actor="pony",
                detail=f"Routine '{archived.title}' archived",
                ref_type="routine", ref_id=str(archived.id),
            )
        return f"archived routine #{archived.id}: {archived.title}"

    # ── goal tools ───────────────────────────────────────────────────

    def _tool_add_goal(self, args: dict) -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "ERROR: title is required"
        goal = self._stores["goals"].add(
            title,
            description=str(args.get("description", "")),
            condition=str(args.get("condition", "")),
            target_count=args.get("target_count"),
            target_streak=args.get("target_streak"),
            linked_routine_ids=args.get("linked_routine_ids") or [],
        )
        if self._activity is not None:
            self._activity.record(
                "goal_added", actor="pony",
                detail=f"Goal '{goal.title}' added (#{goal.id})",
                ref_type="goal", ref_id=str(goal.id),
            )
        parts = [f"added goal #{goal.id}: {goal.title}"]
        if goal.target_count is not None:
            parts.append(f"target={goal.target_count} days")
        if goal.target_streak is not None:
            parts.append(f"streak={goal.target_streak}")
        return " — ".join(parts)

    def _tool_list_goals(self, args: dict) -> str:
        engine = self._goal_engine
        if engine is None:
            goals = self._stores["goals"].list_all()
            if not goals:
                return "No goals set up yet."
            lines = ["Goals:"]
            for g in goals:
                lines.append(f"  • [#{g.id}] {g.title} ({g.status})")
            return "\n".join(lines)
        summaries = engine.summaries()
        if not summaries:
            return "No goals set up yet."
        lines = ["Goals:"]
        for s in summaries:
            targets = []
            if s.target_count is not None:
                targets.append(f"count={s.count}/{s.target_count}")
            if s.target_streak is not None:
                targets.append(f"streak={s.current_streak}/{s.target_streak}")
            target_str = ", ".join(targets) if targets else "no target"
            lines.append(
                f"  • [#{s.goal_id}] {s.title} — {s.status} "
                f"[{target_str}] longest={s.longest_streak}"
            )
        return "\n".join(lines)

    def _tool_check_in_goal(self, args: dict) -> str:
        gid = args.get("goal_id")
        met = args.get("met")
        if gid is None or met is None:
            return "ERROR: goal_id and met are required"
        try:
            self._stores["goals"].get(gid)
        except KeyError:
            return f"no goal #{gid}"
        engine = self._goal_engine
        if engine is None:
            return "ERROR: goal engine not available"
        entry = engine.check_in(gid, date.today(), met=bool(met),
                                note=str(args.get("note", "")))
        status = "met" if entry.met else "not met"
        return f"goal check-in recorded: {status}"

    def _tool_link_routine_to_goal(self, args: dict) -> str:
        gid = args.get("goal_id")
        rid = args.get("routine_id")
        if gid is None or rid is None:
            return "ERROR: goal_id and routine_id are required"
        try:
            self._stores["goals"].get(gid)
        except KeyError:
            return f"no goal #{gid}"
        try:
            self._stores["routines"].get(rid)
        except KeyError:
            return f"no routine #{rid}"
        engine = self._goal_engine
        if engine is None:
            return "ERROR: goal engine not available"
        updated = engine.link_routine(gid, rid)
        return f"linked routine #{rid} to goal #{gid}: {updated.title}"

    def _tool_achieve_goal(self, args: dict) -> str:
        gid = args.get("goal_id")
        if gid is None:
            return "ERROR: goal_id is required"
        try:
            self._stores["goals"].get(gid)
        except KeyError:
            return f"no goal #{gid}"
        engine = self._goal_engine
        if engine is None:
            return "ERROR: goal engine not available"
        achieved = engine.mark_achieved(gid)
        return f"goal #{achieved.id} '{achieved.title}' marked achieved!"

    def _tool_reopen_goal(self, args: dict) -> str:
        gid = args.get("goal_id")
        if gid is None:
            return "ERROR: goal_id is required"
        try:
            self._stores["goals"].get(gid)
        except KeyError:
            return f"no goal #{gid}"
        engine = self._goal_engine
        if engine is None:
            return "ERROR: goal engine not available"
        reopened = engine.reopen(gid)
        return f"goal #{reopened.id} '{reopened.title}' reopened"

    # ── rule tools ───────────────────────────────────────────────────

    def _tool_add_rule(self, args: dict) -> str:
        title = str(args.get("title", "")).strip()
        rule_type = str(args.get("rule_type", "custom")).strip()
        condition = str(args.get("condition", "")).strip()
        if not title:
            return "ERROR: title is required"
        if not rule_type:
            return "ERROR: rule_type is required"
        if not condition:
            return "ERROR: condition is required"
        rule = self._stores["rules"].add(
            title,
            rule_type=rule_type,
            condition=condition,
            message=str(args.get("message", "")),
            cooldown_minutes=int(args.get("cooldown_minutes", 0)),
        )
        if self._activity is not None:
            self._activity.record(
                "rule_added", actor="pony",
                detail=f"Rule '{rule.title}' added (#{rule.id})",
                ref_type="accountability_rule", ref_id=str(rule.id),
            )
        return f"added rule #{rule.id}: {rule.title} ({rule_type})"

    def _tool_list_rules(self, args: dict) -> str:
        rules = self._stores["rules"].list_all()
        if not rules:
            return "No accountability rules set up yet."
        lines = ["Accountability rules:"]
        for r in rules:
            status = "enabled" if r.enabled else "DISABLED"
            cooldown = f" cooldown={r.cooldown_minutes}m" if r.cooldown_minutes else ""
            lines.append(
                f"  • [#{r.id}] {r.title} — {r.rule_type}: {r.condition} "
                f"[{status}]{cooldown}"
            )
        return "\n".join(lines)

    def _tool_edit_rule(self, args: dict) -> str:
        rid = args.get("rule_id")
        if rid is None:
            return "ERROR: rule_id is required"
        try:
            updated = self._stores["rules"].update(
                rid,
                title=str(args.get("title", "")).strip() or None,
                condition=str(args.get("condition", "")).strip() or None,
                message=str(args.get("message", "")) or None,
                cooldown_minutes=int(args["cooldown_minutes"]) if "cooldown_minutes" in args else None,
            )
        except KeyError:
            return f"no rule #{rid}"
        if self._activity is not None:
            self._activity.record(
                "rule_edited", actor="pony",
                detail=f"Rule '{updated.title}' edited",
                ref_type="accountability_rule", ref_id=str(updated.id),
            )
        return f"updated rule #{updated.id}: {updated.title}"

    def _tool_toggle_rule(self, args: dict) -> str:
        rid = args.get("rule_id")
        if rid is None:
            return "ERROR: rule_id is required"
        try:
            toggled = self._stores["rules"].toggle(rid)
        except KeyError:
            return f"no rule #{rid}"
        status = "enabled" if toggled.enabled else "disabled"
        if self._activity is not None:
            self._activity.record(
                "rule_toggled", actor="pony",
                detail=f"Rule '{toggled.title}' {status}",
                ref_type="accountability_rule", ref_id=str(toggled.id),
            )
        return f"rule #{toggled.id} '{toggled.title}' {status}"

    def _tool_delete_rule(self, args: dict) -> str:
        rid = args.get("rule_id")
        if rid is None:
            return "ERROR: rule_id is required"
        try:
            rule = self._stores["rules"].get(rid)
        except KeyError:
            return f"no rule #{rid}"
        self._stores["rules"].delete(rid)
        if self._activity is not None:
            self._activity.record(
                "rule_deleted", actor="pony",
                detail=f"Rule '{rule.title}' deleted",
                ref_type="accountability_rule", ref_id=str(rule.id),
            )
        return f"deleted rule #{rule.id}: {rule.title}"

    # ── activity & token tools ───────────────────────────────────────

    def _tool_recent_activity(self, args: dict) -> str:
        activity = self._activity
        if activity is None:
            return "Activity logging not available."
        limit = int(args.get("limit", 20))
        entries = activity.recent(limit, exclude_actions=AWARENESS_AUDIT_ACTIONS)
        if not entries:
            return "No recent activity."
        lines = ["Recent activity:"]
        for e in entries:
            ref = f" ({e.ref_type}#{e.ref_id})" if e.ref_id else ""
            lines.append(f"  • [{e.at:%Y-%m-%d %H:%M}] {e.action}{ref} — {e.detail}")
        return "\n".join(lines)

    def _tool_token_usage(self, args: dict) -> str:
        period = str(args.get("period", "all")).strip()
        store = self._stores["token_usage"]
        summary = store.summary(period)
        if not summary:
            return f"No token usage data for {period}."
        lines = [f"Token usage ({period}):"]
        for row in summary:
            lines.append(
                f"  • {row['lane']}: {row['total_tokens']} tokens "
                f"({row['count']} calls)"
            )
        return "\n".join(lines)

    # ── onboarding tools ─────────────────────────────────────────────

    def _tool_onboarding_status(self, args: dict) -> str:
        mgr = self._onboarding_manager()
        return f"Onboarding status: {mgr.status()}"

    def _tool_complete_onboarding(self, args: dict) -> str:
        mgr = self._onboarding_manager()
        mgr.complete()
        return "Onboarding complete! I'm all set up now."

    def _tool_skip_onboarding(self, args: dict) -> str:
        mgr = self._onboarding_manager()
        mgr.skip()
        return "Onboarding skipped. We can always come back to setup later."

    def _tool_restart_onboarding(self, args: dict) -> str:
        mgr = self._onboarding_manager()
        mgr.reset()
        return "Onboarding reset. You can start fresh now."

    def _tool_silence_proactive_questions(self, args: dict) -> str:
        q = self._proactive_questioner()
        if q is None:
            return "Proactive questions are not configured."
        hours = int(args.get("hours", 24))
        q.silence(hours)
        return f"I'll stay quiet about context questions for {hours} hours."

    def _tool_resume_proactive_questions(self, args: dict) -> str:
        q = self._proactive_questioner()
        if q is None:
            return "Proactive questions are not configured."
        q.resume()
        return "Proactive questions resumed. I'll ask again when relevant."

    def _onboarding_manager(self):
        """Lazy-access onboarding manager."""
        from .onboarding import OnboardingManager
        return OnboardingManager(self.store)

    def _onboarding_context_note(self) -> str | None:
        """Return onboarding grounding note for system prompt, or None."""
        mgr = self._onboarding_manager()
        return mgr.context_note()

    def _proactive_questioner(self):
        """Return the proactive questioner if wired, else None."""
        return getattr(self, "_proactive_questioner_val", None)

    def _set_proactive_questioner(self, q) -> None:
        self._proactive_questioner_val = q

    # ── mark onboarding categories collected ─────────────────────────

    def _mark_onboarding_collected(self, *categories: str) -> None:
        """Call from tool handlers during onboarding to mark categories done."""
        mgr = self._onboarding_manager()
        if mgr.is_in_progress():
            mgr.mark_collected(*categories)

    # ── deep think / screen / logs (unchanged) ───────────────────────

    def _tool_deep_think(self, args: dict) -> str:
        question = str(args.get("question", "")).strip()
        if not question:
            return "ERROR: question is required"
        recent = chat_history(self.store.recent_messages(10, with_source=True))
        context = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
        result = self._run(
            self._spec(SLOW),
            f"Recent conversation for context:\n{context}\n\nQuestion: {question}",
        )
        return result.output_text or f"ERROR: deep think failed ({result.error})"

    def _tool_look_at_screen(self, args: dict) -> str:
        if not self.config.screenshot_enabled:
            return ("ERROR: screen peeking is disabled — the user can turn it on in "
                    "settings (or config.yaml: screenshot_enabled)")
        if self.screenshot_fn is None:
            return "ERROR: no screen available (running headless?)"
        png = self.screenshot_fn()
        if not png:
            return "ERROR: screenshot failed"
        question = str(args.get("question", "")) or "Describe what the user is looking at."
        b64 = base64.b64encode(png).decode()
        result = self._run(
            self._spec(VISION),
            [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
        return result.output_text or f"ERROR: vision call failed ({result.error})"

    def _tool_recent_logs(self, args: dict) -> str:
        """Tail configured log files and delegate the question to the FAST lane."""
        if not self.config.logwatch.enabled:
            return ("ERROR: log watching is disabled — the user can turn it on in "
                    "settings (or config.yaml: logwatch.enabled)")
        question = str(args.get("question", "")) or "Summarize what happened recently."
        log_text = (
            self.log_fn() if self.log_fn is not None else read_recent_logs(self.config.logwatch)
        )
        if not log_text:
            return "No log content available (files may be empty or not yet written)."
        result = self._run(
            self._sensor_spec(
                "You answer questions about log file content accurately and concisely. "
                "Quote relevant lines when helpful. Say 'nothing relevant found' if "
                "the logs do not address the question.",
                "log-analyst",
            ),
            f"Question: {question}\n\nLog content:\n{log_text}",
        )
        return result.output_text or "ERROR: log analysis failed."
