"""MCP integration tests for the chat brain, with no network access."""

from __future__ import annotations

from dataclasses import dataclass

from clipponyai.brain import TOOL_SPECS
from clipponyai.mcp import MCPToolInfo
from clipponyai.providers import FAST, SLOW, VISION

EMPTY_SENSE = {"done_task_ids": [], "maybe_done_task_ids": [], "commitments": []}


@dataclass
class _State:
    status: str


class FakeMCPManager:
    def __init__(
        self,
        *,
        result: str = "external result",
        instructions: dict[str, str] | None = None,
    ) -> None:
        self.result = result
        self._instructions = (
            {"test": "test-instructions"} if instructions is None else instructions
        )
        self.calls: list[tuple[str, dict]] = []
        self._tools = [
            MCPToolInfo(
                namespaced_name="mcp__test__echo",
                server="test",
                original_name="echo",
                description="Echo text from the test service.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]

    def tools(self):
        return list(self._tools)

    def call(self, name, args):
        self.calls.append((name, args))
        return self.result

    def instructions(self):
        return dict(self._instructions)

    def status(self):
        return {"test": _State("connected")}


def test_mcp_tools_only_appear_in_fast_chat_spec(make_brain):
    manager = FakeMCPManager()
    brain = make_brain({}, mcp_manager=manager)

    fast_tools = {tool.name: tool for tool in brain._spec(FAST).tools}
    assert "add_task" in fast_tools
    assert fast_tools["mcp__test__echo"].description.startswith("[test] ")
    assert fast_tools["mcp__test__echo"].input_schema == manager._tools[0].input_schema
    assert brain._sensor_spec("sensor", "sensor").tools == ()
    assert brain._spec(SLOW).tools == ()
    assert brain._spec(VISION).tools == ()


def test_mcp_tools_are_snapshotted_per_fast_spec_access(make_brain):
    manager = FakeMCPManager()
    brain = make_brain({}, mcp_manager=manager)
    assert any(tool.name == "mcp__test__echo" for tool in brain._spec(FAST).tools)

    manager._tools.clear()
    assert {tool.name for tool in brain._spec(FAST).tools} == {
        tool.name for tool in TOOL_SPECS
    }


async def test_mcp_tool_call_is_routed_and_result_fed_back(make_brain):
    manager = FakeMCPManager(result="echoed externally")
    brain = make_brain(
        {
            "pony": [
                ("tool", "mcp__test__echo", {"text": "hello"}),
                "finished",
            ],
            "message-sensor": EMPTY_SENSE,
        },
        mcp_manager=manager,
    )

    assert await brain.respond("echo hello") == "finished"
    assert manager.calls == [("mcp__test__echo", {"text": "hello"})]
    pony = [client for client in brain._test_clients if client.spec.agent_id == "pony"][-1]
    tool_messages = [
        message
        for message in pony.calls[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages[0]["content"] == "echoed externally"


async def test_mcp_error_result_remains_visible_and_conversation_continues(make_brain):
    manager = FakeMCPManager(result="ERROR: boom")
    brain = make_brain(
        {
            "pony": [
                ("tool", "mcp__test__echo", {"text": "hello"}),
                "I recovered",
            ],
            "message-sensor": EMPTY_SENSE,
        },
        mcp_manager=manager,
    )

    assert await brain.respond("echo hello") == "I recovered"
    pony = [client for client in brain._test_clients if client.spec.agent_id == "pony"][-1]
    tool_messages = [
        message
        for message in pony.calls[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages[0]["content"] == "ERROR: boom"


async def test_connected_server_instructions_are_added_to_system_prompt(make_brain):
    brain = make_brain(
        {"pony": "ok", "message-sensor": EMPTY_SENSE},
        mcp_manager=FakeMCPManager(),
    )

    await brain.respond("hello")
    pony = [client for client in brain._test_clients if client.spec.agent_id == "pony"][-1]
    assert "## Connected external services" in pony.spec.system_prompt
    assert "[test] test-instructions" in pony.spec.system_prompt


def test_missing_server_instructions_use_compact_fallback(make_brain):
    brain = make_brain({}, mcp_manager=FakeMCPManager(instructions={}))
    assert "[test] (no description provided)" in brain._mcp_context_note()


def test_no_mcp_manager_is_fully_inert(make_brain):
    brain = make_brain({})
    fast_spec = brain._spec(FAST)

    assert fast_spec is brain._spec(FAST)
    assert fast_spec.tools == tuple(TOOL_SPECS)
    assert "Connected external services" not in fast_spec.system_prompt
    assert brain._mcp_context_note() == ""
    assert brain._tool_runner("mcp__test__echo", {}) == (
        "ERROR: unknown tool mcp__test__echo"
    )
