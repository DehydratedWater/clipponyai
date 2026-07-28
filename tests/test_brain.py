"""Brain tests with a scripted fake LLM client — no network anywhere.

The fake routes on agent_id: "pony" is the chat model, "message-sensor" and
"when-sensor" are the small fast calls, "pony-slow"/"pony-vision" the other
lanes. Handlers return strings (plain content), dicts (JSON content for
structured output) or ("tool", name, args) tuples (a tool call).
"""

from datetime import datetime, timedelta

from clipponyai.providers import FAST, SLOW, VISION

EMPTY_SENSE = {"done_task_ids": [], "maybe_done_task_ids": [], "commitments": []}


# ── basic turn ────────────────────────────────────────────────────────
async def test_respond_plain_turn(make_brain, store):
    brain = make_brain({"pony": "hi friend! ✨", "message-sensor": EMPTY_SENSE})
    reply = await brain.respond("hello")
    assert reply == "hi friend! ✨"
    history = store.recent_messages(10)
    assert history[-2:] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi friend! ✨"},
    ]


async def test_history_flows_into_next_turn(make_brain, store):
    brain = make_brain({"pony": "ok", "message-sensor": EMPTY_SENSE})
    await brain.respond("first message")
    await brain.respond("second message")
    pony_clients = [c for c in brain._test_clients if c.spec.agent_id == "pony"]
    last_messages = pony_clients[-1].calls[0]["messages"]
    joined = " ".join(m["content"] for m in last_messages if isinstance(m["content"], str))
    assert "first message" in joined and "second message" in joined


# ── proactive messages in the chat history ────────────────────────────
def test_chat_history_keeps_only_the_last_few_nudges():
    from clipponyai.brain import chat_history

    messages = [
        {"role": "assistant", "content": f"nudge {i}", "source": "reminder"} for i in range(5)
    ]
    messages.append({"role": "user", "content": "hello", "source": "desktop"})
    kept = chat_history(messages, keep_proactive=2)
    assert kept == [
        {"role": "assistant", "content": "nudge 3"},
        {"role": "assistant", "content": "nudge 4"},
        {"role": "user", "content": "hello"},
    ]


def test_chat_history_never_drops_real_exchanges():
    from clipponyai.brain import chat_history

    messages = [
        {"role": "user", "content": "a", "source": "desktop"},
        {"role": "assistant", "content": "b", "source": "desktop"},
        {"role": "user", "content": "c", "source": "telegram"},
        {"role": "assistant", "content": "d", "source": "telegram"},
    ]
    assert chat_history(messages, keep_proactive=0) == [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]


async def test_awareness_nudges_do_not_flood_the_chat_lane(make_brain, store):
    """A stream of proactive nudges must not crowd the real conversation out
    of the window — that is what made the fast model answer questions with
    another screen observation instead of a reply."""
    from clipponyai.brain import PROACTIVE_HISTORY_LIMIT

    store.save_message("user", "old question", "desktop")
    store.save_message("assistant", "old answer", "desktop")
    for i in range(8):
        store.save_message("assistant", f"🐴 observation {i}", "reminder")

    brain = make_brain({"pony": "an actual answer", "message-sensor": EMPTY_SENSE})
    assert await brain.respond("what sport have I been doing?") == "an actual answer"

    pony = [c for c in brain._test_clients if c.spec.agent_id == "pony"][-1]
    sent = [m["content"] for m in pony.calls[0]["messages"]]
    assert sum(1 for m in sent if "observation" in m) == PROACTIVE_HISTORY_LIMIT
    assert "🐴 observation 7" in sent  # the most recent nudge is still answerable
    assert "old question" in sent and "old answer" in sent
    assert all("source" not in m for m in pony.calls[0]["messages"])


