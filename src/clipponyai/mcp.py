"""Threaded MCP client manager with a synchronous tool-call facade."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from fastmcp import Client

from .config import MCPConfig, MCPServerConfig

logger = logging.getLogger(__name__)

_ENV_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
_MISSING = object()
_TOOL_PREFIX = "mcp__"
_RETRY_DELAYS = (5.0, 15.0, 60.0, 120.0)


class ServerStatus(str, Enum):
    """Connection lifecycle state for one configured server."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass(frozen=True)
class MCPToolInfo:
    """Model-facing metadata for a discovered MCP tool."""

    namespaced_name: str
    server: str
    original_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ServerState:
    """Public snapshot of one MCP server's state."""

    status: ServerStatus
    tool_count: int = 0
    instructions: str | None = None
    last_error: str | None = None


ClientFactory = Callable[[str, MCPServerConfig], Any]
LogFn = Callable[[str], None]


class MCPManager:
    """Own MCP connections on a private event loop and expose sync operations."""

    def __init__(
        self,
        config: MCPConfig,
        log_fn: LogFn | None = None,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self._log_fn = log_fn or logger.warning
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._started_event = threading.Event()
        self._ready_event = threading.Event()
        self._ready_servers: set[str] = set()
        self._clients: dict[str, Any] = {}
        self._tools_by_server: dict[str, tuple[MCPToolInfo, ...]] = {}
        self._enabled_names = {
            name for name, server in config.servers.items() if config.enabled and server.enabled
        }
        self._states = {
            name: ServerState(
                ServerStatus.CONNECTING
                if name in self._enabled_names
                else ServerStatus.DISABLED
            )
            for name in config.servers
        }
        if not self._enabled_names:
            self._ready_event.set()

    def start(self) -> None:
        """Start the private event-loop thread, unless MCP has no enabled servers."""

        if not self._enabled_names:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready_event.clear()
            self._ready_servers.clear()
            self._started_event.clear()
            for name in self._enabled_names:
                self._states[name] = ServerState(ServerStatus.CONNECTING)
            self._thread = threading.Thread(
                target=self._thread_main,
                name="clipponyai-mcp",
                daemon=True,
            )
            self._thread.start()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Wait until every enabled server has connected or failed at least once."""

        return self._ready_event.wait(timeout)

    def tools(self) -> list[MCPToolInfo]:
        """Return a stable snapshot of tools from connected servers."""

        with self._lock:
            tools = [
                tool
                for name in sorted(self._tools_by_server)
                if self._states[name].status is ServerStatus.CONNECTED
                for tool in self._tools_by_server[name]
            ]
        return sorted(tools, key=lambda tool: tool.namespaced_name)

    def call(self, namespaced_name: str, args: dict[str, Any]) -> str:
        """Synchronously invoke a discovered tool; errors are returned as strings."""

        parsed = self._parse_tool_name(namespaced_name)
        if parsed is None:
            return f"ERROR: unknown MCP tool {namespaced_name!r}"
        server_name, original_name = parsed

        with self._lock:
            state = self._states.get(server_name)
            client = self._clients.get(server_name)
            loop = self._loop
            known_tools = {
                tool.namespaced_name for tool in self._tools_by_server.get(server_name, ())
            }
            server_config = self.config.servers.get(server_name)

        if state is None or server_config is None:
            return f"ERROR: unknown MCP server {server_name!r}"
        if state.status is not ServerStatus.CONNECTED or client is None:
            reason = state.last_error or f"server {server_name!r} is not connected"
            return f"ERROR: {reason}"
        if namespaced_name not in known_tools:
            return f"ERROR: unknown MCP tool {namespaced_name!r}"
        if loop is None or not loop.is_running():
            return "ERROR: MCP manager is not running"
        if threading.current_thread() is self._thread:
            return "ERROR: MCP calls cannot block the MCP event-loop thread"

        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(original_name, args, raise_on_error=False),
            loop,
        )
        try:
            result = future.result(timeout=server_config.timeout_seconds)
        except TimeoutError:
            future.cancel()
            return (
                f"ERROR: MCP tool {namespaced_name!r} timed out after "
                f"{server_config.timeout_seconds:g}s"
            )
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            return f"ERROR: {reason}"
        return self._format_result(result)

    def instructions(self) -> dict[str, str]:
        """Return declared instructions from connected servers."""

        with self._lock:
            return {
                name: state.instructions
                for name, state in self._states.items()
                if state.status is ServerStatus.CONNECTED and state.instructions
            }

    def status(self) -> dict[str, ServerState]:
        """Return immutable snapshots of all configured server states."""

        with self._lock:
            return {name: replace(state) for name, state in self._states.items()}

    def stop(self) -> None:
        """Close all clients and join the event-loop thread with a bounded wait."""

        with self._lock:
            thread = self._thread
            loop = self._loop
            stop_event = self._stop_event
        if thread is None:
            return
        if thread.is_alive() and not self._started_event.is_set():
            self._started_event.wait(timeout=1.0)
            with self._lock:
                loop = self._loop
                stop_event = self._stop_event
        if thread.is_alive() and (loop is None or stop_event is None):
            self._log("MCP event-loop thread did not initialize within 1 second")
            return
        if thread.is_alive():
            loop.call_soon_threadsafe(stop_event.set)
            thread.join(timeout=5.0)
            if thread.is_alive():
                self._log("MCP event-loop thread did not stop within 5 seconds")
                return
        with self._lock:
            self._thread = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            loop.run_until_complete(self._run())
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception as exc:
            self._log(f"MCP event loop failed: {exc}")
            self._mark_unready_servers_failed(exc)
        finally:
            with self._lock:
                self._clients.clear()
                self._tools_by_server.clear()
                self._stop_event = None
                self._loop = None
            asyncio.set_event_loop(None)
            loop.close()

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        self._started_event.set()
        tasks = [
            asyncio.create_task(self._supervise_server(name), name=f"mcp-{name}")
            for name in sorted(self._enabled_names)
        ]
        await self._stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise_server(self, name: str) -> None:
        retry_index = 0
        while self._stop_event is not None and not self._stop_event.is_set():
            self._set_state(name, ServerState(ServerStatus.CONNECTING))
            try:
                await self._connect_server(name)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                with self._lock:
                    self._clients.pop(name, None)
                    self._tools_by_server.pop(name, None)
                self._set_state(
                    name,
                    ServerState(ServerStatus.ERROR, last_error=reason),
                    ready=True,
                )
                self._log(f"MCP server {name!r} failed: {reason}")

            delay = _RETRY_DELAYS[min(retry_index, len(_RETRY_DELAYS) - 1)]
            retry_index += 1
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _connect_server(self, name: str) -> None:
        server_config = self._resolved_server_config(self.config.servers[name])
        client = self._make_client(name, server_config)
        async with client:
            raw_tools = await client.list_tools()
            tools = tuple(
                MCPToolInfo(
                    namespaced_name=f"{_TOOL_PREFIX}{name}__{tool.name}",
                    server=name,
                    original_name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.inputSchema),
                )
                for tool in raw_tools
                if self._tool_allowed(server_config, tool.name)
            )
            instructions = await self._client_instructions(client)
            with self._lock:
                self._clients[name] = client
                self._tools_by_server[name] = tools
            self._set_state(
                name,
                ServerState(
                    ServerStatus.CONNECTED,
                    tool_count=len(tools),
                    instructions=instructions,
                ),
                ready=True,
            )
            await self._stop_event.wait()

        with self._lock:
            self._clients.pop(name, None)
            self._tools_by_server.pop(name, None)

    def _make_client(self, name: str, server_config: MCPServerConfig) -> Any:
        if self._client_factory is not None:
            return self._client_factory(name, server_config)

        if server_config.command is not None:
            connection: dict[str, Any] = {
                "command": server_config.command,
                "args": server_config.args,
                "env": server_config.env,
            }
            if server_config.cwd is not None:
                connection["cwd"] = server_config.cwd
        else:
            connection = {
                "url": server_config.url,
                "headers": server_config.headers,
            }
        if server_config.type is not None:
            connection["transport"] = server_config.type
        return Client({"mcpServers": {name: connection}}, name=f"clipponyai-{name}")

    @staticmethod
    async def _client_instructions(client: Any) -> str | None:
        instructions = getattr(client, "instructions", _MISSING)
        if instructions is not _MISSING:
            return instructions
        initialize_result = client.initialize_result
        if initialize_result is None:
            initialize_result = await client.initialize()
        return initialize_result.instructions

    @staticmethod
    def _tool_allowed(server_config: MCPServerConfig, name: str) -> bool:
        allowed = not server_config.tool_allow or name in server_config.tool_allow
        return allowed and name not in server_config.tool_deny

    def _parse_tool_name(self, namespaced_name: str) -> tuple[str, str] | None:
        if not namespaced_name.startswith(_TOOL_PREFIX):
            return None
        for server_name in sorted(self.config.servers, key=len, reverse=True):
            prefix = f"{_TOOL_PREFIX}{server_name}__"
            if namespaced_name.startswith(prefix):
                original_name = namespaced_name[len(prefix) :]
                if original_name:
                    return server_name, original_name
        return None

    @staticmethod
    def _format_result(result: Any) -> str:
        text = "\n".join(
            block.text for block in result.content if getattr(block, "text", None) is not None
        )
        if result.is_error:
            return f"ERROR: {text or 'MCP tool call failed'}"
        if result.structured_content is not None:
            return json.dumps(result.structured_content, ensure_ascii=False)
        return text

    @classmethod
    def _resolved_server_config(cls, server_config: MCPServerConfig) -> MCPServerConfig:
        return server_config.model_copy(
            update={
                "env": {
                    key: cls._resolve_env_placeholders(value)
                    for key, value in server_config.env.items()
                },
                "headers": {
                    key: cls._resolve_env_placeholders(value)
                    for key, value in server_config.headers.items()
                },
            }
        )

    @staticmethod
    def _resolve_env_placeholders(value: str) -> str:
        def replace_placeholder(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"environment variable {name!r} is not set")
            return os.environ[name]

        return _ENV_PLACEHOLDER.sub(replace_placeholder, value)

    def _set_state(self, name: str, state: ServerState, *, ready: bool = False) -> None:
        with self._lock:
            self._states[name] = state
            if ready:
                self._ready_servers.add(name)
                if self._ready_servers >= self._enabled_names:
                    self._ready_event.set()

    def _mark_unready_servers_failed(self, exc: Exception) -> None:
        reason = str(exc) or type(exc).__name__
        with self._lock:
            unready = self._enabled_names - self._ready_servers
        for name in unready:
            self._set_state(
                name,
                ServerState(ServerStatus.ERROR, last_error=reason),
                ready=True,
            )

    def _log(self, message: str) -> None:
        try:
            self._log_fn(message)
        except Exception:
            logger.exception("MCP log callback failed")
