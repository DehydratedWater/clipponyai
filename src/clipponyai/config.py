"""Configuration: one YAML file, sensible defaults, privacy off by default.

Config lives at ``~/.config/clipponyai/config.yaml`` (platform-appropriate via
platformdirs); sprites and the task database live in the data dir
(``~/.local/share/clipponyai`` on Linux). ``clipponyai init`` writes a
commented default config you can edit.

API keys are NEVER stored in the config file — each provider names an
environment variable (``api_key_env``) that holds the key.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field

APP_NAME = "clipponyai"


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.yaml"


def sprites_dir() -> Path:
    return data_dir() / "sprites"


def db_path() -> Path:
    return data_dir() / "clippony.db"


class ProviderConfig(BaseModel):
    """One LLM provider = an OpenAI-compatible endpoint + model names.

    Anything that speaks the OpenAI chat API works: OpenAI itself, Anthropic
    (their OpenAI-compat endpoint), OpenRouter, Groq, a local Ollama, vLLM,
    llama.cpp server, … Set ``base_url`` for non-OpenAI endpoints.
    """

    base_url: str | None = None  # None = api.openai.com
    api_key_env: str | None = None  # env var holding the API key
    fast_model: str = "gpt-4o-mini"  # snappy chat turns
    slow_model: str | None = None  # deep thinking; defaults to fast_model
    vision_model: str | None = None  # screenshots; defaults to slow_model
    temperature: float = 0.7
    # provider quirks, e.g. {"chat_template_kwargs": {"enable_thinking": false}}
    # for local Qwen "thinking" models on vLLM that otherwise return empty text
    extra_body: dict | None = None

    def resolved_slow_model(self) -> str:
        return self.slow_model or self.fast_model

    def resolved_vision_model(self) -> str:
        return self.vision_model or self.resolved_slow_model()


def default_providers() -> dict[str, ProviderConfig]:
    return {
        "openai": ProviderConfig(
            api_key_env="OPENAI_API_KEY",
            fast_model="gpt-4o-mini",
            slow_model="gpt-4o",
            vision_model="gpt-4o",
        ),
        "anthropic": ProviderConfig(
            base_url="https://api.anthropic.com/v1/",
            api_key_env="ANTHROPIC_API_KEY",
            fast_model="claude-haiku-4-5-20251001",
            slow_model="claude-sonnet-5",
            vision_model="claude-sonnet-5",
        ),
        "openrouter": ProviderConfig(
            base_url="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
            fast_model="openai/gpt-4o-mini",
            slow_model="anthropic/claude-sonnet-4.5",
            vision_model="openai/gpt-4o",
        ),
        "groq": ProviderConfig(
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
            fast_model="llama-3.3-70b-versatile",
        ),
        "ollama": ProviderConfig(
            base_url="http://localhost:11434/v1",
            fast_model="qwen3:8b",
            slow_model="qwen3:32b",
            vision_model="qwen2.5vl:7b",
        ),
    }


class LLMConfig(BaseModel):
    active: str = "openai"  # which provider entry to use
    history_limit: int = 40  # messages of context kept per turn
    max_tool_rounds: int = 6
    providers: dict[str, ProviderConfig] = Field(default_factory=default_providers)

    def active_provider(self) -> ProviderConfig:
        if self.active not in self.providers:
            raise KeyError(
                f"llm.active={self.active!r} is not one of the configured "
                f"providers: {sorted(self.providers)}"
            )
        return self.providers[self.active]


class RemindersConfig(BaseModel):
    enabled: bool = True
    check_interval_seconds: int = 60
    # local wall-clock hours during which the pony stays quiet
    quiet_hours_start: int = 23
    quiet_hours_end: int = 8
    # minutes to wait after the nth nudge before the next one (escalating);
    # the last gap repeats until max_nudges is reached
    nudge_gaps_minutes: list[int] = Field(default=[30, 60, 120, 240, 360])
    max_nudges: int = 8  # then the task is dropped with a notice
    batch_limit: int = 3  # max tasks mentioned in one nudge message


class TelegramConfig(BaseModel):
    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    # empty allowlist = bot answers nobody (safe default); add your numeric
    # Telegram user id(s) to talk to the pony remotely
    allowed_user_ids: list[int] = Field(default_factory=list)


class UIConfig(BaseModel):
    character: str = "twilight"
    scale: float = 1.0
    idle_wander: bool = True
    attention_seconds: int = 30  # how long a reminder chases the cursor


class Config(BaseModel):
    ui: UIConfig = Field(default_factory=UIConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    reminders: RemindersConfig = Field(default_factory=RemindersConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    # privacy: the pony can only look at your screen when you turn this on
    screenshot_enabled: bool = False
    # scan your messages for passing promises ("I'll call mom later") and
    # track them automatically (one extra cheap fast-model call per message)
    auto_track_commitments: bool = True

    # ── persistence ──────────────────────────────────────────────────
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.model_dump(), sort_keys=False, allow_unicode=True))
        return path
