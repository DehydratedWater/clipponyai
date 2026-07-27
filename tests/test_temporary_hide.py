from clipponyai.app import temporary_hide_remaining_seconds
from clipponyai.overlay import TEMPORARY_HIDE_OPTIONS


def test_temporary_hide_menu_options():
    assert TEMPORARY_HIDE_OPTIONS == (
        ("1m", 60),
        ("15m", 900),
        ("30m", 1800),
        ("45m", 2700),
        ("1h", 3600),
        ("2h", 7200),
        ("4h", 14400),
        ("6h", 21600),
    )


def test_temporary_hide_remaining_rounds_up():
    assert temporary_hide_remaining_seconds("1900.25", now=1000.0) == 901


def test_temporary_hide_expired_or_invalid():
    assert temporary_hide_remaining_seconds("999", now=1000.0) == 0
    assert temporary_hide_remaining_seconds("") == 0
    assert temporary_hide_remaining_seconds("not-a-timestamp") == 0
