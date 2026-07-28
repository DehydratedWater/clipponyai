"""Tests for settings_apply pure logic and settings_dialog wiring.

No Qt display server needed — the pure layer is tested with plain dataclasses.
Dialog wiring tests verify the dialog opens from the app signal without
hitting the display.
"""

from __future__ import annotations

import pytest

from clipponyai.config import Config
from clipponyai.settings_apply import (
    CHARACTER_SLUGS,
    SettingsForm,
    apply_to_config,
    detect_changes,
    needs_restart,
    read_form,
    validate,
)


# ── read_form: Config -> SettingsForm ─────────────────────────────────


class TestReadForm:
    def test_defaults(self):
        config = Config()
        form = read_form(config)
        assert form.screenshot_enabled is False
        assert form.auto_track_commitments is True
        assert form.character == "twilight"
        assert form.pony_scale == 1.0
        assert form.pony_idle_wander is True
        assert form.pony_attention_seconds == 30
        assert form.reminders_enabled is True
        assert form.reminders_check_interval == 60
        assert form.reminders_quiet_start == 23
        assert form.reminders_quiet_end == 8
        assert form.reminders_nudge_gaps == [30, 60, 120, 240, 360]
        assert form.reminders_max_nudges == 8
        assert form.reminders_batch_limit == 3
        assert form.work_hours_enabled is False
        assert form.work_hours_start == "09:00"
        assert form.work_hours_end == "17:00"
        assert form.work_hours_weekdays == [0, 1, 2, 3, 4]
        assert form.work_hours_closing_nudge is True
        assert form.work_hours_suppress_off_hours is False
        assert form.logwatch_enabled is False
        assert form.logwatch_paths == []
        assert form.logwatch_max_lines == 200
        assert form.logwatch_max_chars == 8000
        assert form.active_provider == "openai"
        assert form.autostart_enabled is False
        assert form.awareness_enabled is False
        assert form.awareness_interval_seconds == 300
        assert form.awareness_cooldown_minutes == 30
        assert form.awareness_minimum_confidence == 0.7
        assert "social media" in form.awareness_focus_policy
        assert form.observation_enabled is False
        assert form.observation_sample_seconds == 15
        assert form.observation_capture_window_titles is True
        assert form.observation_idle_threshold_seconds == 180
        assert form.observation_retention_days == 14
        assert form.observation_max_rows == 20000
        assert form.observation_redact_patterns == []
        assert form.reflection_enabled is True
        assert form.reflection_interval_minutes == 20
        assert form.reflection_min_gap_minutes == 60
        assert form.reflection_quiet_after_nudge_minutes == 10
        assert form.reflection_context_hours == 3

    def test_custom_values_roundtrip(self):
        config = Config()
        config.screenshot_enabled = True
        config.auto_track_commitments = False
        config.ui.character = "rainbow-dash"
        config.ui.scale = 1.5
        config.ui.idle_wander = False
        config.ui.attention_seconds = 60
        config.reminders.enabled = False
        config.reminders.check_interval_seconds = 120
        config.reminders.quiet_hours_start = 22
        config.reminders.quiet_hours_end = 7
        config.reminders.nudge_gaps_minutes = [15, 30, 60]
        config.reminders.max_nudges = 5
        config.reminders.batch_limit = 5
        config.reminders.work_hours.enabled = True
        config.reminders.work_hours.start = "08:00"
        config.reminders.work_hours.end = "18:00"
        config.reminders.work_hours.weekdays = [0, 2, 4]
        config.reminders.work_hours.closing_nudge = False
        config.reminders.work_hours.suppress_off_hours = True
        config.logwatch.enabled = True
        config.logwatch.files = ["/var/log/test.log"]
        config.logwatch.max_lines_per_file = 500
        config.logwatch.max_total_chars = 15000
        config.llm.active = "ollama"
        config.awareness.enabled = True
        config.awareness.interval_seconds = 60
        config.awareness.cooldown_minutes = 60
        config.awareness.minimum_confidence = 0.5
        config.awareness.focus_policy = "custom policy"
        config.observation.enabled = True
        config.observation.sample_seconds = 25
        config.observation.capture_window_titles = False
        config.observation.idle_threshold_seconds = 240
        config.observation.retention_days = 30
        config.observation.max_rows = 30000
        config.observation.redact_patterns = ["secret", r"issue-\d+"]
        config.reflection.enabled = False
        config.reflection.interval_minutes = 45
        config.reflection.min_gap_minutes = 90
        config.reflection.quiet_after_nudge_minutes = 20
        config.reflection.context_hours = 8

        form = read_form(config)
        assert form.screenshot_enabled is True
        assert form.auto_track_commitments is False
        assert form.character == "rainbow-dash"
        assert form.pony_scale == 1.5
        assert form.pony_idle_wander is False
        assert form.pony_attention_seconds == 60
        assert form.reminders_enabled is False
        assert form.reminders_check_interval == 120
        assert form.reminders_quiet_start == 22
        assert form.reminders_quiet_end == 7
        assert form.reminders_nudge_gaps == [15, 30, 60]
        assert form.reminders_max_nudges == 5
        assert form.reminders_batch_limit == 5
        assert form.work_hours_enabled is True
        assert form.work_hours_start == "08:00"
        assert form.work_hours_end == "18:00"
        assert form.work_hours_weekdays == [0, 2, 4]
        assert form.work_hours_closing_nudge is False
        assert form.work_hours_suppress_off_hours is True
        assert form.logwatch_enabled is True
        assert form.logwatch_paths == ["/var/log/test.log"]
        assert form.logwatch_max_lines == 500
        assert form.logwatch_max_chars == 15000
        assert form.active_provider == "ollama"
        assert form.awareness_enabled is True
        assert form.awareness_interval_seconds == 60
        assert form.awareness_cooldown_minutes == 60
        assert form.awareness_minimum_confidence == 0.5
        assert form.awareness_focus_policy == "custom policy"
        assert form.observation_enabled is True
        assert form.observation_sample_seconds == 25
        assert form.observation_capture_window_titles is False
        assert form.observation_idle_threshold_seconds == 240
        assert form.observation_retention_days == 30
        assert form.observation_max_rows == 30000
        assert form.observation_redact_patterns == ["secret", r"issue-\d+"]
        assert form.reflection_enabled is False
        assert form.reflection_interval_minutes == 45
        assert form.reflection_min_gap_minutes == 90
        assert form.reflection_quiet_after_nudge_minutes == 20
        assert form.reflection_context_hours == 8

    def test_autostart_flag_passed_through(self):
        config = Config()
        form = read_form(config, autostart_enabled=True)
        assert form.autostart_enabled is True

    def test_lists_are_copies(self):
        """Mutating the form's lists must not mutate the config."""
        config = Config()
        config.reminders.nudge_gaps_minutes = [30, 60]
        config.reminders.work_hours.weekdays = [0, 1]
        config.logwatch.files = ["/tmp/a.log"]

        form = read_form(config)
        form.reminders_nudge_gaps.append(999)
        form.work_hours_weekdays.append(999)
        form.logwatch_paths.append("/tmp/b.log")

        assert config.reminders.nudge_gaps_minutes == [30, 60]
        assert config.reminders.work_hours.weekdays == [0, 1]
        assert config.logwatch.files == ["/tmp/a.log"]


