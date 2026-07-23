"""Tests for token usage capture and recording.

Covers:
- Lane/purpose mapping helpers
- Prompt token estimation
- TokenUsageStore record/recent/summary
- TokenCaptureClient with estimated and exact usage paths
- Brain wiring: token_callback fires for all lanes
- Integration: callback writes to TokenUsageStore
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clipponyai.accountability import get_stores, TokenUsageStore
from clipponyai.brain import PonyBrain
from clipponyai.config import Config
from clipponyai.tasks import TaskStore
from clipponyai.token_capture import (
    estimate_prompt_tokens,
    lane_from_agent_id,
    purpose_from_agent_id,
    RawResponseOpenAICompatClient,
    TokenCaptureClient,
)
from open_agent_compiler.interactive.runner import ChatResponse


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "token_test.db")
    yield s
    s.close()


@pytest.fixture
def token_usage(store) -> TokenUsageStore:
    get_stores(store)
    return TokenUsageStore(store)


# ── minimal fake clients for wrapping tests ───────────────────────────


class _FakeNoUsage:
    """Fake client with no _last_response -- forces estimation path."""

    def __init__(self, content: str = "ok", spec: Any = None):
        self.content = content
        self.calls: list[dict] = []
        self.spec = spec

    def complete(self, *, messages, tools=None, model="", **params):
        self.calls.append({"messages": list(messages), "model": model})
        return ChatResponse(content=self.content, tool_calls=[])


class _FakeWithUsage:
    """Fake client that sets _last_response with exact usage."""

    def __init__(self, prompt_tokens: int = 42, completion_tokens: int = 17):
        self._prompt = prompt_tokens
        self._completion = completion_tokens
        self.calls: list[dict] = []
        self.spec = None

    def complete(self, *, messages, tools=None, model="", **params):
        self.calls.append({"messages": list(messages), "model": model})
        self._last_response = _make_fake_raw(self._prompt, self._completion)
        return ChatResponse(content="exact answer", tool_calls=[])


def _make_fake_raw(prompt_tokens: int, completion_tokens: int):
    """Build a fake raw response object with usage."""

    class FakeUsage:
        def __init__(self):
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens
            self.total_tokens = prompt_tokens + completion_tokens

    class FakeRaw:
        def __init__(self):
            self.usage = FakeUsage()

    return FakeRaw()


# ── lane/purpose mapping ─────────────────────────────────────────────


def test_lane_mapping_all_lanes():
    assert lane_from_agent_id("pony") == "chat"
    assert lane_from_agent_id("pony-slow") == "slow"
    assert lane_from_agent_id("pony-vision") == "vision"
    assert lane_from_agent_id("message-sensor") == "sensor"
    assert lane_from_agent_id("when-sensor") == "sensor"
    assert lane_from_agent_id("log-analyst") == "sensor"
    assert lane_from_agent_id("unknown") == "other"


def test_purpose_mapping_all_purposes():
    assert purpose_from_agent_id("pony") == "chat"
    assert purpose_from_agent_id("pony-slow") == "deep_think"
    assert purpose_from_agent_id("pony-vision") == "look_at_screen"
    assert purpose_from_agent_id("message-sensor") == "message-sensor"
    assert purpose_from_agent_id("when-sensor") == "when-sensor"
    assert purpose_from_agent_id("log-analyst") == "log-analyst"
    assert purpose_from_agent_id("weird") == "weird"


# ── estimation ────────────────────────────────────────────────────────


def test_estimate_prompt_tokens_basic():
    msgs = [{"role": "user", "content": "hello world"}]
    assert estimate_prompt_tokens(msgs, None) >= 1


def test_estimate_prompt_tokens_with_tools():
    msgs = [{"role": "user", "content": "do something"}]
    tools = [{"type": "function", "function": {"name": "add_task", "parameters": {}}}]
    assert estimate_prompt_tokens(msgs, tools) >= 1


def test_estimate_prompt_tokens_scales_with_content():
    short = estimate_prompt_tokens([{"role": "user", "content": "hi"}], None)
    long = estimate_prompt_tokens([{"role": "user", "content": "x" * 400}], None)
    assert long > short


# ── TokenUsageStore ───────────────────────────────────────────────────


def test_record_and_readback(token_usage: TokenUsageStore):
    entry = token_usage.record(
        lane="chat",
        purpose="user_query",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=120,
        completion_tokens=80,
        estimated=False,
    )
    assert entry.total_tokens == 200
    assert entry.estimated is False


def test_record_estimated(token_usage: TokenUsageStore):
    entry = token_usage.record(
        lane="sensor",
        purpose="when-sensor",
        provider="ollama",
        model="qwen3:8b",
        prompt_tokens=5,
        completion_tokens=3,
        estimated=True,
    )
    assert entry.total_tokens == 8
    assert entry.estimated is True


def test_recent_order(token_usage: TokenUsageStore):
    token_usage.record(lane="chat", prompt_tokens=10, completion_tokens=5)
    token_usage.record(lane="slow", prompt_tokens=20, completion_tokens=15)
    recent = token_usage.recent()
    assert len(recent) == 2
    assert recent[0].lane == "chat"
    assert recent[1].lane == "slow"


def test_summary_all(token_usage: TokenUsageStore):
    token_usage.record(lane="chat", prompt_tokens=100, completion_tokens=50)
    token_usage.record(lane="chat", prompt_tokens=200, completion_tokens=100)
    token_usage.record(lane="sensor", prompt_tokens=30, completion_tokens=10)
    summary = token_usage.summary("all")
    by_lane = {s["lane"]: s for s in summary}
    assert by_lane["chat"]["total_tokens"] == 450
    assert by_lane["chat"]["count"] == 2
    assert by_lane["sensor"]["total_tokens"] == 40


def test_summary_today(token_usage: TokenUsageStore):
    token_usage.record(lane="chat", prompt_tokens=50, completion_tokens=25)
    summary = token_usage.summary("today")
    chat_row = [s for s in summary if s["lane"] == "chat"]
    assert len(chat_row) == 1
    assert chat_row[0]["total_tokens"] == 75


def test_summary_7d(token_usage: TokenUsageStore):
    token_usage.record(lane="chat", prompt_tokens=10, completion_tokens=5)
    assert len(token_usage.summary("7d")) >= 1


def test_summary_empty(token_usage: TokenUsageStore):
    assert token_usage.summary("all") == []


# ── TokenCaptureClient ───────────────────────────────────────────────


def test_capture_estimated_usage():
    """No _last_response on inner client -> estimated=True."""
    recorded: list[dict] = []

    def cb(lane, purpose, provider, model, pt, ct, estimated):
        recorded.append({
            "lane": lane, "purpose": purpose, "provider": provider,
            "model": model, "pt": pt, "ct": ct, "estimated": estimated,
        })

    inner = _FakeNoUsage(content="hello there")
    wrapper = TokenCaptureClient(
        inner, cb,
        lane="chat", purpose="chat", provider="openai", model="gpt-4o-mini",
    )
    wrapper.complete(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="gpt-4o-mini",
    )
    assert len(recorded) == 1
    r = recorded[0]
    assert r["lane"] == "chat"
    assert r["purpose"] == "chat"
    assert r["provider"] == "openai"
    assert r["model"] == "gpt-4o-mini"
    assert r["estimated"] is True
    assert r["pt"] >= 1
    assert r["ct"] >= 1


def test_capture_exact_usage():
    """Inner client has _last_response -> estimated=False, exact values."""
    recorded: list[dict] = []

    def cb(lane, purpose, provider, model, pt, ct, estimated):
        recorded.append({"pt": pt, "ct": ct, "estimated": estimated})

    inner = _FakeNoUsage(content="exact answer")
    inner._last_response = _make_fake_raw(42, 17)
    wrapper = TokenCaptureClient(
        inner, cb,
        lane="slow", purpose="deep_think", provider="openai", model="gpt-4o",
    )
    wrapper.complete(
        messages=[{"role": "user", "content": "think hard"}],
        tools=None,
        model="gpt-4o",
    )
    assert len(recorded) == 1
    assert recorded[0]["pt"] == 42
    assert recorded[0]["ct"] == 17
    assert recorded[0]["estimated"] is False


def test_capture_no_double_record():
    """Two complete() calls produce exactly two callback invocations."""
    recorded: list[int] = []

    def cb(*a):
        recorded.append(1)

    inner = _FakeNoUsage(content="ok")
    wrapper = TokenCaptureClient(
        inner, cb,
        lane="chat", purpose="chat", provider="openai", model="gpt-4o-mini",
    )
    wrapper.complete(messages=[{"role": "user", "content": "a"}], model="gpt-4o-mini")
    wrapper.complete(messages=[{"role": "user", "content": "b"}], model="gpt-4o-mini")
    assert len(recorded) == 2


def test_capture_forwards_calls_and_spec():
    """TokenCaptureClient exposes inner.client.calls and .spec for test introspection."""
    fake_spec = type("FakeSpec", (), {"agent_id": "pony"})()
    inner = _FakeNoUsage(content="ok", spec=fake_spec)
    wrapper = TokenCaptureClient(
        inner, lambda *a: None,
        lane="chat", purpose="chat", provider="openai", model="gpt-4o-mini",
    )
    wrapper.complete(messages=[{"role": "user", "content": "hi"}], model="gpt-4o-mini")
    assert len(wrapper.calls) == 1
    assert wrapper.spec is fake_spec


# ── Brain wiring: callback fires for every lane ──────────────────────


def _make_brain(store, callback, lane_name):
    """Build a PonyBrain with a fake client factory and token callback."""

    def factory(spec):
        class C:
            def __init__(self, s):
                self.spec = s
                self.calls = []

            def complete(self, *, messages, tools=None, model="", **params):
                self.calls.append({"messages": list(messages)})
                if lane_name == "sensor":
                    return ChatResponse(
                        content=json.dumps({
                            "done_task_ids": [],
                            "maybe_done_task_ids": [],
                            "commitments": [],
                        }),
                        tool_calls=[],
                    )
                return ChatResponse(content="response", tool_calls=[])

        return C(spec)

    return PonyBrain(
        Config(), store, client_factory=factory, token_callback=callback,
    )


def test_brain_chat_lane_triggers_callback(store):
    captured: list[dict] = []

    def cb(lane, purpose, provider, model, pt, ct, estimated):
        captured.append({"lane": lane, "purpose": purpose, "estimated": estimated})

    brain = _make_brain(store, cb, "chat")
    brain._run(brain._spec("fast"), "hello")
    assert len(captured) == 1
    assert captured[0]["lane"] == "chat"
    assert captured[0]["purpose"] == "chat"
    assert captured[0]["estimated"] is True


def test_brain_slow_lane_triggers_callback(store):
    captured: list[dict] = []

    def cb(lane, purpose, provider, model, pt, ct, estimated):
        captured.append({"lane": lane, "purpose": purpose})

    brain = _make_brain(store, cb, "chat")
    brain._run(brain._spec("slow"), "plan my week")
    assert len(captured) == 1
    assert captured[0]["lane"] == "slow"
    assert captured[0]["purpose"] == "deep_think"


def test_brain_vision_lane_triggers_callback(store):
    captured: list[dict] = []

    def cb(lane, purpose, provider, model, pt, ct, estimated):
        captured.append({"lane": lane, "purpose": purpose})

    brain = _make_brain(store, cb, "chat")
    brain._run(brain._spec("vision"), "describe screen")
    assert len(captured) == 1
    assert captured[0]["lane"] == "vision"
    assert captured[0]["purpose"] == "look_at_screen"


def test_brain_sensor_lane_triggers_callback(store):
    captured: list[dict] = []

    def cb(lane, purpose, provider, model, pt, ct, estimated):
        captured.append({"lane": lane, "purpose": purpose})

    brain = _make_brain(store, cb, "sensor")
    sensor_spec = brain._sensor_spec("test prompt", "message-sensor")
    brain._run(sensor_spec, "test input")
    assert len(captured) == 1
    assert captured[0]["lane"] == "sensor"
    assert captured[0]["purpose"] == "message-sensor"


# ── integration: callback writes to TokenUsageStore ──────────────────


def test_brain_wired_to_store(store):
    """End-to-end: brain callback writes to TokenUsageStore."""
    get_stores(store)
    token_usage = TokenUsageStore(store)

    def factory(spec):
        class C:
            def __init__(self, s):
                self.spec = s
                self.calls = []

            def complete(self, *, messages, tools=None, model="", **params):
                return ChatResponse(content="hello!", tool_calls=[])

        return C(spec)

    brain = PonyBrain(
        Config(), store, client_factory=factory,
        token_callback=lambda lane, purpose, provider, model, pt, ct, est:
            token_usage.record(
                lane=lane, purpose=purpose, provider=provider,
                model=model, prompt_tokens=pt, completion_tokens=ct,
                estimated=est,
            ),
    )
    brain._run(brain._spec("fast"), "hi")
    entries = token_usage.recent()
    assert len(entries) == 1
    assert entries[0].lane == "chat"
    assert entries[0].estimated is True


def test_brain_no_callback_silent(store):
    """Without token_callback, no token usage is recorded."""
    get_stores(store)
    token_usage = TokenUsageStore(store)

    def factory(spec):
        class C:
            def __init__(self, s):
                self.spec = s
                self.calls = []

            def complete(self, *, messages, tools=None, model="", **params):
                return ChatResponse(content="ok", tool_calls=[])

        return C(spec)

    brain = PonyBrain(
        Config(), store, client_factory=factory, token_callback=None,
    )
    brain._run(brain._spec("fast"), "hi")
    assert token_usage.recent() == []


# ── RawResponseOpenAICompatClient ─────────────────────────────────────


def test_raw_response_client_instantiable():
    client = RawResponseOpenAICompatClient(api_key="test")
    assert client._last_response is None


def test_raw_response_client_from_spec():
    from open_agent_compiler import AgentDefinition, AgentHeader
    from open_agent_compiler.interactive import build_interactive_spec
    from clipponyai.providers import make_live_profile
    from clipponyai.config import ProviderConfig

    agent = AgentDefinition(
        header=AgentHeader(agent_id="test", name="test", description="test"),
        usage_explanation_short="test",
        usage_explanation_long="test",
        system_prompt="you are a test agent",
    )
    provider_cfg = ProviderConfig(fast_model="gpt-4o-mini")
    spec = build_interactive_spec(
        agent=agent,
        live_profile=make_live_profile("test-provider", provider_cfg, "fast"),
    )
    client = RawResponseOpenAICompatClient.from_spec(spec)
    assert client._base_url is None
    assert client.default_params.get("temperature") == 0.7
