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
from pydantic import BaseModel, Field, field_validator, model_validator

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
        "qwen27b-vllm": ProviderConfig(
            base_url="http://127.0.0.1:8082/v1",
            fast_model="cyankiwi/Qwen3.5-27B-AWQ-BF16-INT8",
            vision_model="cyankiwi/Qwen3.5-27B-AWQ-BF16-INT8",
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
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


class MCPServerConfig(BaseModel):
    """Connection and tool-filter settings for one MCP server."""

    type: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    tool_allow: list[str] = Field(default_factory=list)
    tool_deny: list[str] = Field(default_factory=list)
    timeout_seconds: float = 30.0

    @model_validator(mode="after")
    def _exactly_one_transport_target(self) -> "MCPServerConfig":
        if (self.command is None) == (self.url is None):
            raise ValueError("MCP server must define exactly one of 'command' or 'url'")
        return self


class MCPConfig(BaseModel):
    """Generic MCP host configuration, disabled by default."""

    enabled: bool = False
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _valid_server_names(
        cls, value: dict[str, MCPServerConfig]
    ) -> dict[str, MCPServerConfig]:
        import re

        for name in value:
            if re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None:
                raise ValueError(
                    f"MCP server name {name!r} must contain only letters, numbers, '_' or '-'"
                )
        return value


class SkillsConfig(BaseModel):
    """Agent Skills discovery settings."""

    enabled: bool = True
    dirs: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class WorkHoursConfig(BaseModel):
    """Focused work-hours boundaries for closing reminders.

    When enabled, at the end of each configured workday the scheduler emits
    a single "closing nudge" listing real pending tasks — once per workday,
    persisted via the SQLite meta table so it never fires twice.

    Quiet-hours still take precedence: if closing time falls inside quiet
    hours the closing nudge is suppressed.
    """

    enabled: bool = False
    start: str = "09:00"   # HH:MM workday start
    end: str = "17:00"     # HH:MM workday end
    weekdays: list[int] = Field(default=[0, 1, 2, 3, 4])  # Mon=0 .. Sun=6
    closing_nudge: bool = True           # list pending tasks at end of workday
    suppress_off_hours: bool = False     # silence ordinary reminders outside work hours

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("time must be HH:MM")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("time must be HH:MM (0-23, 0-59)")
        return v

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, v: list[int]) -> list[int]:
        for d in v:
            if not (0 <= d <= 6):
                raise ValueError("weekdays must be 0 (Mon) .. 6 (Sun)")
        return sorted(set(v))


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
    work_hours: WorkHoursConfig = Field(default_factory=WorkHoursConfig)


class LogWatchConfig(BaseModel):
    """Privacy-gated log awareness: disabled by default, explicit paths only.

    When enabled, the pony can tail the last N lines of the configured local
    files and answer questions about them via the FAST LLM lane.  No regex,
    no unbounded reads, no touching services or databases.
    """

    enabled: bool = False
    files: list[str] = Field(default_factory=list)  # explicit absolute paths
    max_lines_per_file: int = 200
    max_total_chars: int = 8000

    @field_validator("max_lines_per_file", "max_total_chars")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("files")
    @classmethod
    def _paths_must_be_absolute(cls, v: list[str]) -> list[str]:
        for p in v:
            if not Path(p).is_absolute():
                raise ValueError(f"log path must be absolute: {p!r}")
        return v


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


class AwarenessConfig(BaseModel):
    """Proactive focus/distraction awareness — privacy off by default.

    When both screenshot_enabled AND awareness.enabled are True, the monitor
    periodically classifies the screen via the LLM's VISION lane and can
    interrupt the user for distractions (TikTok during work hours) or
    after-hours work.  Natural-language focus_policy is editable by the user.
    """

    enabled: bool = False
    interval_seconds: int = 120
    cooldown_minutes: int = 30
    minimum_confidence: float = 0.7
    focus_policy: str = (
        "During work hours, interrupt if the user is on social media "
        "(TikTok, Instagram, Twitter/X, Reddit browsing, YouTube entertainment). "
        "After work hours, gently remind if the user appears to still be working."
    )

    @field_validator("interval_seconds")
    @classmethod
    def _interval_range(cls, v: int) -> int:
        if v < 30:
            raise ValueError("interval must be at least 30 seconds")
        if v > 3600:
            raise ValueError("interval must be at most 3600 seconds")
        return v

    @field_validator("cooldown_minutes")
    @classmethod
    def _cooldown_range(cls, v: int) -> int:
        if v < 5:
            raise ValueError("cooldown must be at least 5 minutes")
        if v > 480:
            raise ValueError("cooldown must be at most 480 minutes (8 hours)")
        return v

    @field_validator("minimum_confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


class OnboardingConfig(BaseModel):
    """First-run onboarding: collect initial context via chat (no GUI wizard)."""

    enabled: bool = True


class ProactiveQuestionsConfig(BaseModel):
    """Non-annoying proactive context-gap questions from the scheduler.

    Fires only when ALL gates pass (onboarding complete, no pending tasks,
    no due routines, no active goal with missing check-in, quiet hours clear,
    silence not active, min gap elapsed, nothing delivered this tick).
    """

    enabled: bool = True
    min_gap_hours: int = 4  # hours between batches (3..24)
    max_questions_per_batch: int = 3  # questions per message (1..5)
    silence_default_hours: int = 24  # hours after user says "don't bother me"
    require_empty_agenda: bool = True  # only ask when no pending tasks/routines/goals

    @field_validator("min_gap_hours")
    @classmethod
    def _gap_range(cls, v: int) -> int:
        if not (3 <= v <= 24):
            raise ValueError("min_gap_hours must be between 3 and 24")
        return v

    @field_validator("max_questions_per_batch")
    @classmethod
    def _batch_range(cls, v: int) -> int:
        if not (1 <= v <= 5):
            raise ValueError("max_questions_per_batch must be between 1 and 5")
        return v

    @field_validator("silence_default_hours")
    @classmethod
    def _silence_sensible(cls, v: int) -> int:
        if v < 1:
            raise ValueError("silence_default_hours must be at least 1")
        return v


class Config(BaseModel):
    ui: UIConfig = Field(default_factory=UIConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    reminders: RemindersConfig = Field(default_factory=RemindersConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    logwatch: LogWatchConfig = Field(default_factory=LogWatchConfig)
    awareness: AwarenessConfig = Field(default_factory=AwarenessConfig)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    proactive_questions: ProactiveQuestionsConfig = Field(default_factory=ProactiveQuestionsConfig)
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