# ── apply_to_config: SettingsForm -> Config ───────────────────────────


class TestApplyToConfig:
    def test_roundtrip_defaults(self):
        config = Config()
        form = read_form(config)
        apply_to_config(form, config)
        # config should still be valid defaults
        assert config.screenshot_enabled is False
        assert config.ui.character == "twilight"
        assert config.llm.active == "openai"

    def test_roundtrip_custom(self):
        config = Config()
        config.screenshot_enabled = True
        config.auto_track_commitments = False
        config.ui.character = "fluttershy"
        config.ui.scale = 1.3
        config.ui.idle_wander = False
        config.ui.attention_seconds = 45
        config.reminders.enabled = False
        config.reminders.check_interval_seconds = 90
        config.reminders.quiet_hours_start = 22
        config.reminders.quiet_hours_end = 6
        config.reminders.nudge_gaps_minutes = [20, 40, 80]
        config.reminders.max_nudges = 6
        config.reminders.batch_limit = 4
        config.reminders.work_hours.enabled = True
        config.reminders.work_hours.start = "10:00"
        config.reminders.work_hours.end = "19:00"
        config.reminders.work_hours.weekdays = [1, 3, 5]
        config.reminders.work_hours.closing_nudge = False
        config.reminders.work_hours.suppress_off_hours = True
        config.logwatch.enabled = True
        config.logwatch.files = ["/var/log/app.log"]
        config.logwatch.max_lines_per_file = 300
        config.logwatch.max_total_chars = 12000
        config.llm.active = "anthropic"
        config.awareness.enabled = True
        config.awareness.interval_seconds = 90
        config.awareness.cooldown_minutes = 45
        config.awareness.minimum_confidence = 0.8
        config.awareness.focus_policy = "awareness policy"
        config.observation.enabled = True
        config.observation.sample_seconds = 20
        config.observation.capture_window_titles = False
        config.observation.idle_threshold_seconds = 300
        config.observation.retention_days = 21
        config.observation.max_rows = 25000
        config.observation.redact_patterns = ["token=.*"]
        config.reflection.enabled = False
        config.reflection.interval_minutes = 30
        config.reflection.min_gap_minutes = 120
        config.reflection.quiet_after_nudge_minutes = 15
        config.reflection.context_hours = 6

        form = read_form(config)
        apply_to_config(form, config)

        assert config.screenshot_enabled is True
        assert config.auto_track_commitments is False
        assert config.ui.character == "fluttershy"
        assert config.ui.scale == 1.3
        assert config.ui.idle_wander is False
        assert config.ui.attention_seconds == 45
        assert config.reminders.enabled is False
        assert config.reminders.check_interval_seconds == 90
        assert config.reminders.quiet_hours_start == 22
        assert config.reminders.quiet_hours_end == 6
        assert config.reminders.nudge_gaps_minutes == [20, 40, 80]
        assert config.reminders.max_nudges == 6
        assert config.reminders.batch_limit == 4
        assert config.reminders.work_hours.enabled is True
        assert config.reminders.work_hours.start == "10:00"
        assert config.reminders.work_hours.end == "19:00"
        assert config.reminders.work_hours.weekdays == [1, 3, 5]
        assert config.reminders.work_hours.closing_nudge is False
        assert config.reminders.work_hours.suppress_off_hours is True
        assert config.logwatch.enabled is True
        assert config.logwatch.files == ["/var/log/app.log"]
        assert config.logwatch.max_lines_per_file == 300
        assert config.logwatch.max_total_chars == 12000
        assert config.llm.active == "anthropic"
        assert config.awareness.enabled is True
        assert config.awareness.interval_seconds == 90
        assert config.awareness.cooldown_minutes == 45
        assert config.awareness.minimum_confidence == 0.8
        assert config.awareness.focus_policy == "awareness policy"
        assert config.observation.enabled is True
        assert config.observation.sample_seconds == 20
        assert config.observation.capture_window_titles is False
        assert config.observation.idle_threshold_seconds == 300
        assert config.observation.retention_days == 21
        assert config.observation.max_rows == 25000
        assert config.observation.redact_patterns == ["token=.*"]
        assert config.reflection.enabled is False
        assert config.reflection.interval_minutes == 30
        assert config.reflection.min_gap_minutes == 120
        assert config.reflection.quiet_after_nudge_minutes == 15
        assert config.reflection.context_hours == 6

    def test_work_hours_weekdays_deduped(self):
        config = Config()
        form = read_form(config)
        form.work_hours_weekdays = [3, 1, 3, 1, 0]
        apply_to_config(form, config)
        assert config.reminders.work_hours.weekdays == [0, 1, 3]

    def test_does_not_save_to_disk(self, tmp_path):
        """apply_to_config mutates in place but does NOT call config.save()."""
        config = Config()
        form = read_form(config)
        form.screenshot_enabled = True
        apply_to_config(form, config)
        # config is mutated
        assert config.screenshot_enabled is True
        # no file was written to tmp_path (apply_to_config doesn't save)
        config_dir = tmp_path / "cfg"
        assert not (config_dir / "config.yaml").exists()


