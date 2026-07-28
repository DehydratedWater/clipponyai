"""Pure validation and apply logic for in-app settings.

No Qt dependencies — every function here works with plain dataclasses and the
existing ``Config`` model.  The PySide6 dialog in ``settings_dialog.py`` is the
only Qt layer; it delegates all validation and persistence to this module so
the core logic is unit-testable without a display server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ProviderConfig
from .characters import CHARACTERS, FORMS


def _default_prov() -> ProviderConfig:
    return ProviderConfig()


ALL_CHARACTERS = [*CHARACTERS, *FORMS]
CHARACTER_SLUGS = [c.slug for c in ALL_CHARACTERS]


def _valid_clock(value: str) -> bool:
    """Validate a strict 24-hour HH:MM value without pattern matching."""
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        return False
    hour, minute = (int(part) for part in parts)
    return 0 <= hour <= 23 and 0 <= minute <= 59


# ── flat DTO mirroring the config tree ─────────────────────────────────

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class SettingsForm:
    """Flat representation of all editable settings.

    Populated from ``Config`` and validated before applying back.
    """

    # screenshot / privacy
    screenshot_enabled: bool = False

    # auto-commitment tracking
    auto_track_commitments: bool = True

    # pony appearance / behaviour
    character: str = "twilight"
    pony_scale: float = 1.0
    pony_idle_wander: bool = True
    pony_attention_seconds: int = 30

    # reminders
    reminders_enabled: bool = True
    reminders_check_interval: int = 60
    reminders_quiet_start: int = 23
    reminders_quiet_end: int = 8
    reminders_nudge_gaps: list[int] = field(default_factory=lambda: [30, 60, 120, 240, 360])
    reminders_max_nudges: int = 8
    reminders_batch_limit: int = 3

    # work hours
    work_hours_enabled: bool = False
    work_hours_start: str = "09:00"
    work_hours_end: str = "17:00"
    work_hours_weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    work_hours_closing_nudge: bool = True
    work_hours_suppress_off_hours: bool = False

    # logwatch
    logwatch_enabled: bool = False
    logwatch_paths: list[str] = field(default_factory=list)
    logwatch_max_lines: int = 200
    logwatch_max_chars: int = 8000

    # LLM provider
    active_provider: str = "openai"
    # editable fields for the active provider's config
    provider_base_url: str = ""
    provider_api_key_env: str = ""
    provider_fast_model: str = "gpt-4o-mini"
    provider_slow_model: str = ""
    provider_vision_model: str = ""

    # autostart
    autostart_enabled: bool = False

    # awareness (proactive focus/distraction)
    awareness_enabled: bool = False
    awareness_interval_seconds: int = 300
    awareness_cooldown_minutes: int = 30
    awareness_minimum_confidence: float = 0.7
    awareness_focus_policy: str = ""

    # continuous screen observation
    observation_enabled: bool = False
    observation_sample_seconds: int = 15
    observation_capture_window_titles: bool = True
    observation_idle_threshold_seconds: int = 180
    observation_retention_days: int = 14
    observation_max_rows: int = 20000
    observation_redact_patterns: list[str] = field(default_factory=list)

    # proactive questions (context-gap nudges from the scheduler)
    onboarding_enabled: bool = True
    proactive_questions_enabled: bool = True
    proactive_min_gap_hours: int = 4
    proactive_max_questions_per_batch: int = 3
    proactive_silence_default_hours: int = 24
    proactive_require_empty_agenda: bool = True

    # periodic reflection
    reflection_enabled: bool = True
    reflection_interval_minutes: int = 20
    reflection_min_gap_minutes: int = 60
    reflection_quiet_after_nudge_minutes: int = 10
    reflection_context_hours: int = 3


# ── read from Config ───────────────────────────────────────────────────


def read_form(config: Config, *, autostart_enabled: bool = False) -> SettingsForm:
    """Build a ``SettingsForm`` from the current ``Config``.

    ``autostart_enabled`` is not stored in Config — it is a live probe result
    passed by the caller (the dialog layer checks the platform autostart state).
    """
    rem = config.reminders
    wh = rem.work_hours
    lw = config.logwatch
    aw = config.awareness
    observation = config.observation
    reflection = config.reflection
    return SettingsForm(
        screenshot_enabled=config.screenshot_enabled,
        auto_track_commitments=config.auto_track_commitments,
        character=config.ui.character,
        pony_scale=config.ui.scale,
        pony_idle_wander=config.ui.idle_wander,
        pony_attention_seconds=config.ui.attention_seconds,
        reminders_enabled=rem.enabled,
        reminders_check_interval=rem.check_interval_seconds,
        reminders_quiet_start=rem.quiet_hours_start,
        reminders_quiet_end=rem.quiet_hours_end,
        reminders_nudge_gaps=list(rem.nudge_gaps_minutes),
        reminders_max_nudges=rem.max_nudges,
        reminders_batch_limit=rem.batch_limit,
        work_hours_enabled=wh.enabled,
        work_hours_start=wh.start,
        work_hours_end=wh.end,
        work_hours_weekdays=list(wh.weekdays),
        work_hours_closing_nudge=wh.closing_nudge,
        work_hours_suppress_off_hours=wh.suppress_off_hours,
        logwatch_enabled=lw.enabled,
        logwatch_paths=list(lw.files),
        logwatch_max_lines=lw.max_lines_per_file,
        logwatch_max_chars=lw.max_total_chars,
        active_provider=config.llm.active,
        provider_base_url=config.llm.providers.get(config.llm.active, _default_prov()).base_url
        or "",
        provider_api_key_env=config.llm.providers.get(
            config.llm.active, _default_prov()
        ).api_key_env
        or "",
        provider_fast_model=config.llm.providers.get(config.llm.active, _default_prov()).fast_model,
        provider_slow_model=config.llm.providers.get(config.llm.active, _default_prov()).slow_model
        or "",
        provider_vision_model=config.llm.providers.get(
            config.llm.active, _default_prov()
        ).vision_model
        or "",
        autostart_enabled=autostart_enabled,
        awareness_enabled=aw.enabled,
        awareness_interval_seconds=aw.interval_seconds,
        awareness_cooldown_minutes=aw.cooldown_minutes,
        awareness_minimum_confidence=aw.minimum_confidence,
        awareness_focus_policy=aw.focus_policy,
        observation_enabled=observation.enabled,
        observation_sample_seconds=observation.sample_seconds,
        observation_capture_window_titles=observation.capture_window_titles,
        observation_idle_threshold_seconds=observation.idle_threshold_seconds,
        observation_retention_days=observation.retention_days,
        observation_max_rows=observation.max_rows,
        observation_redact_patterns=list(observation.redact_patterns),
        onboarding_enabled=config.onboarding.enabled,
        proactive_questions_enabled=config.proactive_questions.enabled,
        proactive_min_gap_hours=config.proactive_questions.min_gap_hours,
        proactive_max_questions_per_batch=config.proactive_questions.max_questions_per_batch,
        proactive_silence_default_hours=config.proactive_questions.silence_default_hours,
        proactive_require_empty_agenda=config.proactive_questions.require_empty_agenda,
        reflection_enabled=reflection.enabled,
        reflection_interval_minutes=reflection.interval_minutes,
        reflection_min_gap_minutes=reflection.min_gap_minutes,
        reflection_quiet_after_nudge_minutes=reflection.quiet_after_nudge_minutes,
        reflection_context_hours=reflection.context_hours,
    )


# ── validation ─────────────────────────────────────────────────────────


class ValidationError(Exception):
    """Raised when the form fails validation. ``errors`` is a list of messages."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate(
    form: SettingsForm,
    *,
    available_providers: list[str],
    available_characters: list[str] | None = None,
) -> list[str]:
    """Return a list of human-readable error strings (empty = valid).

    Validates ranges, formats and cross-field constraints.  Does NOT modify
    the form.
    """
    errors: list[str] = []
    ch = available_characters or CHARACTER_SLUGS

    # pony scale
    if form.pony_scale < 0.3 or form.pony_scale > 3.0:
        errors.append("Pony scale must be between 0.3 and 3.0")

    # attention seconds
    if form.pony_attention_seconds < 5 or form.pony_attention_seconds > 300:
        errors.append("Attention duration must be 5–300 seconds")

    # reminder intervals
    if form.reminders_check_interval < 10 or form.reminders_check_interval > 3600:
        errors.append("Check interval must be 10–3600 seconds")
    if form.reminders_max_nudges < 1:
        errors.append("Max nudges must be at least 1")
    if form.reminders_batch_limit < 1 or form.reminders_batch_limit > 20:
        errors.append("Batch limit must be 1–20")

    # quiet hours
    if not (0 <= form.reminders_quiet_start <= 23):
        errors.append("Quiet start hour must be 0–23")
    if not (0 <= form.reminders_quiet_end <= 23):
        errors.append("Quiet end hour must be 0–23")

    # nudge gaps
    if not form.reminders_nudge_gaps:
        errors.append("Need at least one nudge gap")
    else:
        for gap in form.reminders_nudge_gaps:
            if gap < 1:
                errors.append("Each nudge gap must be at least 1 minute")
                break

    # Work-hour syntax is mechanical configuration validation; natural-language
    # time understanding remains a small LLM sensor in PonyBrain.
    if not _valid_clock(form.work_hours_start):
        errors.append("Work start must be a valid 24-hour HH:MM time")
    if not _valid_clock(form.work_hours_end):
        errors.append("Work end must be a valid 24-hour HH:MM time")

    # weekdays
    for d in form.work_hours_weekdays:
        if not (0 <= d <= 6):
            errors.append("Weekday index must be 0 (Mon) – 6 (Sun)")
            break

    # logwatch
    for p in form.logwatch_paths:
        if not p:
            errors.append("Log path must not be blank")
            break
        expanded = str(Path(p).expanduser())
        if not Path(expanded).is_absolute():
            errors.append(f"Log path must be absolute: {p!r}")
            break
    if form.logwatch_max_lines < 1:
        errors.append("Max lines per file must be >= 1")
    if form.logwatch_max_chars < 100:
        errors.append("Max total chars must be >= 100")

    # provider
    if form.active_provider and form.active_provider not in available_providers:
        errors.append(f"Provider {form.active_provider!r} is not configured")
    if not form.provider_fast_model.strip():
        errors.append("Fast model must not be blank")

    # character
    if form.character and form.character not in ch:
        errors.append(f"Character {form.character!r} is not available")

    # awareness
    if form.awareness_interval_seconds < 30:
        errors.append("Awareness interval must be at least 30 seconds")
    if form.awareness_interval_seconds > 3600:
        errors.append("Awareness interval must be at most 3600 seconds")
    if form.awareness_cooldown_minutes < 5:
        errors.append("Awareness cooldown must be at least 5 minutes")
    if form.awareness_cooldown_minutes > 480:
        errors.append("Awareness cooldown must be at most 480 minutes")
    if not (0.0 <= form.awareness_minimum_confidence <= 1.0):
        errors.append("Awareness confidence must be between 0.0 and 1.0")

    # screen observation
    if not (5 <= form.observation_sample_seconds <= 300):
        errors.append("Screen observation sample interval must be 5–300 seconds")
    if not (30 <= form.observation_idle_threshold_seconds <= 3600):
        errors.append("Screen observation idle threshold must be 30–3600 seconds")
    if not (1 <= form.observation_retention_days <= 365):
        errors.append("Screen observation retention must be 1–365 days")
    if not (500 <= form.observation_max_rows <= 100000):
        errors.append("Screen observation max rows must be 500–100000")
    for pattern in form.observation_redact_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"Screen observation redact pattern {pattern!r} is invalid: {exc}")
            break

    # proactive questions
    if not (3 <= form.proactive_min_gap_hours <= 24):
        errors.append("Proactive questions gap must be 3–24 hours")
    if not (1 <= form.proactive_max_questions_per_batch <= 5):
        errors.append("Proactive questions per batch must be 1–5")
    if not (1 <= form.proactive_silence_default_hours <= 168):
        errors.append("Proactive silence duration must be 1–168 hours")

    # reflection
    if not (5 <= form.reflection_interval_minutes <= 240):
        errors.append("Reflection interval must be 5–240 minutes")
    if not (15 <= form.reflection_min_gap_minutes <= 480):
        errors.append("Reflection minimum gap must be 15–480 minutes")
    if not (0 <= form.reflection_quiet_after_nudge_minutes <= 120):
        errors.append("Reflection quiet after nudge must be 0–120 minutes")
    if not (1 <= form.reflection_context_hours <= 24):
        errors.append("Reflection context must be 1–24 hours")

    return errors


