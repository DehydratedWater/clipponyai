"""Unit tests for the check-mcp CLI command, using in-memory MCP servers."""

from __future__ import annotations

from fastmcp import Client, FastMCP
from mcp.types import TextContent

from clipponyai.cli import main
from clipponyai.config import Config, MCPConfig, MCPServerConfig
from clipponyai.mcp import MCPManager


def _configured_mcp() -> Config:
    config = Config()
    config.mcp = MCPConfig(
        enabled=True,
        servers={"test": MCPServerConfig(url="https://unused.invalid/mcp")},
    )
    return config


def test_check_mcp_registered_in_help(capsys):
    try:
        main(["--help"])
    except SystemExit:
        pass
    assert "check-mcp" in capsys.readouterr().out


def test_check_mcp_lists_in_memory_server_tools(capsys, monkeypatch):
    server = FastMCP("test-server", instructions="Use this test service.")

    @server.tool
    def echo(text: str) -> TextContent:
        return TextContent(type="text", text=text)

    _configured_mcp().save()
    real_manager = MCPManager

    def manager_factory(config):
        return real_manager(
            config,
            client_factory=lambda _name, _config: Client(server),
        )

    monkeypatch.setattr("clipponyai.mcp.MCPManager", manager_factory)

    assert main(["check-mcp"]) == 0
    output = capsys.readouterr().out
    assert "test  CONNECTED" in output
    assert "mcp__test__echo" in output
    assert "yes" in output


def test_check_mcp_failing_server_returns_1_with_error(capsys, monkeypatch):
    _configured_mcp().save()
    real_manager = MCPManager

    def manager_factory(config):
        def fail(_name, _config):
            raise ConnectionError("connection refused")

        return real_manager(config, client_factory=fail)

    monkeypatch.setattr("clipponyai.mcp.MCPManager", manager_factory)

    assert main(["check-mcp"]) == 1
    output = capsys.readouterr().out
    assert "test  ERROR" in output
    assert "connection refused" in output


def test_check_mcp_disabled_returns_0(capsys):
    Config().save()

    assert main(["check-mcp"]) == 0
    assert "MCP disabled" in capsys.readouterr().out


def test_check_mcp_server_filter_rejects_unknown_name(capsys):
    _configured_mcp().save()

    assert main(["check-mcp", "--server", "missing"]) == 1
    assert "not configured" in capsys.readouterr().out