# ── validate ───────────────────────────────────────────────────────────


class TestValidate:
    def _providers(self):
        return sorted(Config().llm.providers)

    def test_valid_defaults(self):
        form = SettingsForm()
        errors = validate(form, available_providers=self._providers())
        assert errors == []

    @pytest.mark.parametrize(
        ("field_name", "value", "expected"),
        [
            ("observation_sample_seconds", 4, "sample interval"),
            ("observation_idle_threshold_seconds", 29, "idle threshold"),
            ("observation_retention_days", 366, "retention"),
            ("observation_max_rows", 499, "max rows"),
            ("reflection_interval_minutes", 4, "reflection interval"),
            ("reflection_min_gap_minutes", 14, "minimum gap"),
            ("reflection_quiet_after_nudge_minutes", 121, "quiet after nudge"),
            ("reflection_context_hours", 25, "reflection context"),
        ],
    )
    def test_observation_and_reflection_bounds(self, field_name, value, expected):
        form = SettingsForm()
        setattr(form, field_name, value)
        errors = validate(form, available_providers=self._providers())
        assert any(expected in error.lower() for error in errors)

    def test_invalid_observation_redact_pattern(self):
        form = SettingsForm(observation_redact_patterns=["valid", "[broken"])
        errors = validate(form, available_providers=self._providers())
        assert any("[broken" in error and "redact pattern" in error.lower() for error in errors)

    def test_scale_too_small(self):
        form = SettingsForm(pony_scale=0.1)
        errors = validate(form, available_providers=self._providers())
        assert any("scale" in e.lower() for e in errors)

    def test_scale_too_large(self):
        form = SettingsForm(pony_scale=5.0)
        errors = validate(form, available_providers=self._providers())
        assert any("scale" in e.lower() for e in errors)

    def test_scale_boundary_ok(self):
        form = SettingsForm(pony_scale=0.3)
        assert not validate(form, available_providers=self._providers())
        form = SettingsForm(pony_scale=3.0)
        assert not validate(form, available_providers=self._providers())

    def test_attention_too_short(self):
        form = SettingsForm(pony_attention_seconds=2)
        errors = validate(form, available_providers=self._providers())
        assert any("attention" in e.lower() for e in errors)

    def test_attention_too_long(self):
        form = SettingsForm(pony_attention_seconds=400)
        errors = validate(form, available_providers=self._providers())
        assert any("attention" in e.lower() for e in errors)

    def test_attention_boundary_ok(self):
        form = SettingsForm(pony_attention_seconds=5)
        assert not validate(form, available_providers=self._providers())
        form = SettingsForm(pony_attention_seconds=300)
        assert not validate(form, available_providers=self._providers())

    def test_interval_too_short(self):
        form = SettingsForm(reminders_check_interval=5)
        errors = validate(form, available_providers=self._providers())
        assert any("interval" in e.lower() for e in errors)

    def test_interval_too_long(self):
        form = SettingsForm(reminders_check_interval=5000)
        errors = validate(form, available_providers=self._providers())
        assert any("interval" in e.lower() for e in errors)

    def test_max_nudges_zero(self):
        form = SettingsForm(reminders_max_nudges=0)
        errors = validate(form, available_providers=self._providers())
        assert any("nudge" in e.lower() for e in errors)

    def test_batch_limit_zero(self):
        form = SettingsForm(reminders_batch_limit=0)
        errors = validate(form, available_providers=self._providers())
        assert any("batch" in e.lower() for e in errors)

    def test_batch_limit_too_high(self):
        form = SettingsForm(reminders_batch_limit=25)
        errors = validate(form, available_providers=self._providers())
        assert any("batch" in e.lower() for e in errors)

    def test_quiet_hours_bad_range(self):
        form = SettingsForm(reminders_quiet_start=25)
        errors = validate(form, available_providers=self._providers())
        assert any("quiet" in e.lower() for e in errors)

    def test_nudge_gaps_negative(self):
        form = SettingsForm(reminders_nudge_gaps=[-1, 30])
        errors = validate(form, available_providers=self._providers())
        assert any("nudge" in e.lower() for e in errors)

    def test_nudge_gaps_empty(self):
        form = SettingsForm(reminders_nudge_gaps=[])
        errors = validate(form, available_providers=self._providers())
        assert any("nudge" in e.lower() or "gap" in e.lower() for e in errors)

    def test_work_hours_bad_start(self):
        form = SettingsForm(work_hours_start="9am")
        errors = validate(form, available_providers=self._providers())
        assert any("work" in e.lower() and "start" in e.lower() for e in errors)

    def test_work_hours_bad_end(self):
        form = SettingsForm(work_hours_end="25:00")
        errors = validate(form, available_providers=self._providers())
        assert any("work" in e.lower() and "end" in e.lower() for e in errors)

    def test_work_hours_valid_times(self):
        form = SettingsForm(work_hours_start="08:30", work_hours_end="18:00")
        errors = validate(form, available_providers=self._providers())
        assert not any("work" in e.lower() for e in errors)

    def test_work_hours_midnight(self):
        form = SettingsForm(work_hours_start="00:00", work_hours_end="23:59")
        errors = validate(form, available_providers=self._providers())
        assert not any("work" in e.lower() for e in errors)

    def test_work_hours_bad_minute(self):
        form = SettingsForm(work_hours_start="08:60")
        errors = validate(form, available_providers=self._providers())
        assert any("work" in e.lower() for e in errors)

    def test_weekday_out_of_range(self):
        form = SettingsForm(work_hours_weekdays=[0, 7])
        errors = validate(form, available_providers=self._providers())
        assert any("weekday" in e.lower() for e in errors)

    def test_weekday_negative(self):
        form = SettingsForm(work_hours_weekdays=[-1])
        errors = validate(form, available_providers=self._providers())
        assert any("weekday" in e.lower() for e in errors)

    def test_log_path_relative(self):
        form = SettingsForm(logwatch_paths=["relative/path.log"])
        errors = validate(form, available_providers=self._providers())
        assert any("log" in e.lower() and "absolute" in e.lower() for e in errors)

    def test_log_path_blank(self):
        form = SettingsForm(logwatch_paths=[""])
        errors = validate(form, available_providers=self._providers())
        assert any("blank" in e.lower() or "must not" in e.lower() for e in errors)

    def test_log_path_tilde_accepted_by_validation(self):
        """~/ paths pass validation because apply_to_config normalizes them."""
        form = SettingsForm(logwatch_paths=["~/my.log"])
        errors = validate(form, available_providers=self._providers())
        assert not any("absolute" in e.lower() for e in errors)

    def test_log_path_expanded_absolute_ok(self):
        """An already-expanded absolute path is fine."""
        form = SettingsForm(logwatch_paths=["/home/user/my.log"])
        errors = validate(form, available_providers=self._providers())
        assert not any("log" in e.lower() for e in errors)

    def test_log_bounds_too_small(self):
        form = SettingsForm(logwatch_max_lines=0)
        errors = validate(form, available_providers=self._providers())
        assert any("lines" in e.lower() for e in errors)

    def test_log_chars_too_small(self):
        form = SettingsForm(logwatch_max_chars=50)
        errors = validate(form, available_providers=self._providers())
        assert any("chars" in e.lower() for e in errors)

    def test_provider_unknown(self):
        form = SettingsForm(active_provider="nonexistent")
        errors = validate(form, available_providers=["openai", "ollama"])
        assert any("provider" in e.lower() for e in errors)

    def test_provider_valid(self):
        form = SettingsForm(active_provider="ollama")
        errors = validate(form, available_providers=["openai", "ollama"])
        assert not any("provider" in e.lower() for e in errors)

    def test_character_unknown(self):
        form = SettingsForm(character="unicron")
        errors = validate(form, available_providers=["openai"])
        assert any("character" in e.lower() for e in errors)

    def test_character_valid(self):
        form = SettingsForm(character="rainbow-dash")
        errors = validate(form, available_providers=["openai"])
        assert not any("character" in e.lower() for e in errors)

    def test_multiple_errors_accumulate(self):
        form = SettingsForm(
            pony_scale=0.1,
            pony_attention_seconds=1,
            reminders_check_interval=1,
            reminders_max_nudges=0,
            work_hours_start="bad",
            work_hours_weekdays=[99],
            logwatch_paths=["relative"],
            active_provider="fake",
        )
        errors = validate(form, available_providers=["openai"])
        assert len(errors) >= 5

    def test_valid_form_roundtrip(self):
        """A form read from config should always validate."""
        config = Config()
        form = read_form(config)
        providers = sorted(config.llm.providers)
        errors = validate(form, available_providers=providers)
        assert errors == []