# ── apply back to Config ───────────────────────────────────────────────


def apply_to_config(form: SettingsForm, config: Config) -> None:
    """Mutate ``config`` in place from a validated ``SettingsForm``.

    Does NOT save to disk — caller decides when to persist.
    """
    config.screenshot_enabled = form.screenshot_enabled
    config.auto_track_commitments = form.auto_track_commitments

    config.ui.scale = form.pony_scale
    config.ui.idle_wander = form.pony_idle_wander
    config.ui.attention_seconds = form.pony_attention_seconds
    config.ui.character = form.character

    rem = config.reminders
    rem.enabled = form.reminders_enabled
    rem.check_interval_seconds = form.reminders_check_interval
    rem.quiet_hours_start = form.reminders_quiet_start
    rem.quiet_hours_end = form.reminders_quiet_end
    rem.nudge_gaps_minutes = form.reminders_nudge_gaps
    rem.max_nudges = form.reminders_max_nudges
    rem.batch_limit = form.reminders_batch_limit

    wh = rem.work_hours
    wh.enabled = form.work_hours_enabled
    wh.start = form.work_hours_start
    wh.end = form.work_hours_end
    wh.weekdays = sorted(set(form.work_hours_weekdays))
    wh.closing_nudge = form.work_hours_closing_nudge
    wh.suppress_off_hours = form.work_hours_suppress_off_hours

    lw = config.logwatch
    lw.enabled = form.logwatch_enabled
    lw.files = [str(Path(p).expanduser()) for p in form.logwatch_paths]
    lw.max_lines_per_file = form.logwatch_max_lines
    lw.max_total_chars = form.logwatch_max_chars

    config.llm.active = form.active_provider
    # edit the active provider's config fields in-place
    prov = config.llm.providers.get(form.active_provider)
    if prov is not None:
        prov.base_url = form.provider_base_url.strip() or None
        prov.api_key_env = form.provider_api_key_env.strip() or None
        prov.fast_model = form.provider_fast_model.strip()
        prov.slow_model = form.provider_slow_model.strip() or None
        prov.vision_model = form.provider_vision_model.strip() or None

    aw = config.awareness
    aw.enabled = form.awareness_enabled
    aw.interval_seconds = form.awareness_interval_seconds
    aw.cooldown_minutes = form.awareness_cooldown_minutes
    aw.minimum_confidence = form.awareness_minimum_confidence
    aw.focus_policy = form.awareness_focus_policy

    observation = config.observation
    observation.enabled = form.observation_enabled
    observation.sample_seconds = form.observation_sample_seconds
    observation.capture_window_titles = form.observation_capture_window_titles
    observation.idle_threshold_seconds = form.observation_idle_threshold_seconds
    observation.retention_days = form.observation_retention_days
    observation.max_rows = form.observation_max_rows
    observation.redact_patterns = list(form.observation_redact_patterns)

    config.onboarding.enabled = form.onboarding_enabled

    pq = config.proactive_questions
    pq.enabled = form.proactive_questions_enabled
    pq.min_gap_hours = form.proactive_min_gap_hours
    pq.max_questions_per_batch = form.proactive_max_questions_per_batch
    pq.silence_default_hours = form.proactive_silence_default_hours
    pq.require_empty_agenda = form.proactive_require_empty_agenda

    reflection = config.reflection
    reflection.enabled = form.reflection_enabled
    reflection.interval_minutes = form.reflection_interval_minutes
    reflection.min_gap_minutes = form.reflection_min_gap_minutes
    reflection.quiet_after_nudge_minutes = form.reflection_quiet_after_nudge_minutes
    reflection.context_hours = form.reflection_context_hours


def detect_changes(old: SettingsForm, new: SettingsForm) -> dict[str, bool]:
    """Return a dict of field_name -> True for every field that differs."""
    changes: dict[str, bool] = {}
    for fld in old.__dataclass_fields__:
        old_val = getattr(old, fld)
        new_val = getattr(new, fld)
        if old_val != new_val:
            changes[fld] = True
    return changes


def needs_restart(changes: dict[str, bool]) -> list[str]:
    """Given a set of changed field names, return reasons why a restart is needed.

    Most settings can be applied live, but some require a restart because they
    affect subsystems that are initialised at startup.
    """
    reasons: list[str] = []
    if changes.get("active_provider"):
        reasons.append(
            "LLM provider switch takes full effect after restart "
            "(current conversation keeps the old model until you restart)."
        )
    return reasons
