"""Unit tests for the threaded MCP manager."""

from __future__ import annotations

import json
import logging

import pytest
from fastmcp import Client, FastMCP
from mcp.types import TextContent

from clipponyai.config import Config, MCPConfig, MCPServerConfig
from clipponyai.mcp import MCPManager, ServerStatus


@pytest.fixture
def mcp_server():
    server = FastMCP("test-server", instructions="test-instructions")

    @server.tool
    def echo(text: str) -> TextContent:
        return TextContent(type="text", text=text)

    @server.tool
    def add(a: int, b: int) -> dict[str, int]:
        return {"sum": a + b}

    @server.tool
    def boom() -> None:
        raise RuntimeError("test explosion")

    return server


def manager_for(
    server,
    *,
    server_config: MCPServerConfig | None = None,
    log_fn=None,
) -> MCPManager:
    config = MCPConfig(
        enabled=True,
        servers={"test": server_config or MCPServerConfig(url="https://unused.invalid/mcp")},
    )
    return MCPManager(
        config,
        log_fn=log_fn,
        client_factory=lambda _name, _config: Client(server),
    )


def test_connect_discovers_namespaced_tools_and_instructions(mcp_server):
    manager = manager_for(mcp_server)
    try:
        manager.start()
        assert manager.wait_ready(timeout=2)

        tools = manager.tools()
        assert {tool.namespaced_name for tool in tools} == {
            "mcp__test__add",
            "mcp__test__boom",
            "mcp__test__echo",
        }
        echo = next(tool for tool in tools if tool.original_name == "echo")
        assert echo.server == "test"
        assert echo.input_schema["required"] == ["text"]
        assert manager.instructions() == {"test": "test-instructions"}
        assert manager.status()["test"].status is ServerStatus.CONNECTED
        assert manager.status()["test"].tool_count == 3
    finally:
        manager.stop()


def test_sync_call_formats_text_structured_results_and_errors(mcp_server):
    manager = manager_for(mcp_server)
    try:
        manager.start()
        assert manager.wait_ready(timeout=2)

        assert manager.call("mcp__test__echo", {"text": "hello"}) == "hello"
        assert json.loads(manager.call("mcp__test__add", {"a": 2, "b": 5})) == {"sum": 7}
        assert manager.call("mcp__test__boom", {}).startswith("ERROR:")
        assert "test explosion" in manager.call("mcp__test__boom", {})
        assert manager.call("mcp__test__missing", {}) == (
            "ERROR: unknown MCP tool 'mcp__test__missing'"
        )
        assert manager.call("not_namespaced", {}) == (
            "ERROR: unknown MCP tool 'not_namespaced'"
        )
    finally:
        manager.stop()


@pytest.mark.parametrize(
    ("server_config", "expected"),
    [
        (
            MCPServerConfig(
                url="https://unused.invalid/mcp",
                tool_allow=["echo", "add"],
            ),
            {"echo", "add"},
        ),
        (
            MCPServerConfig(
                url="https://unused.invalid/mcp",
                tool_deny=["boom"],
            ),
            {"echo", "add"},
        ),
        (
            MCPServerConfig(
                url="https://unused.invalid/mcp",
                tool_allow=["echo", "boom"],
                tool_deny=["boom"],
            ),
            {"echo"},
        ),
    ],
)
def test_tool_allow_and_deny_filters(mcp_server, server_config, expected):
    manager = manager_for(mcp_server, server_config=server_config)
    try:
        manager.start()
        assert manager.wait_ready(timeout=2)
        assert {tool.original_name for tool in manager.tools()} == expected
    finally:
        manager.stop()


def test_disabled_server_and_global_switch_do_not_connect(mcp_server):
    calls = []

    def factory(name, config):
        calls.append((name, config))
        return Client(mcp_server)

    disabled_server = MCPManager(
        MCPConfig(
            enabled=True,
            servers={
                "test": MCPServerConfig(
                    url="https://unused.invalid/mcp",
                    enabled=False,
                )
            },
        ),
        client_factory=factory,
    )
    disabled_server.start()
    assert disabled_server.wait_ready(timeout=0)
    assert disabled_server.tools() == []
    assert disabled_server.status()["test"].status is ServerStatus.DISABLED

    disabled_globally = MCPManager(
        MCPConfig(
            enabled=False,
            servers={"test": MCPServerConfig(url="https://unused.invalid/mcp")},
        ),
        client_factory=factory,
    )
    disabled_globally.start()
    assert disabled_globally.wait_ready(timeout=0)
    assert disabled_globally.tools() == []
    assert disabled_globally.status()["test"].status is ServerStatus.DISABLED
    assert calls == []


def test_environment_placeholders_resolve_at_connect_time(mcp_server, monkeypatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-value")
    seen_configs = []
    config = MCPConfig(
        enabled=True,
        servers={
            "test": MCPServerConfig(
                url="https://unused.invalid/mcp",
                headers={"Authorization": "Bearer ${TEST_MCP_TOKEN}"},
            )
        },
    )

    def factory(_name, server_config):
        seen_configs.append(server_config)
        return Client(mcp_server)

    manager = MCPManager(config, client_factory=factory)
    try:
        manager.start()
        assert manager.wait_ready(timeout=2)
        assert seen_configs[0].headers == {"Authorization": "Bearer secret-value"}
        assert config.servers["test"].headers == {
            "Authorization": "Bearer ${TEST_MCP_TOKEN}"
        }
    finally:
        manager.stop()


def test_missing_environment_placeholder_sets_error_state(mcp_server, monkeypatch):
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    logs = []
    manager = manager_for(
        mcp_server,
        server_config=MCPServerConfig(
            url="https://unused.invalid/mcp",
            headers={"Authorization": "Bearer ${MISSING_MCP_TOKEN}"},
        ),
        log_fn=logs.append,
    )
    try:
        manager.start()
        assert manager.wait_ready(timeout=2)
        state = manager.status()["test"]
        assert state.status is ServerStatus.ERROR
        assert "MISSING_MCP_TOKEN" in state.last_error
        assert manager.tools() == []
        assert logs and "MISSING_MCP_TOKEN" in logs[0]
    finally:
        manager.stop()


def test_stop_terminates_event_loop_thread(mcp_server):
    manager = manager_for(mcp_server)
    manager.start()
    assert manager.wait_ready(timeout=2)
    thread = manager._thread

    manager.stop()

    assert thread is not None
    assert not thread.is_alive()


def test_empty_manager_start_and_stop_is_a_noop():
    manager = MCPManager(MCPConfig())
    manager.start()
    manager.stop()
    assert manager.wait_ready(timeout=0)
    assert manager.tools() == []


async def test_core_starts_and_stops_mcp_manager_without_dangling_thread(
    mcp_server,
    caplog,
):
    from clipponyai.app import Core

    config = Config()
    config.mcp = MCPConfig(
        enabled=True,
        servers={"test": MCPServerConfig(url="https://unused.invalid/mcp")},
    )
    manager = MCPManager(
        config.mcp,
        client_factory=lambda _name, _config: Client(mcp_server),
    )
    core = Core(config, mcp_manager=manager)

    with caplog.at_level(logging.INFO, logger="clipponyai.app"):
        await core.start()
        assert manager.wait_ready(timeout=2)
        thread = manager._thread
        await core.stop()

    assert "MCP: 1 servers configured, connecting in background..." in caplog.messages
    assert thread is not None
    assert not thread.is_alive()