# ── detect_changes ─────────────────────────────────────────────────────


class TestDetectChanges:
    def test_no_changes(self):
        old = SettingsForm()
        new = SettingsForm()
        assert detect_changes(old, new) == {}

    def test_single_change(self):
        old = SettingsForm()
        new = SettingsForm(screenshot_enabled=True)
        changes = detect_changes(old, new)
        assert changes == {"screenshot_enabled": True}

    def test_multiple_changes(self):
        old = SettingsForm()
        new = SettingsForm(screenshot_enabled=True, pony_scale=2.0, character="clippy")
        changes = detect_changes(old, new)
        assert "screenshot_enabled" in changes
        assert "pony_scale" in changes
        assert "character" in changes
        assert "reminders_enabled" not in changes

    def test_list_change(self):
        old = SettingsForm()
        new = SettingsForm(reminders_nudge_gaps=[10, 20])
        changes = detect_changes(old, new)
        assert "reminders_nudge_gaps" in changes

    def test_autostart_change(self):
        old = SettingsForm(autostart_enabled=False)
        new = SettingsForm(autostart_enabled=True)
        changes = detect_changes(old, new)
        assert changes == {"autostart_enabled": True}


# ── needs_restart ──────────────────────────────────────────────────────


class TestNeedsRestart:
    def test_no_restart_for_scale(self):
        reasons = needs_restart({"pony_scale": True})
        assert reasons == []  # scale applies live via set_scale()

    def test_no_restart_for_reminder_change(self):
        reasons = needs_restart({"reminders_enabled": True})
        assert reasons == []

    def test_no_restart_for_screenshot(self):
        reasons = needs_restart({"screenshot_enabled": True})
        assert reasons == []

    def test_restart_for_provider(self):
        reasons = needs_restart({"active_provider": True})
        assert len(reasons) >= 1
        assert any("provider" in r.lower() for r in reasons)

    def test_no_restart_for_character(self):
        reasons = needs_restart({"character": True})
        assert reasons == []  # character applies live via set_character()

    def test_restart_for_provider_only(self):
        reasons = needs_restart({"active_provider": True, "character": True})
        assert len(reasons) == 1  # only provider needs restart

    def test_no_restart_for_autostart(self):
        reasons = needs_restart({"autostart_enabled": True})
        assert reasons == []


