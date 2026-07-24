"""Live LLM tests — gated by CLIPPONYAI_LIVE_BASE_URL env var.

These tests hit a real OpenAI-compatible endpoint. They are marked with
``@pytest.mark.live`` so the normal test suite skips them.

Run with::

    CLIPPONYAI_LIVE_BASE_URL=http://127.0.0.1:8082/v1 pytest -m live

The model defaults to the qwen27b-vllm provider's fast_model but can be
overridden with ``CLIPPONYAI_LIVE_MODEL``.
"""

from __future__ import annotations

import os

import pytest

from open_agent_compiler import AgentDefinition, AgentHeader
from open_agent_compiler.interactive import build_interactive_spec, run_interactive
from open_agent_compiler.interactive.runner import OpenAICompatClient
from open_agent_compiler.interactive.spec import ToolSpec

from clipponyai.awareness import PonyBrainAssessor
from clipponyai.brain import PonyBrain
from clipponyai.cli import _make_synthetic_vision_image
from clipponyai.config import Config, ProviderConfig
from clipponyai.providers import FAST, make_live_profile
from clipponyai.tasks import TaskStore

LIVE_BASE_URL = os.environ.get("CLIPPONYAI_LIVE_BASE_URL")
LIVE_MODEL = os.environ.get("CLIPPONYAI_LIVE_MODEL", "cyankiwi/Qwen3.5-27B-AWQ-BF16-INT8")

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def _live_provider_cfg():
    return ProviderConfig(
        base_url=LIVE_BASE_URL,
        fast_model=LIVE_MODEL,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


@pytest.fixture(scope="module")
def _live_spec(_live_provider_cfg):
    agent = AgentDefinition(
        header=AgentHeader(
            agent_id="live-test",
            name="live-test",
            description="live test agent",
        ),
        usage_explanation_short="live",
        usage_explanation_long="live test",
        system_prompt="You are a helpful assistant. Answer briefly.",
    )
    return build_interactive_spec(
        agent=agent,
        live_profile=make_live_profile("live", _live_provider_cfg, FAST),
    )


@pytest.fixture(scope="module")
def _live_client(_live_spec):
    return OpenAICompatClient.from_spec(_live_spec)


@pytest.mark.skipif(not LIVE_BASE_URL, reason="set CLIPPONYAI_LIVE_BASE_URL to run live tests")
def test_live_basic_chat(_live_spec, _live_client):
    """The endpoint returns a non-empty text response to a simple prompt."""
    result = run_interactive(
        _live_spec, "Say pong in one word.", client=_live_client, max_tool_rounds=0,
    )
    assert not result.error, f"run error: {result.error}"
    text = (result.output_text or "").strip()
    assert text, "empty response from live endpoint"
    assert len(text) < 500, "response unexpectedly long for a one-word prompt"


@pytest.mark.skipif(not LIVE_BASE_URL, reason="set CLIPPONYAI_LIVE_BASE_URL to run live tests")
def test_live_tool_call(_live_spec, _live_client):
    """The endpoint can emit tool calls when schemas are provided."""
    tool_spec = ToolSpec(
        name="echo",
        description="Repeat the given text back.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    spec_with_tools = _live_spec.model_copy(update={"tools": (tool_spec,)})

    def tool_runner(name: str, args: dict) -> str:
        if name == "echo":
            return f"echoed: {args.get('text', '')}"
        return f"unknown tool {name}"

    result = run_interactive(
        spec_with_tools,
        'Use the echo tool to say "hello from tool".',
        client=_live_client,
        tool_runner=tool_runner,
        max_tool_rounds=3,
    )
    assert not result.error, f"run error: {result.error}"
    text = (result.output_text or "").strip()
    assert text, "empty response from live endpoint after tool call"


@pytest.mark.skipif(not LIVE_BASE_URL, reason="set CLIPPONYAI_LIVE_BASE_URL to run live tests")
def test_live_multimodal_awareness_path(_live_provider_cfg, tmp_path):
    """The configured VISION lane sees an image through the real awareness path."""
    config = Config()
    config.llm.active = "live-vision"
    config.llm.providers["live-vision"] = _live_provider_cfg.model_copy(
        update={"vision_model": LIVE_MODEL}
    )
    store = TaskStore(tmp_path / "live-vision.sqlite3")
    try:
        assessment = PonyBrainAssessor(PonyBrain(config, store)).assess(
            _make_synthetic_vision_image(),
            current_time="2026-07-22 10:00 (Wednesday)",
            work_hours_status="Currently INSIDE work hours.",
            task_overview="(none)",
            focus_policy=(
                "Interrupt only if the image does not have red on the left and blue on the right."
            ),
        )
    finally:
        store.close()

    assert assessment.should_interrupt is False
    assert assessment.confidence >= 0.8
    assert assessment.reason
