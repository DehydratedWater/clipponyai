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
from datetime import datetime, timedelta
from typing import Any, Callable

from open_agent_compiler import (
    AgentDefinition,
    AgentHeader,
    ToolDefinition,
    ToolDefinitionHeader,
)
from open_agent_compiler.interactive import build_interactive_spec, run_interactive
from open_agent_compiler.interactive.spec import ToolSpec

from .characters import build_system_prompt, get_character
from .config import Config
from .logwatch import read_recent_logs
from .providers import FAST, SLOW, VISION, make_live_profile
from .tasks import TaskStore, content_tokens
from .timeparse import parse_when as parse_when_offline

log = logging.getLogger("clipponyai.brain")

# words hinting an auto-detected commitment is a quick errand (short TTL)
_MICRO_HINTS = {
    "eat", "drink", "shower", "walk", "meds", "medication", "pill", "call",
    "text", "email", "reply", "wash", "dishes", "trash", "laundry", "water",
}
_COMMITMENT_TTL_MICRO = timedelta(minutes=90)
_COMMITMENT_TTL = timedelta(hours=36)

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
  pending list, and requests TO the assistant ("remind me to X" is the
  assistant's job, not a promise). Keep each under 8 words, empty list if none.
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
    ) -> None:
        self.config = config
        self.store = store
        self.screenshot_fn = screenshot_fn
        self.log_fn = log_fn
        self.client_factory = client_factory
        self.character_slug = config.ui.character
        self.provider_name = config.llm.active
        self._specs: dict[tuple, Any] = {}
        self._turn_lock = asyncio.Lock()

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
        return self._specs[key]

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
            kwargs["client"] = self.client_factory(spec)
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
        history = self.store.recent_messages(self.config.llm.history_limit)[:-1]
        result = self._run(
            self._spec(FAST), user_turn,
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

        # commitments: auto-track passing promises, grounded by token overlap
        if self.config.auto_track_commitments:
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
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"ERROR: unknown tool {name}"
            return handler(args)
        except Exception as e:  # tool errors go back to the model, never raise
            log.exception("tool %s failed", name)
            return f"ERROR: {e}"

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
        return f"done ✅ {self.store.complete(task, actor='pony').describe()}"

    def _tool_cancel_task(self, args: dict) -> str:
        task, err = self._resolve_or_explain(args)
        if err:
            return err
        return f"cancelled: {self.store.cancel(task, actor='pony').describe()}"

    def _tool_snooze_task(self, args: dict) -> str:
        task, err = self._resolve_or_explain(args)
        if err:
            return err
        until = self.parse_when(str(args.get("until", "")))
        if until is None:
            return "ERROR: could not understand the time — ask the user when to come back"
        return f"snoozed until {until:%a %d %b %H:%M}: {self.store.snooze(task, until).describe()}"

    def _tool_restore_task(self, args: dict) -> str:
        ref = str(args.get("ref", "")).strip()
        task = self.store.restore(ref)
        return f"restored: {task.describe()}" if task else f"no dropped task matches {ref!r}"

    def _tool_deep_think(self, args: dict) -> str:
        question = str(args.get("question", "")).strip()
        if not question:
            return "ERROR: question is required"
        recent = self.store.recent_messages(10)
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
