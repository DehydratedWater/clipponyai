"""Token usage capture layer — sits between PonyBrain and the LLM provider.

Wraps the ChatClient produced by the client_factory (production:
OpenAICompatClient, tests: FakeClient) and records every completion's usage
into a TokenUsageStore.  When the API response omits usage (some local
servers), estimates prompt/completion from serialized message lengths and
marks estimated=True.

FakeClient tests remain untouched — they simply do not carry the callback.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

log = logging.getLogger("clipponyai.token_capture")

# Rough chars-per-token for estimation (English-average, conservative)
_CHARS_PER_TOKEN = 4

# Callback signature:
#   (lane, purpose, provider, model, prompt_tokens, completion_tokens, estimated)
TokenCallback = Callable[
    [str, str, str, str, int, int, bool],
    None,
]


# ── estimation helpers ────────────────────────────────────────────────


def estimate_prompt_tokens(messages: list[dict], tools: list[dict] | None) -> int:
    """Estimate prompt tokens from serialized messages + tool definitions.

    Conservative estimate: total character count / 4.
    """
    total_chars = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(role) + len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total_chars += len(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        total_chars += 50  # flat cost for image metadata
                    else:
                        total_chars += len(json.dumps(part, default=str))
        else:
            total_chars += len(str(content))
        # tool_calls in assistant messages also count as input context
        for tc in msg.get("tool_calls", []):
            if isinstance(tc, dict):
                total_chars += len(json.dumps(tc.get("function", {}), default=str))

    if tools:
        total_chars += len(json.dumps(tools, default=str))

    return max(1, total_chars // _CHARS_PER_TOKEN)


def _try_get_raw_usage(inner: Any) -> tuple[int, int] | None:
    """Try to extract (prompt_tokens, completion_tokens) from the inner client.

    RawResponseOpenAICompatClient stores the raw openai.ChatCompletion on
    _last_response.  FakeClient and other test clients won't have it, so
    we return None and fall through to estimation.
    """
    raw = getattr(inner, "_last_response", None)
    if raw is None:
        return None
    usage = getattr(raw, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    if prompt == 0 and completion == 0:
        return None
    return (prompt, completion)


# ── wrapper client ────────────────────────────────────────────────────


class TokenCaptureClient:
    """Wraps any ChatClient and records token usage via a callback.

    Does not alter the ChatResponse returned to the caller.
    """

    def __init__(
        self,
        inner: Any,
        callback: TokenCallback,
        *,
        lane: str = "chat",
        purpose: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self._inner = inner
        self._callback = callback
        self._lane = lane
        self._purpose = purpose
        self._provider = provider
        self._model = model
        # Forward attributes so FakeClient tests still work
        self.calls = getattr(inner, "calls", [])
        self.handlers = getattr(inner, "handlers", {})
        self.spec = getattr(inner, "spec", None)

    def complete(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        **params: Any,
    ) -> Any:
        response = self._inner.complete(
            messages=messages, tools=tools, model=model, **params
        )
        self._record(messages, tools, response)
        return response

    def _record(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        response: Any,
    ) -> None:
        raw_usage = _try_get_raw_usage(self._inner)
        if raw_usage is not None:
            prompt_tokens, completion_tokens = raw_usage
            estimated = False
        else:
            prompt_tokens = estimate_prompt_tokens(messages, tools)
            completion_text = getattr(response, "content", "") or ""
            completion_chars = len(completion_text)
            completion_tokens = (
                max(1, completion_chars // _CHARS_PER_TOKEN)
                if completion_chars
                else 0
            )
            estimated = True

        try:
            self._callback(
                self._lane,
                self._purpose,
                self._provider,
                self._model,
                prompt_tokens,
                completion_tokens,
                estimated,
            )
        except Exception:
            log.exception("token usage callback failed")


# ── production client that captures raw API responses ─────────────────


class RawResponseOpenAICompatClient:
    """Like OpenAICompatClient but stores the raw API response on ``_last_response``.

    The standard OpenAICompatClient.complete() returns a ChatResponse (pydantic
    model) and discards the raw openai.ChatCompletion.  This variant keeps it
    so the TokenCaptureClient wrapper can extract exact usage numbers.

    Reuses the same lazy client construction and parameter merging as the
    original -- only the response handling differs.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str = "not-needed",
        default_params: dict[str, Any] | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self.default_params = dict(default_params or {})
        self._client: Any = None
        self._last_response: Any = None

    @classmethod
    def from_spec(
        cls, spec: Any, *, api_key: str | None = None
    ) -> "RawResponseOpenAICompatClient":
        """Build from an InteractiveAgentSpec (same contract as OpenAICompatClient)."""
        from open_agent_compiler.interactive.runner import _resolve_api_key

        params: dict[str, Any] = {}
        if spec.temperature is not None:
            params["temperature"] = spec.temperature
        extra = spec.model.provider_options.get("extra_body")
        if extra:
            params["extra_body"] = (
                json.loads(extra) if isinstance(extra, str) else extra
            )
        return cls(
            base_url=spec.base_url,
            api_key=_resolve_api_key(spec, api_key),
            default_params=params,
        )

    def _ensure_client(self) -> Any:
        if self._client is None:
            openai = __import__("openai", fromlist=["_"])
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def complete(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str,
        **params: Any,
    ) -> Any:
        from open_agent_compiler.interactive.runner import (
            ChatResponse,
            ChatToolCall,
            _parse_tool_args,
        )

        client = self._ensure_client()
        kwargs = {**self.default_params, **params}
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if tools:
            kwargs["tools"] = tools
        raw = client.chat.completions.create(
            model=model, messages=messages, **kwargs
        )
        self._last_response = raw  # capture for token accounting
        message = raw.choices[0].message
        calls = [
            ChatToolCall(
                id=tc.id or "",
                name=tc.function.name,
                args=_parse_tool_args(tc.function.arguments),
            )
            for tc in (message.tool_calls or [])
        ]
        return ChatResponse(content=message.content or "", tool_calls=calls)


# ── lane/purpose mapping helpers ──────────────────────────────────────


def lane_from_agent_id(agent_id: str) -> str:
    """Map an open-agent-compiler agent_id to a token-accounting lane."""
    if agent_id == "pony":
        return "chat"
    if agent_id == "pony-slow":
        return "slow"
    if agent_id == "pony-vision":
        return "vision"
    if "sensor" in agent_id or "analyst" in agent_id:
        return "sensor"
    return "other"


def purpose_from_agent_id(agent_id: str) -> str:
    """Map an agent_id to a human-readable purpose string."""
    purpose_map = {
        "pony": "chat",
        "pony-slow": "deep_think",
        "pony-vision": "look_at_screen",
        "message-sensor": "message-sensor",
        "when-sensor": "when-sensor",
        "log-analyst": "log-analyst",
    }
    return purpose_map.get(agent_id, agent_id)
