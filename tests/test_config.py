import pytest
from pydantic import ValidationError

from clipponyai.config import (
    Config,
    MCPConfig,
    MCPServerConfig,
    ObservationConfig,
    ReflectionConfig,
    config_path,
)
from clipponyai.providers import FAST, SLOW, VISION, make_preset, model_for


def test_defaults_are_private_and_sane(config):
    assert config.screenshot_enabled is False  # privacy off by default
    assert config.telegram.enabled is False
    assert config.telegram.allowed_user_ids == []  # answers nobody
    assert config.ui.character == "twilight"
    assert "openai" in config.llm.providers
    assert "ollama" in config.llm.providers  # local-GPU path out of the box
    assert config.mcp.enabled is False
    assert config.observation.enabled is False
    assert config.reflection.enabled is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval_minutes", 4),
        ("interval_minutes", 241),
        ("min_gap_minutes", 14),
        ("min_gap_minutes", 481),
        ("quiet_after_nudge_minutes", -1),
        ("quiet_after_nudge_minutes", 121),
        ("context_hours", 0),
        ("context_hours", 25),
        ("max_tool_rounds", 0),
        ("max_tool_rounds", 11),
    ],
)
def test_reflection_config_rejects_out_of_bounds(field, value):
    with pytest.raises(ValidationError, match=field):
        ReflectionConfig(**{field: value})


def test_reflection_config_accepts_bounds():
    configured = ReflectionConfig(
        interval_minutes=5,
        min_gap_minutes=480,
        quiet_after_nudge_minutes=0,
        context_hours=24,
        max_tool_rounds=10,
    )

    assert configured.interval_minutes == 5
    assert configured.min_gap_minutes == 480
    assert configured.quiet_after_nudge_minutes == 0
    assert configured.context_hours == 24
    assert configured.max_tool_rounds == 10


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_seconds", 4),
        ("sample_seconds", 301),
        ("idle_threshold_seconds", 29),
        ("idle_threshold_seconds", 3601),
        ("retention_days", 0),
        ("retention_days", 366),
        ("max_rows", 499),
        ("max_rows", 100001),
    ],
)
def test_observation_config_rejects_out_of_bounds(field, value):
    with pytest.raises(ValidationError, match=field):
        ObservationConfig(**{field: value})


def test_observation_config_accepts_bounds_and_valid_regexes():
    configured = ObservationConfig(
        sample_seconds=5,
        idle_threshold_seconds=3600,
        retention_days=365,
        max_rows=500,
        redact_patterns=[r"secret-\d+"],
    )

    assert configured.redact_patterns == [r"secret-\d+"]


def test_observation_config_rejects_invalid_regex():
    with pytest.raises(ValidationError, match="invalid redact pattern"):
        ObservationConfig(redact_patterns=["["])


def test_save_load_roundtrip(config):
    config.ui.character = "rainbow-dash"
    config.llm.active = "ollama"
    config.screenshot_enabled = True
    path = config.save()
    assert path == config_path()
    loaded = Config.load()
    assert loaded.ui.character == "rainbow-dash"
    assert loaded.llm.active == "ollama"
    assert loaded.screenshot_enabled is True


def test_load_missing_file_gives_defaults(config):
    assert Config.load().ui.character == "twilight"


def test_active_provider_unknown_raises(config):
    config.llm.active = "nonexistent"
    with pytest.raises(KeyError):
        config.llm.active_provider()


def test_model_fallbacks(config):
    groq = config.llm.providers["groq"]
    assert groq.slow_model is None
    assert model_for(groq, SLOW) == groq.fast_model
    assert model_for(groq, VISION) == groq.fast_model
    openai = config.llm.providers["openai"]
    assert model_for(openai, SLOW) == "gpt-4o"


def test_make_preset_carries_provider_options(config):
    ollama = config.llm.providers["ollama"]
    preset = make_preset("ollama", ollama, FAST)
    assert preset.provider_options["base_url"] == "http://localhost:11434/v1"
    assert "api_key_env" not in preset.provider_options  # local: no key needed
    assert preset.model_id == "qwen3:8b"

    openai_preset = make_preset("openai", config.llm.providers["openai"], VISION)
    assert openai_preset.provider_options["api_key_env"] == "OPENAI_API_KEY"
    assert openai_preset.input_modalities == ("text", "image")


def test_extra_body_serialized_as_json_string(config):
    from clipponyai.config import ProviderConfig

    cfg = ProviderConfig(
        base_url="http://x/v1",
        fast_model="m",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    preset = make_preset("vllm", cfg, FAST)
    assert isinstance(preset.provider_options["extra_body"], str)
    assert "enable_thinking" in preset.provider_options["extra_body"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"command": "server", "url": "https://example.test/mcp"},
    ],
)
def test_mcp_server_requires_exactly_one_connection_target(kwargs):
    with pytest.raises(ValidationError, match="exactly one"):
        MCPServerConfig(**kwargs)


def test_mcp_server_names_are_safe_tool_name_components():
    with pytest.raises(ValidationError, match="server name"):
        MCPConfig(
            enabled=True,
            servers={"not valid": MCPServerConfig(command="server")},
        )


def test_mcp_config_yaml_roundtrip_preserves_environment_placeholders(tmp_path):
    path = tmp_path / "mcp-config.yaml"
    config = Config(
        mcp=MCPConfig(
            enabled=True,
            servers={
                "private-data": MCPServerConfig(
                    url="https://example.test/mcp",
                    headers={"Authorization": "Bearer ${PRIVATE_DATA_TOKEN}"},
                )
            },
        )
    )

    config.save(path)
    loaded = Config.load(path)

    assert loaded.mcp.servers["private-data"].headers == {
        "Authorization": "Bearer ${PRIVATE_DATA_TOKEN}"
    }
    assert "${PRIVATE_DATA_TOKEN}" in path.read_text()