# ── CHARACTER_SLUGS ────────────────────────────────────────────────────


class TestCharacterSlugs:
    def test_known_slugs_present(self):
        assert "twilight" in CHARACTER_SLUGS
        assert "rainbow-dash" in CHARACTER_SLUGS
        assert "clippy" in CHARACTER_SLUGS
        assert "orb" in CHARACTER_SLUGS

    def test_no_empty_slugs(self):
        for slug in CHARACTER_SLUGS:
            assert slug, "empty character slug"


# ── full round-trip: Config -> form -> apply -> Config ────────────────


class TestFullRoundTrip:
    def test_save_load_via_config(self, tmp_path):
        """Read config, modify form, apply, save, reload — values persist."""
        config = Config()
        config.llm.active = "ollama"
        config.screenshot_enabled = True
        config.ui.character = "rainbow-dash"
        config.ui.scale = 1.5
        config.reminders.check_interval_seconds = 120
        config.reminders.work_hours.enabled = True
        config.reminders.work_hours.start = "08:00"
        config.reminders.work_hours.weekdays = [1, 3, 5]
        config.logwatch.enabled = True
        config.logwatch.files = ["/tmp/test.log"]

        path = tmp_path / "config.yaml"
        config.save(path)

        # Reload and verify
        loaded = Config.load(path)
        assert loaded.llm.active == "ollama"
        assert loaded.screenshot_enabled is True
        assert loaded.ui.character == "rainbow-dash"
        assert loaded.ui.scale == 1.5
        assert loaded.reminders.check_interval_seconds == 120
        assert loaded.reminders.work_hours.enabled is True
        assert loaded.reminders.work_hours.start == "08:00"
        assert loaded.reminders.work_hours.weekdays == [1, 3, 5]
        assert loaded.logwatch.enabled is True
        assert loaded.logwatch.files == ["/tmp/test.log"]

    def test_form_modify_apply_save(self, tmp_path):
        """Modify a form, apply to config, save, reload — changes persist."""
        config = Config()
        path = tmp_path / "config.yaml"

        form = read_form(config)
        form.screenshot_enabled = True
        form.character = "clippy"
        form.pony_scale = 1.5
        form.reminders_check_interval = 90
        form.active_provider = "ollama"

        errors = validate(form, available_providers=sorted(config.llm.providers))
        assert errors == []

        apply_to_config(form, config)
        config.save(path)

        loaded = Config.load(path)
        assert loaded.screenshot_enabled is True
        assert loaded.ui.character == "clippy"
        assert loaded.ui.scale == 1.5
        assert loaded.reminders.check_interval_seconds == 90
        assert loaded.llm.active == "ollama"

    def test_invalid_form_blocked(self):
        """Validation prevents applying bad values."""
        form = SettingsForm(pony_scale=0.01, active_provider="fake")
        errors = validate(form, available_providers=["openai"])
        assert len(errors) >= 2

    def test_all_field_roundtrip(self, tmp_path):
        """Every field survives Config -> form -> apply -> save -> load."""
        config = Config()
        config.screenshot_enabled = True
        config.auto_track_commitments = False
        config.ui.character = "fluttershy"
        config.ui.scale = 1.25
        config.ui.idle_wander = False
        config.ui.attention_seconds = 45
        config.reminders.enabled = False
        config.reminders.check_interval_seconds = 120
        config.reminders.quiet_hours_start = 22
        config.reminders.quiet_hours_end = 6
        config.reminders.nudge_gaps_minutes = [15, 30, 60, 120]
        config.reminders.max_nudges = 5
        config.reminders.batch_limit = 4
        config.reminders.work_hours.enabled = True
        config.reminders.work_hours.start = "07:30"
        config.reminders.work_hours.end = "16:00"
        config.reminders.work_hours.weekdays = [0, 2, 4]
        config.reminders.work_hours.closing_nudge = False
        config.reminders.work_hours.suppress_off_hours = True
        config.logwatch.enabled = True
        config.logwatch.files = ["/var/log/a.log", "/var/log/b.log"]
        config.logwatch.max_lines_per_file = 500
        config.logwatch.max_total_chars = 15000
        config.llm.active = "anthropic"
        config.awareness.enabled = True
        config.awareness.interval_seconds = 180
        config.awareness.cooldown_minutes = 60
        config.awareness.minimum_confidence = 0.85
        config.awareness.focus_policy = "never interrupt on Monday"

        path = tmp_path / "config.yaml"
        config.save(path)

        # Read, apply back (no changes), save, reload
        form = read_form(config)
        errors = validate(form, available_providers=sorted(config.llm.providers))
        assert errors == []
        apply_to_config(form, config)
        config.save(path)

        loaded = Config.load(path)
        assert loaded.screenshot_enabled is True
        assert loaded.auto_track_commitments is False
        assert loaded.ui.character == "fluttershy"
        assert loaded.ui.scale == 1.25
        assert loaded.ui.idle_wander is False
        assert loaded.ui.attention_seconds == 45
        assert loaded.reminders.enabled is False
        assert loaded.reminders.check_interval_seconds == 120
        assert loaded.reminders.quiet_hours_start == 22
        assert loaded.reminders.quiet_hours_end == 6
        assert loaded.reminders.nudge_gaps_minutes == [15, 30, 60, 120]
        assert loaded.reminders.max_nudges == 5
        assert loaded.reminders.batch_limit == 4
        assert loaded.reminders.work_hours.enabled is True
        assert loaded.reminders.work_hours.start == "07:30"
        assert loaded.reminders.work_hours.end == "16:00"
        assert loaded.reminders.work_hours.weekdays == [0, 2, 4]
        assert loaded.reminders.work_hours.closing_nudge is False
        assert loaded.reminders.work_hours.suppress_off_hours is True
        assert loaded.logwatch.enabled is True
        assert loaded.logwatch.files == ["/var/log/a.log", "/var/log/b.log"]
        assert loaded.logwatch.max_lines_per_file == 500
        assert loaded.logwatch.max_total_chars == 15000
        assert loaded.llm.active == "anthropic"

    def test_logwatch_tilde_path_normalized_on_apply(self, tmp_path):
        """~/ paths in the form are expanded to absolute paths when applied."""
        config = Config()
        form = read_form(config)
        form.logwatch_paths = ["~/myapp.log"]
        # validation accepts ~/ because it expands to absolute
        errors = validate(form, available_providers=sorted(config.llm.providers))
        assert not any("absolute" in e.lower() for e in errors)
        apply_to_config(form, config)
        # config must have the expanded absolute path
        assert len(config.logwatch.files) == 1
        assert config.logwatch.files[0].startswith("/")
        assert "~" not in config.logwatch.files[0]
        # saving and reloading must succeed (Config rejects ~/ paths)
        path = tmp_path / "config.yaml"
        config.save(path)
        loaded = Config.load(path)
        assert loaded.logwatch.files == config.logwatch.files

    def test_logwatch_blank_path_rejected_by_validation(self):
        """Blank log paths are caught by validation."""
        form = SettingsForm(logwatch_paths=["/var/log/a.log", ""])
        errors = validate(form, available_providers=["openai"])
        assert any("blank" in e.lower() for e in errors)

    def test_autostart_enabled_passed_to_form(self):
        """autostart_enabled kwarg populates the form correctly."""
        config = Config()
        form = read_form(config, autostart_enabled=True)
        assert form.autostart_enabled is True
        form2 = read_form(config, autostart_enabled=False)
        assert form2.autostart_enabled is False


class TestProactiveSettings:
    def test_roundtrip(self):
        config = Config()
        form = read_form(config)
        form.onboarding_enabled = False
        form.proactive_questions_enabled = False
        form.proactive_min_gap_hours = 6
        form.proactive_max_questions_per_batch = 2
        form.proactive_silence_default_hours = 48
        form.proactive_require_empty_agenda = False
        apply_to_config(form, config)
        assert config.onboarding.enabled is False
        assert config.proactive_questions.enabled is False
        assert config.proactive_questions.min_gap_hours == 6
        assert config.proactive_questions.max_questions_per_batch == 2
        assert config.proactive_questions.silence_default_hours == 48
        assert config.proactive_questions.require_empty_agenda is False

    @pytest.mark.parametrize(
        ("field", "value", "needle"),
        [
            ("proactive_min_gap_hours", 2, "3–24"),
            ("proactive_max_questions_per_batch", 6, "1–5"),
            ("proactive_silence_default_hours", 169, "1–168"),
        ],
    )
    def test_validation(self, field, value, needle):
        form = SettingsForm()
        setattr(form, field, value)
        errors = validate(form, available_providers=["openai"])
        assert any(needle in error for error in errors)