# ── tool loop ─────────────────────────────────────────────────────────
async def test_add_task_tool_with_llm_time_parsing(make_brain, store):
    brain = make_brain(
        {
            "pony": [
                ("tool", "add_task", {"title": "submit the report", "when": "tomorrow at 10"}),
                "noted! I'll remind you ✨",
            ],
            "message-sensor": EMPTY_SENSE,
            "when-sensor": {"datetime": "2026-07-23 10:00"},
        }
    )
    reply = await brain.respond("remind me to submit the report tomorrow at 10")
    assert "remind" in reply
    tasks = store.pending()
    assert len(tasks) == 1
    assert tasks[0].title == "submit the report"
    assert tasks[0].deadline == datetime(2026, 7, 23, 10, 0)


async def test_unparseable_time_reported_to_model(make_brain, store):
    brain = make_brain(
        {
            "pony": [
                ("tool", "add_task", {"title": "thing", "when": "whenever vibes"}),
                "when exactly?",
            ],
            "message-sensor": EMPTY_SENSE,
            "when-sensor": {"datetime": ""},
        }
    )
    await brain.respond("track thing whenever vibes")
    assert store.pending() == []  # nothing added on unparseable time
    pony = [c for c in brain._test_clients if c.spec.agent_id == "pony"][-1]
    tool_result = [m for m in pony.calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_result and "could not understand the time" in tool_result[0]["content"]


async def test_complete_task_tool_by_ref(make_brain, store):
    task, _ = store.add("water the plants")
    brain = make_brain(
        {
            "pony": [("tool", "complete_task", {"ref": f"#{task.id}"}), "done! 🎉"],
            "message-sensor": EMPTY_SENSE,
        }
    )
    await brain.respond("mark the plants as watered please")
    assert store.get(task.id).status == "done"


async def test_list_tasks_tool_returns_verbatim_overview(make_brain, store):
    store.add("water the plants")
    brain = make_brain(
        {
            "pony": [("tool", "list_tasks", {}), "here you go"],
            "message-sensor": EMPTY_SENSE,
        }
    )
    await brain.respond("what's on my list?")
    pony = [c for c in brain._test_clients if c.spec.agent_id == "pony"][-1]
    tool_result = [m for m in pony.calls[-1]["messages"] if m.get("role") == "tool"]
    assert "water the plants" in tool_result[0]["content"]


async def test_deep_think_routes_to_slow_lane(make_brain, store, config):
    brain = make_brain(
        {
            "pony": [("tool", "deep_think", {"question": "plan my week"}), "the slow lane says…"],
            "message-sensor": EMPTY_SENSE,
            "pony-slow": "a very thorough plan",
        }
    )
    await brain.respond("help me plan my week properly")
    slow_clients = [c for c in brain._test_clients if c.spec.agent_id == "pony-slow"]
    assert slow_clients and slow_clients[0].calls
    # the slow lane uses the provider's slow model
    provider = config.llm.providers[config.llm.active]
    assert slow_clients[0].calls[0]["model"] == provider.resolved_slow_model()


async def test_unknown_tool_reports_error(make_brain):
    brain = make_brain(
        {
            "pony": [("tool", "imaginary_tool", {}), "oops"],
            "message-sensor": EMPTY_SENSE,
        }
    )
    await brain.respond("hello")
    pony = [c for c in brain._test_clients if c.spec.agent_id == "pony"][-1]
    tool_result = [m for m in pony.calls[-1]["messages"] if m.get("role") == "tool"]
    assert "unknown tool" in tool_result[0]["content"]


# ── screen peeking ────────────────────────────────────────────────────
def test_look_at_screen_disabled_by_default(make_brain):
    brain = make_brain({})
    result = brain._tool_look_at_screen({})
    assert "disabled" in result


def test_look_at_screen_headless(make_brain, config):
    brain = make_brain({}, screenshot_enabled=True)
    assert "no screen" in brain._tool_look_at_screen({})


def test_look_at_screen_sends_image_to_vision_lane(make_brain, config):
    brain = make_brain({"pony-vision": "you are looking at a code editor"}, screenshot_enabled=True)
    brain.screenshot_fn = lambda: b"\x89PNG fake bytes"
    result = brain._tool_look_at_screen({"question": "what app is this?"})
    assert result == "you are looking at a code editor"
    vision = [c for c in brain._test_clients if c.spec.agent_id == "pony-vision"][0]
    content = vision.calls[0]["messages"][-1]["content"]
    kinds = {part["type"] for part in content}
    assert kinds == {"text", "image_url"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ── the message sensor: grounded completions & commitments ────────────
async def test_sensor_completion_grounded(make_brain, store):
    task, _ = store.add("call the dentist")
    brain = make_brain(
        {
            "pony": "yay, dentist done! 🎉",
            "message-sensor": {
                "done_task_ids": [task.id],
                "maybe_done_task_ids": [],
                "commitments": [],
            },
        }
    )
    await brain.respond("just got back from the dentist call")
    assert store.get(task.id).status == "done"
    # the chat model was told what really changed
    pony = [c for c in brain._test_clients if c.spec.agent_id == "pony"][-1]
    user_turn = pony.calls[0]["messages"][-1]["content"]
    assert "already marked done in the database" in user_turn


async def test_sensor_completion_ungrounded_id_ignored(make_brain, store):
    store.add("call the dentist")
    store.add("water plants")
    brain = make_brain(
        {
            "pony": "ok",
            # sensor invents an id that shares no words with the message → demoted
            "message-sensor": {
                "done_task_ids": [999],
                "maybe_done_task_ids": [],
                "commitments": [],
            },
        }
    )
    await brain.respond("finished it")
    assert all(t.status == "pending" for t in store.pending())


async def test_sensor_ungrounded_title_becomes_question(make_brain, store):
    dentist, _ = store.add("call the dentist")
    store.add("water plants")
    brain = make_brain(
        {
            "pony": "which one?",
            # message shares no words with the dentist task → must not complete
            "message-sensor": {
                "done_task_ids": [dentist.id],
                "maybe_done_task_ids": [],
                "commitments": [],
            },
        }
    )
    await brain.respond("załatwione!")  # "done!" in Polish, no shared tokens
    assert store.get(dentist.id).status == "pending"
    pony = [c for c in brain._test_clients if c.spec.agent_id == "pony"][-1]
    user_turn = pony.calls[0]["messages"][-1]["content"]
    assert "do NOT guess" in user_turn


async def test_sensor_single_pending_task_completes_without_overlap(make_brain, store):
    only, _ = store.add("call the dentist")
    brain = make_brain(
        {
            "pony": "yay!",
            "message-sensor": {
                "done_task_ids": [only.id],
                "maybe_done_task_ids": [],
                "commitments": [],
            },
        }
    )
    await brain.respond("załatwione!")  # only one candidate → trust the sensor
    assert store.get(only.id).status == "done"


async def test_sensor_captures_commitment(make_brain, store):
    brain = make_brain(
        {
            "pony": "I'll hold you to that! ✨",
            "message-sensor": {
                "done_task_ids": [],
                "maybe_done_task_ids": [],
                "commitments": [{"text": "call mom", "when": "tonight"}],
            },
            "when-sensor": {"datetime": "2026-07-22 20:00"},
        }
    )
    await brain.respond("busy day… I'll call mom tonight")
    tasks = store.pending()
    assert len(tasks) == 1
    assert tasks[0].title == "call mom"
    assert tasks[0].source == "commitment"
    assert tasks[0].deadline == datetime(2026, 7, 22, 20, 0)


async def test_sensor_commitment_without_time_gets_ttl(make_brain, store):
    brain = make_brain(
        {
            "pony": "ok!",
            "message-sensor": {
                "done_task_ids": [],
                "maybe_done_task_ids": [],
                "commitments": [{"text": "eat lunch", "when": ""}],
            },
        }
    )
    before = datetime.now()
    await brain.respond("I should eat lunch")
    task = store.pending()[0]
    # "eat" is a micro hint → short TTL (90 min), not 36 h
    assert task.deadline - before < timedelta(hours=2)


async def test_sensor_does_not_duplicate_assistant_planner_command(make_brain, store):
    brain = make_brain(
        {
            "pony": "routine added",
            "message-sensor": {
                "done_task_ids": [],
                "maybe_done_task_ids": [],
                "commitments": [{"text": "Breakfast routine", "when": ""}],
            },
        }
    )
    await brain.respond("Set up a daily Breakfast routine at 08:00")
    assert store.pending() == []


async def test_sensor_ungrounded_commitment_skipped(make_brain, store):
    brain = make_brain(
        {
            "pony": "ok",
            "message-sensor": {
                "done_task_ids": [],
                "maybe_done_task_ids": [],
                "commitments": [{"text": "conquer the moon", "when": ""}],
            },
        }
    )
    await brain.respond("nice weather today")  # no shared words → invention
    assert store.pending() == []


async def test_sensor_disabled_by_config(make_brain, store):
    brain = make_brain(
        {
            "pony": "ok",
            "message-sensor": {
                "done_task_ids": [],
                "maybe_done_task_ids": [],
                "commitments": [{"text": "call mom", "when": ""}],
            },
        },
        auto_track_commitments=False,
    )
    await brain.respond("I'll call mom later")
    assert store.pending() == []


async def test_sensor_failure_fails_open(make_brain, store):
    def boom(messages, tools):
        raise RuntimeError("provider down")

    brain = make_brain({"pony": "still here!", "message-sensor": boom})
    assert await brain.respond("hello") == "still here!"


# ── time parsing: LLM primary, offline fallback ──────────────────────
def test_parse_when_llm_primary(make_brain):
    brain = make_brain({"when-sensor": {"datetime": "2026-12-24 18:00"}})
    assert brain.parse_when("wigilia wieczorem") == datetime(2026, 12, 24, 18, 0)


def test_parse_when_falls_back_offline_on_error(make_brain):
    def boom(messages, tools):
        raise RuntimeError("no network")

    brain = make_brain({"when-sensor": boom})
    now = datetime(2026, 7, 22, 14, 0)
    assert brain.parse_when("in 2h", now) == now + timedelta(hours=2)


def test_parse_when_empty_answer_is_none(make_brain):
    brain = make_brain({"when-sensor": {"datetime": ""}})
    assert brain.parse_when("no time here") is None


# ── switching ─────────────────────────────────────────────────────────
def test_switch_character_rebuilds_prompt(make_brain):
    brain = make_brain({})
    twilight_prompt = brain._spec(FAST).system_prompt
    brain.set_character("rainbow-dash")
    dash_prompt = brain._spec(FAST).system_prompt
    assert dash_prompt != twilight_prompt
    assert "Rainbow Dash" in dash_prompt


def test_switch_provider_changes_models(make_brain, config):
    brain = make_brain({})
    brain.set_provider("ollama")
    assert brain._spec(FAST).model_id == "qwen3:8b"
    assert brain._spec(SLOW).model_id == "qwen3:32b"
    assert brain._spec(VISION).base_url == "http://localhost:11434/v1"


def test_recent_screen_activity_tool_only_appears_in_fast_lane(make_brain):
    brain = make_brain({})

    assert "recent_screen_activity" in {tool.name for tool in brain._spec(FAST).tools}
    assert brain._spec(SLOW).tools == ()
    assert brain._spec(VISION).tools == ()
    assert brain._sensor_spec("sensor", "sensor").tools == ()


def test_switch_unknown_provider_raises(make_brain):
    import pytest

    brain = make_brain({})
    with pytest.raises(KeyError):
        brain.set_provider("nope")
