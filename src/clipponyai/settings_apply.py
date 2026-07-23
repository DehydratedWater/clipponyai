"""Pure validation and apply logic for in-app settings.

No Qt dependencies — every function here works with plain dataclasses and the
existing ``Config`` model.  The PySide6 dialog in ``settings_dialog.py`` is the
only Qt layer; it delegates all validation and persistence to this module so
the core logic is unit-testable without a display server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .characters import CHARACTERS, FORMS

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

    # autostart
    autostart_enabled: bool = False


# ── read from Config ───────────────────────────────────────────────────

def read_form(config: Config, *, autostart_enabled: bool = False) -> SettingsForm:
    """Build a ``SettingsForm`` from the current ``Config``.

    ``autostart_enabled`` is not stored in Config — it is a live probe result
    passed by the caller (the dialog layer checks the platform autostart state).
    """
    rem = config.reminders
    wh = rem.work_hours
    lw = config.logwatch
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
        autostart_enabled=autostart_enabled,
    )


# ── validation ─────────────────────────────────────────────────────────

class ValidationError(Exception):
    """Raised when the form fails validation. ``errors`` is a list of messages."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate(form: SettingsForm, *, available_providers: list[str],
             available_characters: list[str] | None = None) -> list[str]:
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
        if p and not Path(p).expanduser().is_absolute():
            errors.append(f"Log path must be absolute: {p!r}")
            break
    if form.logwatch_max_lines < 1:
        errors.append("Max lines per file must be >= 1")
    if form.logwatch_max_chars < 100:
        errors.append("Max total chars must be >= 100")

    # provider
    if form.active_provider and form.active_provider not in available_providers:
        errors.append(f"Provider {form.active_provider!r} is not configured")

    # character
    if form.character and form.character not in ch:
        errors.append(f"Character {form.character!r} is not available")

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
    lw.files = list(form.logwatch_paths)
    lw.max_lines_per_file = form.logwatch_max_lines
    lw.max_total_chars = form.logwatch_max_chars

    config.llm.active = form.active_provider


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
    if changes.get("character"):
        reasons.append(
            "Character switch takes full effect after restart "
            "(the pony's sprites and personality reload cleanly)."
        )
    return reasons
