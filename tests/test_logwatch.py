"""Tests for privacy-gated log awareness (logwatch module + brain tool).

Covers:
- LogWatchConfig defaults, validation, round-trip
- logwatch.py bounds, missing files, disabled mode, labelling
- recent_logs brain tool: disabled error, enabled FAST-lane delegation
- Fake-LLM tool behaviour end-to-end
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clipponyai.config import Config, LogWatchConfig
from clipponyai.logwatch import read_recent_logs, tail_file


# ── LogWatchConfig ────────────────────────────────────────────────────
def test_logwatch_defaults_are_private():
    cfg = LogWatchConfig()
    assert cfg.enabled is False
    assert cfg.files == []
    assert cfg.max_lines_per_file == 200
    assert cfg.max_total_chars == 8000


def test_logwatch_requires_positive_bounds():
    with pytest.raises(ValidationError, match="must be positive"):
        LogWatchConfig(max_lines_per_file=0)
    with pytest.raises(ValidationError, match="must be positive"):
        LogWatchConfig(max_total_chars=-1)


def test_logwatch_requires_absolute_paths():
    with pytest.raises(ValidationError, match="must be absolute"):
        LogWatchConfig(files=["relative/path.log"])
    # absolute is fine
    LogWatchConfig(files=["/var/log/app.log"])


def test_logwatch_enabled_with_paths():
    cfg = LogWatchConfig(
        enabled=True,
        files=["/var/log/app.log", "/tmp/service.log"],
        max_lines_per_file=50,
        max_total_chars=4000,
    )
    assert cfg.enabled is True
    assert len(cfg.files) == 2
    assert cfg.max_lines_per_file == 50


def test_config_includes_logwatch():
    cfg = Config()
    assert isinstance(cfg.logwatch, LogWatchConfig)
    assert cfg.logwatch.enabled is False


def test_config_logwatch_roundtrip(tmp_path):
    cfg = Config()
    cfg.logwatch = LogWatchConfig(
        enabled=True,
        files=["/var/log/app.log"],
        max_lines_per_file=100,
    )
    path = tmp_path / "cfg.yaml"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.logwatch.enabled is True
    assert loaded.logwatch.files == ["/var/log/app.log"]
    assert loaded.logwatch.max_lines_per_file == 100


# ── tail_file ─────────────────────────────────────────────────────────
def test_tail_file_returns_last_n_lines(tmp_path):
    log = tmp_path / "test.log"
    lines = [f"line-{i}\n" for i in range(100)]
    log.write_text("".join(lines))
    result = tail_file(log, 5)
    assert result == ["line-95", "line-96", "line-97", "line-98", "line-99"]


def test_tail_file_fewer_lines_than_max(tmp_path):
    log = tmp_path / "small.log"
    log.write_text("only\nthree\nlines\n")
    result = tail_file(log, 200)
    assert result == ["only", "three", "lines"]


def test_tail_file_empty_file(tmp_path):
    log = tmp_path / "empty.log"
    log.write_text("")
    assert tail_file(log, 10) == []


def test_tail_file_missing_file(tmp_path):
    assert tail_file(tmp_path / "nope.log", 10) == []


def test_tail_file_unreadable_file(tmp_path):
    log = tmp_path / "secret.log"
    log.write_text("secret\n")
    log.chmod(0o000)
    assert tail_file(log, 10) == []
    # restore so tmp cleanup works
    log.chmod(0o644)


def test_tail_file_no_trailing_newline(tmp_path):
    log = tmp_path / "bare.log"
    log.write_text("first\nsecond\nthird")
    result = tail_file(log, 10)
    assert result == ["first", "second", "third"]


def test_tail_file_single_line_no_newline(tmp_path):
    log = tmp_path / "single.log"
    log.write_text("just one line")
    result = tail_file(log, 5)
    assert result == ["just one line"]


def test_tail_file_respects_max_lines(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("\n".join(f"row-{i}" for i in range(500)))
    result = tail_file(log, 3)
    assert len(result) == 3
    assert result[0] == "row-497"


# ── read_recent_logs ──────────────────────────────────────────────────
def test_read_recent_logs_disabled_returns_empty():
    cfg = LogWatchConfig(enabled=False, files=["/tmp/x.log"])
    assert read_recent_logs(cfg) == ""


def test_read_recent_logs_no_files_returns_empty():
    cfg = LogWatchConfig(enabled=True, files=[])
    assert read_recent_logs(cfg) == ""


def test_read_recent_logs_labels_sources(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("INFO started\nERROR boom\n")
    cfg = LogWatchConfig(
        enabled=True,
        files=[str(log)],
        max_lines_per_file=10,
        max_total_chars=4000,
    )
    result = read_recent_logs(cfg)
    assert "[app.log]" in result
    assert "ERROR boom" in result


def test_read_recent_logs_multiple_files(tmp_path):
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("alpha\n")
    b.write_text("beta\n")
    cfg = LogWatchConfig(
        enabled=True,
        files=[str(a), str(b)],
        max_lines_per_file=10,
        max_total_chars=4000,
    )
    result = read_recent_logs(cfg)
    assert "[a.log]" in result
    assert "[b.log]" in result
    assert "alpha" in result
    assert "beta" in result


def test_read_recent_logs_respects_max_total_chars(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("\n".join(f"line-{i:04d}" for i in range(500)))
    cfg = LogWatchConfig(
        enabled=True,
        files=[str(log)],
        max_lines_per_file=200,
        max_total_chars=500,
    )
    result = read_recent_logs(cfg)
    # With max_lines_per_file=200, tail_file returns 200 lines (~2000 chars),
    # but max_total_chars=500 should cap the total output.
    assert len(result) <= 520  # small tolerance for label overhead


def test_read_recent_logs_skips_missing_files(tmp_path):
    existing = tmp_path / "exists.log"
    existing.write_text("here\n")
    cfg = LogWatchConfig(
        enabled=True,
        files=[str(tmp_path / "missing.log"), str(existing)],
        max_lines_per_file=10,
        max_total_chars=4000,
    )
    result = read_recent_logs(cfg)
    assert "[exists.log]" in result
    assert "here" in result


# ── recent_logs brain tool ────────────────────────────────────────────
def test_recent_logs_tool_disabled_by_default(make_brain):
    brain = make_brain({})
    result = brain._tool_recent_logs({"question": "what happened?"})
    assert "disabled" in result


def test_recent_logs_tool_enabled_calls_fast_lane(make_brain, config, tmp_path):
    log = tmp_path / "svc.log"
    log.write_text("2024-01-01 ERROR connection refused\n2024-01-01 INFO retrying\n")
    config.logwatch = LogWatchConfig(
        enabled=True,
        files=[str(log)],
        max_lines_per_file=10,
        max_total_chars=4000,
    )
    brain = make_brain({"log-analyst": "The service hit a connection error and is retrying."})
    result = brain._tool_recent_logs({"question": "what errors occurred?"})
    assert result == "The service hit a connection error and is retrying."


def test_recent_logs_tool_uses_injected_source(make_brain, config):
    config.logwatch = LogWatchConfig(enabled=True, files=[])
    brain = make_brain({"log-analyst": "The injected service is healthy."})
    brain.log_fn = lambda: "INFO injected service healthy\n"

    result = brain._tool_recent_logs({"question": "is the service healthy?"})

    assert result == "The injected service is healthy."


def test_recent_logs_tool_empty_logs_message(make_brain, config):
    config.logwatch = LogWatchConfig(enabled=True, files=["/nonexistent/log.log"])
    brain = make_brain({"log-analyst": "nothing"})
    result = brain._tool_recent_logs({"question": "any errors?"})
    assert "No log content available" in result


def test_recent_logs_tool_default_question(make_brain, config, tmp_path):
    log = tmp_path / "x.log"
    log.write_text("some log line\n")
    config.logwatch = LogWatchConfig(
        enabled=True,
        files=[str(log)],
        max_lines_per_file=10,
        max_total_chars=4000,
    )
    brain = make_brain({"log-analyst": "summary here"})
    result = brain._tool_recent_logs({})  # no question key
    assert result == "summary here"


# ── end-to-end: model calls recent_logs tool ─────────────────────────
async def test_model_uses_recent_logs_tool(make_brain, config, store, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("ERROR: database timeout\nINFO: recovered\n")
    config.logwatch = LogWatchConfig(
        enabled=True,
        files=[str(log)],
        max_lines_per_file=10,
        max_total_chars=4000,
    )
    brain = make_brain({
        "pony": [
            ("tool", "recent_logs", {"question": "what errors in the logs?"}),
            "The logs show a database timeout that was recovered from.",
        ],
        "message-sensor": {"done_task_ids": [], "maybe_done_task_ids": [], "commitments": []},
        "log-analyst": "A database timeout error occurred and the service recovered.",
    })
    reply = await brain.respond("what happened in the logs?")
    assert "timeout" in reply.lower() or "database" in reply.lower()
