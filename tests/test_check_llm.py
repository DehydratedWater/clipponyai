"""Unit tests for the check-llm CLI command — no network, fakes only.

Verifies that the CLI subcommand is registered, that it returns the right
exit codes for misconfiguration, and that the qwen27b-vllm provider is
present in defaults.
"""

from __future__ import annotations

from unittest.mock import patch

from clipponyai.cli import main
from clipponyai.config import Config


def test_check_llm_registered_in_help(capsys):
    """The check-llm subcommand appears in --help output."""
    try:
        main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "check-llm" in out


def test_check_llm_missing_provider_returns_1(capsys):
    """When llm.active names a nonexistent provider, check-llm returns 1."""
    Config().save()
    config = Config.load()
    config.llm.active = "does-not-exist"
    config.save()

    code = main(["check-llm"])
    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out


def test_check_llm_missing_api_key_returns_1(capsys, monkeypatch):
    """When the active provider needs a key that is not set, check-llm returns 1."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    Config().save()

    code = main(["check-llm"])
    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out


def test_check_llm_success_with_fake_client(capsys, monkeypatch):
    """When the provider is local (no key) and the client returns text, exit 0."""
    # Use the qwen27b-vllm provider (no api_key_env needed)
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.save()

    # Patch OpenAICompatClient.from_spec to return a fake that succeeds
    from open_agent_compiler.interactive.runner import ChatResponse

    class FakeClient:
        def complete(self, *, messages, tools=None, model, **params):
            return ChatResponse(content="pong", tool_calls=[])

    def fake_from_spec(spec):
        return FakeClient()

    with patch(
        "open_agent_compiler.interactive.runner.OpenAICompatClient.from_spec",
        fake_from_spec,
    ):
        code = main(["check-llm"])

    assert code == 0
    out = capsys.readouterr().out
    assert "ok" in out.lower()
    assert "pong" in out


def test_check_llm_empty_response_returns_1(capsys):
    """When the model returns empty text with no error, check-llm returns 1."""
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.save()

    from open_agent_compiler.interactive.runner import ChatResponse

    class FakeClient:
        def complete(self, *, messages, tools=None, model, **params):
            return ChatResponse(content="", tool_calls=[])

    def fake_from_spec(spec):
        return FakeClient()

    with patch(
        "open_agent_compiler.interactive.runner.OpenAICompatClient.from_spec",
        fake_from_spec,
    ):
        code = main(["check-llm"])

    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out


def test_check_llm_exception_returns_1(capsys):
    """When run_interactive raises, check-llm catches and returns 1."""
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.save()

    class FakeClient:
        def complete(self, *, messages, tools=None, model, **params):
            raise ConnectionError("refused")

    def fake_from_spec(spec):
        return FakeClient()

    with patch(
        "open_agent_compiler.interactive.runner.OpenAICompatClient.from_spec",
        fake_from_spec,
    ):
        code = main(["check-llm"])

    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out


# ── qwen27b-vllm provider defaults ────────────────────────────────────
def test_qwen27b_vllm_in_default_providers():
    config = Config()
    assert "qwen27b-vllm" in config.llm.providers
    prov = config.llm.providers["qwen27b-vllm"]
    assert prov.base_url == "http://127.0.0.1:8082/v1"
    assert prov.fast_model == "cyankiwi/Qwen3.5-27B-AWQ-BF16-INT8"
    assert prov.api_key_env is None
    assert prov.extra_body is not None
    assert prov.extra_body["chat_template_kwargs"]["enable_thinking"] is False


def test_qwen27b_vllm_no_api_key_needed():
    """The qwen27b-vllm provider has no api_key_env, so missing_api_key returns None."""
    from clipponyai.providers import missing_api_key

    config = Config()
    prov = config.llm.providers["qwen27b-vllm"]
    assert missing_api_key(prov) is None
