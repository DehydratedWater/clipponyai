from datetime import datetime, timedelta

import pytest

from clipponyai.timeparse import parse_when

NOW = datetime(2026, 7, 22, 14, 0)  # a Wednesday


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("in 20m", NOW + timedelta(minutes=20)),
        ("in 2h", NOW + timedelta(hours=2)),
        ("in 3 days", NOW + timedelta(days=3)),
        ("in 1 week", NOW + timedelta(weeks=1)),
        ("tomorrow", datetime(2026, 7, 23, 9, 0)),
        ("tomorrow at 10", datetime(2026, 7, 23, 10, 0)),
        ("tomorrow at 10:30", datetime(2026, 7, 23, 10, 30)),
        ("tomorrow at 8pm", datetime(2026, 7, 23, 20, 0)),
        ("today at 17", datetime(2026, 7, 22, 17, 0)),
        ("at 17:30", datetime(2026, 7, 22, 17, 30)),
        ("5pm", datetime(2026, 7, 22, 17, 0)),
        ("9am", datetime(2026, 7, 23, 9, 0)),  # already past today → tomorrow
        ("friday", datetime(2026, 7, 24, 9, 0)),
        ("friday at 9", datetime(2026, 7, 24, 9, 0)),
        ("wednesday", datetime(2026, 7, 29, 9, 0)),  # today is wed → next wed
        ("tonight", datetime(2026, 7, 22, 20, 0)),
        ("noon", datetime(2026, 7, 23, 12, 0)),  # past today's noon
        ("midnight", datetime(2026, 7, 23, 0, 0)),
        ("next week", datetime(2026, 7, 29, 9, 0)),
        ("tomorrow morning", datetime(2026, 7, 23, 9, 0)),
        ("2026-08-01", datetime(2026, 8, 1, 9, 0)),
        ("2026-08-01 14:30", datetime(2026, 8, 1, 14, 30)),
        ("remind me tomorrow at 10", datetime(2026, 7, 23, 10, 0)),
        ("now", NOW),
    ],
)
def test_parses(phrase, expected):
    assert parse_when(phrase, NOW) == expected


@pytest.mark.parametrize("phrase", ["", "whenever", "the thing", "at 99:99", "2026-13-45"])
def test_unparseable_returns_none(phrase):
    assert parse_when(phrase, NOW) is None
