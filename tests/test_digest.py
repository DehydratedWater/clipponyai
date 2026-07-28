from __future__ import annotations

from datetime import datetime, timedelta

from clipponyai.accountability import Observation
from clipponyai.digest import EMPTY_DIGEST, render_activity_digest, summarize_categories

NOW = datetime(2026, 7, 28, 16, 10)


def observation(
    started_at: datetime,
    ended_at: datetime,
    *,
    row_id: int = 1,
    source: str = "os",
    app: str = "Cursor",
    title: str = "brain.py",
    category: str = "work",
    activity: str = "",
) -> Observation:
    return Observation(
        id=row_id,
        started_at=started_at,
        ended_at=ended_at,
        source=source,
        app=app,
        window_title=title,
        category=category,
        activity=activity,
        detail="",
        idle_seconds=0,
        confidence=0.9,
        payload="",
    )


def test_digest_is_a_chronological_sensor_log_with_duration_and_totals():
    rows = [
        observation(
            NOW - timedelta(minutes=11),
            NOW,
            row_id=2,
            app="Slack",
            title="",
            category="communication",
        ),
        observation(
            NOW - timedelta(minutes=58),
            NOW - timedelta(minutes=11),
            row_id=1,
        ),
        observation(
            NOW - timedelta(hours=1, minutes=54),
            NOW - timedelta(minutes=58),
            row_id=3,
            title="tests.py",
        ),
    ]

    digest = render_activity_digest(rows, now=NOW)

    assert digest.startswith("Screen activity log (written by a sensor, not by your friend)")
    assert digest.index("tests.py") < digest.index("brain.py") < digest.index("Slack")
    assert "47m" in digest
    assert "Totals: work 1h43m · communication 11m" in digest


def test_vision_annotation_and_category_fold_into_containing_os_episode():
    start = NOW - timedelta(minutes=67)
    rows = [
        observation(start, NOW, category="unknown"),
        observation(
            NOW - timedelta(minutes=10),
            NOW - timedelta(minutes=10),
            row_id=2,
            source="vision",
            app="",
            title="",
            category="work",
            activity="debugging a failing test",
        ),
    ]

    digest = render_activity_digest(rows, now=NOW)

    assert digest.count("debugging a failing test") == 1
    assert "Cursor — brain.py [work] · seen: debugging a failing test" in digest
    assert summarize_categories(rows) == {"work": 67}


def test_unmatched_vision_observation_gets_its_own_line():
    row = observation(
        NOW - timedelta(minutes=5),
        NOW - timedelta(minutes=5),
        source="vision",
        app="Brave",
        title="Docs",
        category="learning",
        activity="reading API documentation",
    )

    digest = render_activity_digest([row], now=NOW)

    assert "Brave — Docs · seen: reading API documentation [learning]" in digest


def test_up_to_three_short_episodes_merge_into_a_neighbour():
    start = NOW - timedelta(minutes=12)
    rows = [observation(start, start + timedelta(minutes=10), app="Editor")]
    for index, app in enumerate(("Mail", "Calendar", "Terminal"), start=1):
        at = start + timedelta(minutes=10, seconds=index * 20)
        rows.append(
            observation(
                at,
                at + timedelta(seconds=15),
                row_id=index + 1,
                app=app,
                title="",
            )
        )

    digest = render_activity_digest(rows, now=NOW)

    assert "briefly: Mail → Calendar → Terminal" in digest
    assert digest.count("\n  ") == 1


def test_four_short_episodes_become_one_quick_switching_line():
    start = NOW - timedelta(minutes=4)
    rows = [
        observation(
            start + timedelta(seconds=index * 40),
            start + timedelta(seconds=index * 40 + 20),
            row_id=index,
            app=f"App {index}",
            title="",
        )
        for index in range(4)
    ]

    digest = render_activity_digest(rows, now=NOW)

    assert "(quick switching between 4 apps)" in digest
    assert "App 0" not in digest


def test_idle_hides_app_and_title_and_uses_away_total():
    row = observation(
        NOW - timedelta(minutes=4),
        NOW,
        app="Secret App",
        title="Secret title",
        category="idle",
    )

    digest = render_activity_digest([row], now=NOW)

    assert "(away from keyboard)" in digest
    assert "Secret" not in digest
    assert "Totals: away 4m" in digest


def test_max_chars_trims_oldest_activity_and_keeps_newest():
    rows = [
        observation(
            NOW - timedelta(minutes=90 - index * 20),
            NOW - timedelta(minutes=72 - index * 20),
            row_id=index,
            app=f"Application-{index}-with-a-long-name",
            title=f"document-{index}-with-a-long-title.md",
        )
        for index in range(4)
    ]

    digest = render_activity_digest(rows, now=NOW, max_chars=300)

    assert len(digest) <= 300
    assert "(… earlier activity trimmed)" in digest
    assert "Application-0" not in digest
    assert "Application-3" in digest


def test_empty_input_has_explicit_exact_message():
    assert render_activity_digest([], now=NOW) == EMPTY_DIGEST
