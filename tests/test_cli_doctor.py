"""Focused CLI tests for doctor and init — platform/install mocked, isolated paths.

Verifies that doctor reports work-hours state, logwatch privacy/paths,
autostart status, platform screen permission guidance, local-provider health
check suggestions, vision model limitations, and first-run next steps.
Also verifies init output mentions qwen27b-vllm and check-llm.

All tests use the conftest isolated_dirs fixture (tmp_path config/data) and
monkeypatch platform.system() and install functions. No network calls.
"""

from __future__ import annotations

from pathlib import Path

from clipponyai.cli import main
from clipponyai.config import Config


# ── helpers ────────────────────────────────────────────────────────────


def _run_doctor(monkeypatch, tmp_path, platform_name: str = "Linux",
                autostart_msg: str = "disabled") -> str:
    """Run doctor with mocked platform and autostart, return stdout."""
    monkeypatch.setattr("clipponyai.install._SYSTEM", platform_name)
    monkeypatch.setattr("clipponyai.install.platform.system", lambda: platform_name)
    monkeypatch.setattr(
        "clipponyai.install.autostart_status",
        lambda: autostart_msg,
    )
    # Prevent sprite fetch network calls
    monkeypatch.setattr("clipponyai.sprite_fetch.have_sprites", lambda: False)
    code = main(["doctor"])
    # We capture via capsys in the actual tests
    return code


# ── init output ────────────────────────────────────────────────────────


def test_init_output_mentions_qwen27b_vllm(capsys, monkeypatch, tmp_path):
    """init next-steps mention qwen27b-vllm as a local provider option."""
    # Ensure config does not exist
    config_p = tmp_path / "cfg" / "clipponyai" / "config.yaml"
    if config_p.exists():
        config_p.unlink()
    monkeypatch.setattr("clipponyai.config.user_config_dir", lambda name: str(tmp_path / "cfg"))

    code = main(["init"])
    assert code == 0
    out = capsys.readouterr().out
    assert "qwen27b-vllm" in out


def test_init_output_mentions_check_llm(capsys, monkeypatch, tmp_path):
    """init next-steps mention check-llm for verification."""
    config_p = tmp_path / "cfg" / "clipponyai" / "config.yaml"
    if config_p.exists():
        config_p.unlink()
    monkeypatch.setattr("clipponyai.config.user_config_dir", lambda name: str(tmp_path / "cfg"))

    code = main(["init"])
    assert code == 0
    out = capsys.readouterr().out
    assert "check-llm" in out


# ── doctor: work-hours state ───────────────────────────────────────────


def test_doctor_reports_work_hours_disabled(capsys, monkeypatch, tmp_path):
    """Doctor prints 'work hours: disabled' when work_hours.enabled is False."""
    Config().save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "work hours: disabled" in out


def test_doctor_reports_work_hours_enabled(capsys, monkeypatch, tmp_path):
    """Doctor prints work hours details when enabled."""
    config = Config()
    config.reminders.work_hours.enabled = True
    config.reminders.work_hours.start = "08:30"
    config.reminders.work_hours.end = "17:30"
    config.reminders.work_hours.weekdays = [0, 1, 2, 3, 4]
    config.reminders.work_hours.closing_nudge = True
    config.save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "08:30" in out
    assert "17:30" in out
    assert "Mon" in out
    assert "closing nudge=on" in out


# ── doctor: logwatch privacy/paths ─────────────────────────────────────


def test_doctor_reports_logwatch_disabled(capsys, monkeypatch, tmp_path):
    """Doctor prints 'logwatch: disabled' when logwatch is off."""
    Config().save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "logwatch: disabled" in out


def test_doctor_reports_logwatch_enabled_with_paths(capsys, monkeypatch, tmp_path):
    """Doctor lists logwatch files and their existence status."""
    log_file = str(tmp_path / "test.log")
    Path(log_file).write_text("some log\n")
    missing_file = "/nonexistent/path/app.log"

    config = Config()
    config.logwatch.enabled = True
    config.logwatch.files = [log_file, missing_file]
    config.save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "logwatch: enabled" in out
    assert log_file in out
    assert "found" in out.lower()
    assert missing_file in out
    assert "not found" in out.lower()


# ── doctor: autostart status ───────────────────────────────────────────


def test_doctor_reports_autostart_disabled(capsys, monkeypatch, tmp_path):
    """Doctor includes autostart status line."""
    Config().save()
    _run_doctor(monkeypatch, tmp_path, autostart_msg="disabled (~/.config/autostart/clipponyai.desktop not found)")
    out = capsys.readouterr().out
    assert "autostart:" in out
    assert "disabled" in out


