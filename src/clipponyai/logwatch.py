"""Pure log-tailing module: bounded, privacy-gated, no side effects.

Reads the *last N lines* of configured local files without streaming, without
unbounded reads, and without touching any service or database.  Missing or
unreadable files are silently skipped.  Returns an empty string when the
feature is disabled.

Used only as a data source for the ``recent_logs`` brain tool — the actual
answering is delegated to the FAST LLM lane, never regex.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import LogWatchConfig

log = logging.getLogger("clipponyai.logwatch")


def tail_file(path: Path, max_lines: int) -> list[str]:
    """Return the last *max_lines* lines of a text file.

    Reads the file in binary mode and slices from the end so we never load
    gigabyte logs into memory.  Returns an empty list on any error.
    """
    try:
        if not path.is_file():
            return []
        raw = path.read_bytes()
    except OSError as exc:
        log.debug("skipping unreadable log %s: %s", path, exc)
        return []

    if not raw:
        return []

    # Find newline positions scanning from the end.
    newline_positions: list[int] = []
    for i in range(len(raw) - 1, -1, -1):
        if raw[i:i + 1] == b"\n":
            newline_positions.append(i)
            if len(newline_positions) == max_lines + 1:
                break

    if not newline_positions:
        # No newlines at all — entire file is one line
        try:
            text = raw.decode("utf-8", errors="replace").strip()
            return [text] if text else []
        except Exception:
            return []

    # newline_positions is [last_nl, second_last_nl, ...]
    # We want the last max_lines lines.
    # If we found max_lines+1 newlines, the content starts after the
    # (max_lines+1)th newline from the end.
    # If we found fewer, start from the beginning of the file.
    if len(newline_positions) > max_lines:
        start = newline_positions[max_lines] + 1  # byte after the cut-off newline
    else:
        start = 0

    # End is the last byte of the file (not the last newline — a trailing
    # newline just means the last line is empty, which splitlines handles).
    end = len(raw)

    chunk = raw[start:end]
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    return lines[-max_lines:] if len(lines) > max_lines else lines


def read_recent_logs(config: LogWatchConfig) -> str:
    """Return a labelled, bounded snapshot of configured log files.

    Returns ``""`` when ``config.enabled`` is ``False``.  Each file's
    contribution is prefixed with ``[<filename>]`` so the LLM can attribute
    findings.  Total output is hard-capped at ``config.max_total_chars``.
    """
    if not config.enabled:
        return ""

    paths: list[Path] = []
    for p in config.files:
        try:
            paths.append(Path(p))
        except Exception:
            continue

    if not paths:
        return ""

    parts: list[str] = []
    total_chars = 0
    max_chars = config.max_total_chars

    for path in paths:
        lines = tail_file(path, config.max_lines_per_file)
        if not lines:
            continue
        label = f"[{path.name}]\n"
        text = label + "\n".join(lines)
        # Enforce per-contribution truncation against remaining budget
        remaining = max_chars - total_chars
        if len(text) > remaining:
            text = text[:remaining]
        total_chars += len(text)
        parts.append(text)
        if total_chars >= max_chars:
            break

    return "\n\n".join(parts)
