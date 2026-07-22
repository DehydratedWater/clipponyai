"""Shared fixtures: isolated dirs, a scriptable fake LLM client."""

from __future__ import annotations

import json

import pytest

from clipponyai.config import Config
from clipponyai.tasks import TaskStore


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep every test away from the real user config/data dirs."""
    monkeypatch.setattr("clipponyai.config.user_config_dir", lambda name: str(tmp_path / "cfg"))
    monkeypatch.setattr("clipponyai.config.user_data_dir", lambda name: str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def config():
    return Config()


class FakeClient:
    """Stands in for OpenAICompatClient. Routes on the spec's agent_id so one
    factory can script the chat model, the sensors and the slow/vision lanes
    independently. Each handler is either a static payload or a callable
    (messages, tools) -> payload; payload may be a string (content), a dict
    (JSON content), or a list of ChatToolCall-like dicts (tool calls).
    """

    def __init__(self, spec, handlers):
        self.spec = spec
        self.handlers = handlers
        self.calls = []

    def complete(self, *, messages, tools=None, model, **params):
        from open_agent_compiler.interactive.runner import ChatResponse, ChatToolCall

        # copy: the runner appends to the same list after this call returns
        self.calls.append({"messages": list(messages), "tools": tools, "model": model})
        handler = self.handlers.get(self.spec.agent_id, "ok")
        payload = handler(messages, tools) if callable(handler) else handler
        # a handler may be a list of payloads consumed call by call
        if isinstance(payload, list) and payload and isinstance(payload[0], (str, dict, tuple)):
            payload = payload.pop(0) if len(payload) > 1 else payload[0]
        if isinstance(payload, tuple):  # ("tool", name, args)
            _, name, args = payload
            return ChatResponse(
                content="", tool_calls=[ChatToolCall(id="c1", name=name, args=args)]
            )
        if isinstance(payload, dict):
            return ChatResponse(content=json.dumps(payload), tool_calls=[])
        return ChatResponse(content=str(payload), tool_calls=[])


@pytest.fixture
def make_brain(config, store):
    """Brain factory with a scriptable fake client; no network ever."""
    from clipponyai.brain import PonyBrain

    def _make(handlers=None, **config_overrides):
        for key, value in config_overrides.items():
            setattr(config, key, value)
        clients = []

        def factory(spec):
            client = FakeClient(spec, handlers or {})
            clients.append(client)
            return client

        brain = PonyBrain(config, store, client_factory=factory)
        brain._test_clients = clients
        return brain

    return _make