def test_doctor_reports_autostart_enabled(capsys, monkeypatch, tmp_path):
    """Doctor reports enabled autostart."""
    Config().save()
    _run_doctor(monkeypatch, tmp_path, autostart_msg="enabled — ~/.config/autostart/clipponyai.desktop")
    out = capsys.readouterr().out
    assert "autostart:" in out
    assert "enabled" in out


# ── doctor: macOS screen permission guidance ───────────────────────────


def test_doctor_macos_screen_permission_when_enabled(capsys, monkeypatch, tmp_path):
    """Doctor prints macOS Screen Recording guidance when screenshot is ON."""
    config = Config()
    config.screenshot_enabled = True
    config.save()
    _run_doctor(monkeypatch, tmp_path, platform_name="Darwin")
    out = capsys.readouterr().out
    assert "Screen Recording" in out
    assert "Accessibility" in out


def test_doctor_macos_screen_permission_note_when_disabled(capsys, monkeypatch, tmp_path):
    """Doctor prints a macOS note about future permissions when screenshot is OFF."""
    config = Config()
    config.screenshot_enabled = False
    config.save()
    _run_doctor(monkeypatch, tmp_path, platform_name="Darwin")
    out = capsys.readouterr().out
    assert "Screen Recording" in out or "screen recording" in out.lower()


def test_doctor_linux_no_macos_permissions(capsys, monkeypatch, tmp_path):
    """Doctor does not print macOS-specific guidance on Linux."""
    config = Config()
    config.screenshot_enabled = True
    config.save()
    _run_doctor(monkeypatch, tmp_path, platform_name="Linux")
    out = capsys.readouterr().out
    assert "Screen Recording" not in out
    assert "Accessibility" not in out


# ── doctor: local-provider health-check suggestion ─────────────────────


def test_doctor_local_provider_check_llm_hint(capsys, monkeypatch, tmp_path):
    """Doctor suggests check-llm for local providers."""
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "check-llm" in out


def test_doctor_cloud_provider_no_check_llm_hint(capsys, monkeypatch, tmp_path):
    """Doctor does not suggest check-llm for cloud providers."""
    config = Config()
    config.llm.active = "openai"
    config.save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    # The hint only appears for local (base_url + no api_key_env) providers
    assert "local endpoint" not in out


# ── doctor: vision model limitation ────────────────────────────────────


def test_doctor_text_only_vision_warning(capsys, monkeypatch, tmp_path):
    """Doctor warns about text-only vision for local models without dedicated vision model."""
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.screenshot_enabled = True
    config.save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "text-only" in out.lower()


# ── doctor: first-run next steps ───────────────────────────────────────


def test_doctor_first_run_next_steps_no_sprites(capsys, monkeypatch, tmp_path):
    """Doctor shows first-run next steps when sprites are missing."""
    Config().save()
    _run_doctor(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "first-run next steps" in out
    assert "fetch-sprites" in out


def test_doctor_no_first_run_when_sprites_exist(capsys, monkeypatch, tmp_path):
    """Doctor omits first-run steps when sprites exist and config is present."""
    Config().save()
    monkeypatch.setattr("clipponyai.sprite_fetch.have_sprites", lambda: True)
    monkeypatch.setattr("clipponyai.install._SYSTEM", "Linux")
    monkeypatch.setattr("clipponyai.install.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "clipponyai.install.autostart_status",
        lambda: "disabled",
    )
    main(["doctor"])
    out = capsys.readouterr().out
    assert "first-run next steps" not in out


# ── doctor: non-mutating ───────────────────────────────────────────────


def test_doctor_does_not_modify_config(capsys, monkeypatch, tmp_path):
    """Doctor is read-only — it does not change config.yaml."""
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.reminders.work_hours.enabled = True
    config.logwatch.enabled = True
    config.logwatch.files = ["/var/log/syslog"]
    config.save()

    # Snapshot before
    before = config
    _run_doctor(monkeypatch, tmp_path)

    # Reload and compare
    after = Config.load()
    assert after.llm.active == before.llm.active
    assert after.reminders.work_hours.enabled == before.reminders.work_hours.enabled
    assert after.logwatch.enabled == before.logwatch.enabled
    assert after.logwatch.files == before.logwatch.files


# ── doctor: no network calls ───────────────────────────────────────────


def test_doctor_no_network_with_local_provider(capsys, monkeypatch, tmp_path):
    """Doctor with a local provider does not make network calls."""
    config = Config()
    config.llm.active = "qwen27b-vllm"
    config.save()

    # Block all HTTP
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("blocked")))

    _run_doctor(monkeypatch, tmp_path)
    # If we get here without raising, no network was made
    out = capsys.readouterr().out
    assert "clipponyai" in out  # version line always prints
