# ruff: noqa: DTZ001, DTZ005
"""Tests for the deterministic accountability rule engine.

Covers:
  - Time-condition parsing (12h/24h, cross-midnight, before/after/between)
  - RuleEngine.evaluate_time (enabled/disabled, cooldown)
  - RuleEngine.tick (fire, activity log, delivery, allow_delivery)
  - Screen rules (no screen context, grounded assessor, invented ID ignored)
  - Validation helpers (bad cooldown, empty title/condition)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from clipponyai.accountability import get_stores
from clipponyai.rules import (
    RuleEngine,
    ScreenAssessment,
    TimeWindow,
    parse_time_condition,
    time_in_window,
    validate_add_rule,
    validate_update_rule,
)

# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def stores(tmp_path):
    from clipponyai.tasks import TaskStore

    store = TaskStore(tmp_path / "rules.db")
    result = get_stores(store)
    yield result, store
    store.close()


@pytest.fixture
def rule_store(stores):
    return stores[0]["rules"]


@pytest.fixture
def activity_store(stores):
    return stores[0]["activity"]


@pytest.fixture
def engine(rule_store, activity_store):
    return RuleEngine(rule_store, activity_store)


# ─── Time-condition parsing ───────────────────────────────────────────


class TestParseTimeCondition:
    """parse_time_condition() — 24h, 12h, before/after/between."""

    def test_after_24h(self):
        w = parse_time_condition("after 22:00")
        assert w is not None
        assert w.start_minutes == 22 * 60
        assert w.end_minutes == 24 * 60
        assert w.cross_midnight is False

    def test_after_12h_pm(self):
        w = parse_time_condition("after 10 PM")
        assert w is not None
        assert w.start_minutes == 22 * 60
        assert w.end_minutes == 24 * 60

    def test_after_12h_pm_with_minutes(self):
        w = parse_time_condition("after 10:30 PM")
        assert w is not None
        assert w.start_minutes == 22 * 60 + 30

    def test_after_12h_am(self):
        w = parse_time_condition("after 2 AM")
        assert w is not None
        assert w.start_minutes == 2 * 60

    def test_after_12h_noon(self):
        """12 PM = 12:00 (noon)."""
        w = parse_time_condition("after 12 PM")
        assert w is not None
        assert w.start_minutes == 12 * 60

    def test_after_12h_midnight(self):
        """12 AM = 0:00 (midnight)."""
        w = parse_time_condition("after 12 AM")
        assert w is not None
        assert w.start_minutes == 0

    def test_before_24h(self):
        w = parse_time_condition("before 08:30")
        assert w is not None
        assert w.start_minutes == 0
        assert w.end_minutes == 8 * 60 + 30

    def test_before_12h_am(self):
        w = parse_time_condition("before 8:30 AM")
        assert w is not None
        assert w.start_minutes == 0
        assert w.end_minutes == 8 * 60 + 30

    def test_before_12h_pm(self):
        w = parse_time_condition("before 3 PM")
        assert w is not None
        assert w.start_minutes == 0
        assert w.end_minutes == 15 * 60

    def test_between_normal(self):
        w = parse_time_condition("between 09:00 and 17:00")
        assert w is not None
        assert w.start_minutes == 9 * 60
        assert w.end_minutes == 17 * 60
        assert w.cross_midnight is False

    def test_between_cross_midnight(self):
        w = parse_time_condition("between 22:00 and 06:00")
        assert w is not None
        assert w.start_minutes == 22 * 60
        assert w.end_minutes == 6 * 60
        assert w.cross_midnight is True

    def test_between_12h(self):
        w = parse_time_condition("between 10 PM and 6 AM")
        assert w is not None
        assert w.start_minutes == 22 * 60
        assert w.end_minutes == 6 * 60
        assert w.cross_midnight is True

    def test_between_with_minutes_12h(self):
        w = parse_time_condition("between 9:30 AM and 5:15 PM")
        assert w is not None
        assert w.start_minutes == 9 * 60 + 30
        assert w.end_minutes == 17 * 60 + 15
        assert w.cross_midnight is False

    def test_unknown_pattern_returns_none(self):
        assert parse_time_condition("user is idle for 10 minutes") is None

    def test_empty_returns_none(self):
        assert parse_time_condition("") is None

    def test_malformed_returns_none(self):
        assert parse_time_condition("after") is None
        assert parse_time_condition("before") is None
        assert parse_time_condition("between") is None
        assert parse_time_condition("between 9:00") is None

    def test_p_dot_m_dot_notation(self):
        w = parse_time_condition("after 10 P.M.")
        assert w is not None
        assert w.start_minutes == 22 * 60

    def test_a_dot_m_dot_notation(self):
        w = parse_time_condition("before 8 A.M.")
        assert w is not None
        assert w.end_minutes == 8 * 60


# ─── time_in_window ───────────────────────────────────────────────────


class TestTimeInWindow:
    def test_normal_window_inside(self):
        w = TimeWindow(start_minutes=9 * 60, end_minutes=17 * 60)
        assert time_in_window(datetime(2026, 1, 1, 12, 0), w) is True

    def test_normal_window_before(self):
        w = TimeWindow(start_minutes=9 * 60, end_minutes=17 * 60)
        assert time_in_window(datetime(2026, 1, 1, 8, 59), w) is False

    def test_normal_window_after(self):
        w = TimeWindow(start_minutes=9 * 60, end_minutes=17 * 60)
        assert time_in_window(datetime(2026, 1, 1, 17, 0), w) is False

    def test_normal_window_exact_start(self):
        w = TimeWindow(start_minutes=9 * 60, end_minutes=17 * 60)
        assert time_in_window(datetime(2026, 1, 1, 9, 0), w) is True

    def test_cross_midnight_in_start_zone(self):
        w = TimeWindow(start_minutes=22 * 60, end_minutes=6 * 60, cross_midnight=True)
        assert time_in_window(datetime(2026, 1, 1, 23, 30), w) is True

    def test_cross_midnight_in_end_zone(self):
        w = TimeWindow(start_minutes=22 * 60, end_minutes=6 * 60, cross_midnight=True)
        assert time_in_window(datetime(2026, 1, 2, 3, 0), w) is True

    def test_cross_midnight_outside(self):
        w = TimeWindow(start_minutes=22 * 60, end_minutes=6 * 60, cross_midnight=True)
        assert time_in_window(datetime(2026, 1, 2, 10, 0), w) is False

    def test_cross_midnight_exact_boundary_start(self):
        w = TimeWindow(start_minutes=22 * 60, end_minutes=6 * 60, cross_midnight=True)
        assert time_in_window(datetime(2026, 1, 1, 22, 0), w) is True

    def test_cross_midnight_exact_boundary_end(self):
        w = TimeWindow(start_minutes=22 * 60, end_minutes=6 * 60, cross_midnight=True)
        assert time_in_window(datetime(2026, 1, 2, 6, 0), w) is False

    def test_after_window_at_boundary(self):
        w = TimeWindow(start_minutes=22 * 60, end_minutes=24 * 60)
        assert time_in_window(datetime(2026, 1, 1, 22, 0), w) is True
        assert time_in_window(datetime(2026, 1, 1, 23, 59), w) is True
        assert time_in_window(datetime(2026, 1, 1, 21, 59), w) is False

    def test_before_window_at_boundary(self):
        w = TimeWindow(start_minutes=0, end_minutes=8 * 60 + 30)
        assert time_in_window(datetime(2026, 1, 1, 0, 0), w) is True
        assert time_in_window(datetime(2026, 1, 1, 8, 29), w) is True
        assert time_in_window(datetime(2026, 1, 1, 8, 30), w) is False


# ─── RuleEngine.evaluate_time ─────────────────────────────────────────


class TestEvaluateTime:
    def test_matches_enabled_time_rule(self, engine, rule_store):
        rule_store.add(
            "Late night rule",
            rule_type="time",
            condition="after 22:00",
        )
        now = datetime(2026, 1, 1, 23, 0)
        matches = engine.evaluate_time(now)
        assert len(matches) == 1
        assert matches[0].title == "Late night rule"

    def test_skips_disabled_rule(self, engine, rule_store):
        rule_store.add(
            "Disabled rule",
            rule_type="time",
            condition="after 22:00",
            enabled=True,
        )
        engine._rule_store.toggle(1)
        now = datetime(2026, 1, 1, 23, 0)
        assert engine.evaluate_time(now) == []

    def test_skips_non_time_rule(self, engine, rule_store):
        rule_store.add(
            "Screen rule",
            rule_type="screen",
            condition="gaming detected",
        )
        now = datetime(2026, 1, 1, 23, 0)
        assert engine.evaluate_time(now) == []

    def test_skips_unparseable_condition(self, engine, rule_store):
        rule_store.add(
            "Custom time rule",
            rule_type="time",
            condition="user is idle",
        )
        now = datetime(2026, 1, 1, 23, 0)
        assert engine.evaluate_time(now) == []

    def test_respects_cooldown(self, engine, rule_store):
        rule_store.add(
            "Cooldown rule",
            rule_type="time",
            condition="after 22:00",
            cooldown_minutes=60,
        )
        # Simulate last fire 30 minutes ago
        rule_store.record_fire(1)
        # last_fired_at is now; evaluate 30 min later
        now = datetime.now() + timedelta(minutes=30)
        # The rule was just recorded, so it's within cooldown
        matches = engine.evaluate_time(now)
        # last_fired_at was set to datetime.now(), now is 30 min later
        # but cooldown is 60 min, so still in cooldown
        assert len(matches) == 0

    def test_cooldown_expired(self, engine, rule_store):
        rule_store.add(
            "Expired cooldown",
            rule_type="time",
            condition="after 22:00",
            cooldown_minutes=30,
        )
        rule_store.record_fire(1)
        # last_fired_at is ~now; evaluate at 23:00 which is 60+ min later
        # and also inside the "after 22:00" window
        now = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0)
        matches = engine.evaluate_time(now)
        assert len(matches) == 1

    def test_zero_cooldown_always_matches(self, engine, rule_store):
        rule_store.add(
            "No cooldown",
            rule_type="time",
            condition="after 22:00",
            cooldown_minutes=0,
        )
        rule_store.record_fire(1)
        now = datetime(2026, 1, 1, 23, 0)
        matches = engine.evaluate_time(now)
        assert len(matches) == 1

    def test_cross_midnight_matching(self, engine, rule_store):
        rule_store.add(
            "Night owl",
            rule_type="time",
            condition="between 22:00 and 06:00",
        )
        # 23:00 should match
        assert len(engine.evaluate_time(datetime(2026, 1, 1, 23, 0))) == 1
        # 03:00 should match
        assert len(engine.evaluate_time(datetime(2026, 1, 2, 3, 0))) == 1
        # 10:00 should NOT match
        assert len(engine.evaluate_time(datetime(2026, 1, 2, 10, 0))) == 0

    def test_before_matching(self, engine, rule_store):
        rule_store.add(
            "Early bird",
            rule_type="time",
            condition="before 08:30",
        )
        assert len(engine.evaluate_time(datetime(2026, 1, 1, 7, 0))) == 1
        assert len(engine.evaluate_time(datetime(2026, 1, 1, 8, 30))) == 0
        assert len(engine.evaluate_time(datetime(2026, 1, 1, 12, 0))) == 0

    def test_does_not_fire(self, engine, rule_store):
        """evaluate_time returns matches but does NOT update last_fired_at."""
        rule_store.add(
            "Check only",
            rule_type="time",
            condition="after 22:00",
        )
        engine.evaluate_time(datetime(2026, 1, 1, 23, 0))
        rule = rule_store.get(1)
        assert rule.last_fired_at is None


# ─── RuleEngine.tick ──────────────────────────────────────────────────


class TestTick:
    def test_fires_time_rule(self, engine, rule_store):
        rule_store.add(
            "Fire me",
            rule_type="time",
            condition="after 22:00",
            message="Go to sleep!",
        )
        fired = engine.tick(datetime(2026, 1, 1, 23, 0))
        assert len(fired) == 1
        assert fired[0].title == "Fire me"

    def test_records_activity_on_fire(self, engine, rule_store):
        rule_store.add(
            "Activity test",
            rule_type="time",
            condition="after 22:00",
            message="Do it!",
        )
        engine.tick(datetime(2026, 1, 1, 23, 0))
        entries = engine._activity_store.recent(limit=10)
        rule_entries = [e for e in entries if e.action == "rule_fired"]
        assert len(rule_entries) == 1
        assert rule_entries[0].actor == "rule_engine"
        assert "Do it!" in rule_entries[0].detail

    def test_updates_last_fired_at(self, engine, rule_store):
        rule_store.add(
            "Fire once",
            rule_type="time",
            condition="after 22:00",
        )
        engine.tick(datetime(2026, 1, 1, 23, 0))
        rule = rule_store.get(1)
        assert rule.last_fired_at is not None

    def test_cooldown_prevents_double_fire(self, engine, rule_store):
        rule_store.add(
            "Cool rule",
            rule_type="time",
            condition="after 22:00",
            cooldown_minutes=60,
        )
        now = datetime(2026, 1, 1, 23, 0)
        fired1 = engine.tick(now)
        assert len(fired1) == 1
        # Same time — still in cooldown
        fired2 = engine.tick(now)
        assert len(fired2) == 0

    def test_default_message_when_empty(self, engine, rule_store):
        rule_store.add(
            "No message",
            rule_type="time",
            condition="after 22:00",
            message="",
        )
        engine.tick(datetime(2026, 1, 1, 23, 0))
        entries = engine._activity_store.recent(limit=10)
        rule_entries = [e for e in entries if e.action == "rule_fired"]
        assert "Rule triggered: No message" in rule_entries[0].detail

    def test_delivery_called(self, engine, rule_store):
        delivered: list[tuple[str, int]] = []

        def delivery(msg: str, rid: int) -> None:
            delivered.append((msg, rid))

        eng = RuleEngine(rule_store, engine._activity_store, delivery=delivery)
        rule_store.add(
            "Deliver me",
            rule_type="time",
            condition="after 22:00",
            message="Hello!",
        )
        eng.tick(datetime(2026, 1, 1, 23, 0))
        assert len(delivered) == 1
        assert delivered[0][0] == "Hello!"
        assert delivered[0][1] == 1

    def test_allow_delivery_false(self, engine, rule_store):
        delivered: list[tuple[str, int]] = []

        def delivery(msg: str, rid: int) -> None:
            delivered.append((msg, rid))

        eng = RuleEngine(rule_store, engine._activity_store, delivery=delivery)
        rule_store.add(
            "Quiet rule",
            rule_type="time",
            condition="after 22:00",
            message="Shh",
        )
        eng.tick(datetime(2026, 1, 1, 23, 0), allow_delivery=False)
        # Rule still fires (activity recorded) but delivery skipped
        assert len(delivered) == 0
        entries = eng._activity_store.recent(limit=10)
        rule_entries = [e for e in entries if e.action == "rule_fired"]
        assert len(rule_entries) == 1

    def test_async_delivery(self, engine, rule_store):
        delivered: list[tuple[str, int]] = []

        async def async_deliver(msg: str, rid: int) -> None:
            delivered.append((msg, rid))

        def delivery(msg: str, rid: int):
            loop = asyncio.new_event_loop()
            f = asyncio.ensure_future(async_deliver(msg, rid), loop=loop)
            loop.run_until_complete(f)
            return f

        eng = RuleEngine(rule_store, engine._activity_store, delivery=delivery)
        rule_store.add(
            "Async rule",
            rule_type="time",
            condition="after 22:00",
            message="Async!",
        )
        eng.tick(datetime(2026, 1, 1, 23, 0))
        assert len(delivered) == 1
        assert delivered[0][0] == "Async!"

    def test_no_rules_no_fire(self, engine):
        fired = engine.tick(datetime(2026, 1, 1, 12, 0))
        assert fired == []

    def test_screen_context_none_no_screen_fire(self, engine, rule_store):
        """Without screen_context, screen rules are never evaluated."""
        rule_store.add(
            "Screen rule",
            rule_type="screen",
            condition="gaming detected",
        )
        fired = engine.tick(datetime(2026, 1, 1, 12, 0), screen_context=None)
        assert len(fired) == 0

    def test_screen_no_assessor_no_fire(self, engine, rule_store):
        """Without a screen_assessor, screen rules never fire even with context."""
        rule_store.add(
            "Screen rule",
            rule_type="screen",
            condition="gaming detected",
        )
        fired = engine.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="gaming window open",
        )
        assert len(fired) == 0


# ─── Screen rules with assessor ───────────────────────────────────────


class TestScreenRules:
    def _make_assessor(self, assessment: ScreenAssessment | None):
        class FixedAssessor:
            def assess(self, screen_context: str, candidate_rule_ids: list[int]):
                return assessment

        return FixedAssessor()

    def test_grounded_assessment_fires(self, rule_store, activity_store):
        rule_store.add(
            "Gaming rule",
            rule_type="screen",
            condition="no gaming during work",
            message="Stop gaming!",
        )
        assessor = self._make_assessor(ScreenAssessment(rule_id=1, confidence=0.9))
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        fired = eng.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="game window visible",
        )
        assert len(fired) == 1
        assert fired[0].title == "Gaming rule"

    def test_invented_rule_id_ignored(self, rule_store, activity_store):
        rule_store.add(
            "Real rule",
            rule_type="screen",
            condition="no gaming",
        )
        # Assessor returns an ID that doesn't exist
        assessor = self._make_assessor(ScreenAssessment(rule_id=9999, confidence=1.0))
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        fired = eng.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="something",
        )
        assert len(fired) == 0

    def test_zero_confidence_ignored(self, rule_store, activity_store):
        rule_store.add(
            "Screen rule",
            rule_type="screen",
            condition="no gaming",
        )
        assessor = self._make_assessor(ScreenAssessment(rule_id=1, confidence=0.0))
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        fired = eng.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="something",
        )
        assert len(fired) == 0

    def test_assessor_returns_none(self, rule_store, activity_store):
        rule_store.add(
            "Screen rule",
            rule_type="screen",
            condition="no gaming",
        )
        assessor = self._make_assessor(None)
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        fired = eng.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="browsing news",
        )
        assert len(fired) == 0

    def test_screen_rule_cooldown(self, rule_store, activity_store):
        rule_store.add(
            "Screen cooldown",
            rule_type="screen",
            condition="no gaming",
            cooldown_minutes=30,
        )
        assessor = self._make_assessor(ScreenAssessment(rule_id=1, confidence=0.9))
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        now = datetime(2026, 1, 1, 12, 0)
        fired1 = eng.tick(now, screen_context="game")
        assert len(fired1) == 1
        fired2 = eng.tick(now, screen_context="game")
        assert len(fired2) == 0

    def test_screen_rule_disabled_ignored(self, rule_store, activity_store):
        rule_store.add(
            "Disabled screen",
            rule_type="screen",
            condition="no gaming",
            enabled=True,
        )
        rule_store.toggle(1)
        assessor = self._make_assessor(ScreenAssessment(rule_id=1, confidence=0.9))
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        fired = eng.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="game",
        )
        assert len(fired) == 0

    def test_assessor_wrong_type_ignored(self, rule_store, activity_store):
        """Assessor returns a rule_id that is not a screen rule."""
        rule_store.add(
            "Time rule",
            rule_type="time",
            condition="after 22:00",
        )
        assessor = self._make_assessor(ScreenAssessment(rule_id=1, confidence=0.9))
        eng = RuleEngine(rule_store, activity_store, screen_assessor=assessor)
        fired = eng.tick(
            datetime(2026, 1, 1, 12, 0),
            screen_context="game",
        )
        assert len(fired) == 0


# ─── Validation helpers ───────────────────────────────────────────────


class TestValidation:
    def test_empty_title_raises(self):
        with pytest.raises(ValueError, match="title"):
            validate_add_rule("", rule_type="time", condition="after 22:00")

    def test_whitespace_title_raises(self):
        with pytest.raises(ValueError, match="title"):
            validate_add_rule("   ", rule_type="time", condition="after 22:00")

    def test_empty_condition_raises(self):
        with pytest.raises(ValueError, match="condition"):
            validate_add_rule("My rule", rule_type="time", condition="")

    def test_whitespace_condition_raises(self):
        with pytest.raises(ValueError, match="condition"):
            validate_add_rule("My rule", rule_type="time", condition="  ")

    def test_negative_cooldown_raises(self):
        with pytest.raises(ValueError, match="cooldown"):
            validate_add_rule("My rule", rule_type="time", condition="after 22:00", cooldown_minutes=-5)

    def test_invalid_rule_type_raises(self):
        with pytest.raises(ValueError, match="rule_type"):
            validate_add_rule("My rule", rule_type="invalid", condition="after 22:00")

    def test_valid_params_pass(self):
        validate_add_rule("My rule", rule_type="time", condition="after 22:00", cooldown_minutes=0)
        validate_add_rule("My rule", rule_type="screen", condition="gaming", cooldown_minutes=60)
        validate_add_rule("My rule", rule_type="custom", condition="anything", cooldown_minutes=0)

    def test_update_empty_title_raises(self):
        with pytest.raises(ValueError, match="title"):
            validate_update_rule(title="")

    def test_update_empty_condition_raises(self):
        with pytest.raises(ValueError, match="condition"):
            validate_update_rule(condition="")

    def test_update_negative_cooldown_raises(self):
        with pytest.raises(ValueError, match="cooldown"):
            validate_update_rule(cooldown_minutes=-1)

    def test_update_invalid_rule_type_raises(self):
        with pytest.raises(ValueError, match="rule_type"):
            validate_update_rule(rule_type="bogus")

    def test_update_none_fields_pass(self):
        validate_update_rule()  # all None — no-op

    def test_update_valid_fields_pass(self):
        validate_update_rule(title="New name", cooldown_minutes=120)


# ─── Rule store CRUD (add/edit/toggle/delete) ─────────────────────────


class TestRuleStoreCRUD:
    def test_add_rule(self, rule_store):
        r = rule_store.add("Test rule", rule_type="time", condition="after 22:00")
        assert r.title == "Test rule"
        assert r.rule_type == "time"
        assert r.enabled is True

    def test_update_rule(self, rule_store):
        r = rule_store.add("Old", rule_type="time", condition="after 22:00")
        updated = rule_store.update(r.id, title="New", cooldown_minutes=45)
        assert updated.title == "New"
        assert updated.cooldown_minutes == 45

    def test_toggle_rule(self, rule_store):
        r = rule_store.add("Toggle", rule_type="time", condition="after 22:00")
        assert r.enabled
        toggled = rule_store.toggle(r.id)
        assert toggled.enabled is False
        toggled2 = rule_store.toggle(r.id)
        assert toggled2.enabled is True

    def test_delete_rule(self, rule_store):
        r = rule_store.add("Delete me", rule_type="time", condition="after 22:00")
        rule_store.delete(r.id)
        with pytest.raises(KeyError):
            rule_store.get(r.id)

    def test_delete_missing_raises(self, rule_store):
        with pytest.raises(KeyError):
            rule_store.delete(9999)


# ─── Idempotent tick through cooldown ─────────────────────────────────


class TestIdempotentTick:
    def test_tick_twice_same_second(self, engine, rule_store):
        rule_store.add(
            "Idempotent",
            rule_type="time",
            condition="after 22:00",
            cooldown_minutes=30,
        )
        now = datetime(2026, 1, 1, 23, 0)
        assert len(engine.tick(now)) == 1
        assert len(engine.tick(now)) == 0

    def test_tick_after_cooldown_expires(self, engine, rule_store):
        rule_store.add(
            "Re-fire",
            rule_type="time",
            condition="after 22:00",
            cooldown_minutes=1,
        )
        now = datetime(2026, 1, 1, 23, 0)
        assert len(engine.tick(now)) == 1
        later = now + timedelta(minutes=2)
        assert len(engine.tick(later)) == 1
