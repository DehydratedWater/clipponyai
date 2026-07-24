"""Tests for the proactive focus/distraction awareness system.

Covers:
- ScreenAssessment parsing and validation
- AwarenessConfig validation
- AwarenessMonitor opt-in gates (screenshot + awareness enabled)
- Cooldown enforcement across monitor restarts
- Work-hours context in assessment
- Screenshot/assessor failure handling
- Monitor lifecycle (start/stop)
- Settings roundtrip (Config -> form -> apply -> Config)
- Headless behavior (no screenshots = no scanning)

No live model, no real screenshots, no real services.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from clipponyai.awareness import (
    AwarenessMonitor,
    PonyBrainAssessor,
    ScreenAssessment,
    _META_LAST_ALERT,
    parse_assessment,
)
from clipponyai.config import AwarenessConfig, Config
from clipponyai.scheduler import in_work_hours
from clipponyai.settings_apply import (
    SettingsForm,
    apply_to_config,
    read_form,
    validate,
)
from clipponyai.tasks import TaskStore


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def awareness_config():
    return AwarenessConfig()


@pytest.fixture
def fake_screenshot():
    return b"\x89PNG fake screenshot bytes"


@pytest.fixture
def delivered():
    return []


class FakeClock:
    """Injectable clock returning a fixed time."""

    def __init__(self, base: datetime | None = None):
        self._now = base or datetime(2026, 7, 22, 10, 0)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeAssessor:
    """Scriptable assessor returning a fixed ScreenAssessment."""

    def __init__(self, assessment: ScreenAssessment | None = None):
        self.assessment = assessment or ScreenAssessment(
            should_interrupt=False, confidence=0.9, reason="all clear"
        )
        self.calls = []
        self._fail_next = False

    def assess(self, screenshot_bytes, *, work_hours_status, task_overview, focus_policy):
        self.calls.append({
            "work_hours_status": work_hours_status,
            "task_overview": task_overview,
            "focus_policy": focus_policy,
        })
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("assessor down")
        return self.assessment

    def fail_once(self) -> None:
        self._fail_next = True


# ── parse_assessment: strict JSON/type/range validation ───────────────


class TestParseAssessment:
    def _result(self, structured):
        r = MagicMock()
        r.structured = structured
        r.error = None
        return r

    def test_valid_interrupt(self):
        r = self._result({"should_interrupt": True, "confidence": 0.95, "reason": "TikTok"})
        a = parse_assessment(r)
        assert a.should_interrupt is True
        assert a.confidence == 0.95
        assert a.reason == "TikTok"

    def test_valid_no_interrupt(self):
        r = self._result({"should_interrupt": False, "confidence": 0.8, "reason": "working"})
        a = parse_assessment(r)
        assert a.should_interrupt is False

    def test_confidence_int_coerced_to_float(self):
        r = self._result({"should_interrupt": False, "confidence": 1, "reason": "ok"})
        a = parse_assessment(r)
        assert isinstance(a.confidence, float)
        assert a.confidence == 1.0

    def test_missing_should_interrupt(self):
        r = self._result({"confidence": 0.9, "reason": "ok"})
        with pytest.raises(ValueError, match="should_interrupt"):
            parse_assessment(r)

    def test_should_interrupt_wrong_type(self):
        r = self._result({"should_interrupt": "yes", "confidence": 0.9, "reason": "ok"})
        with pytest.raises(ValueError, match="should_interrupt"):
            parse_assessment(r)

    def test_confidence_wrong_type(self):
        r = self._result({"should_interrupt": True, "confidence": "high", "reason": "ok"})
        with pytest.raises(ValueError, match="confidence"):
            parse_assessment(r)

    def test_confidence_out_of_range_high(self):
        r = self._result({"should_interrupt": True, "confidence": 1.5, "reason": "ok"})
        with pytest.raises(ValueError, match="confidence"):
            parse_assessment(r)

    def test_confidence_out_of_range_negative(self):
        r = self._result({"should_interrupt": True, "confidence": -0.1, "reason": "ok"})
        with pytest.raises(ValueError, match="confidence"):
            parse_assessment(r)

    def test_reason_wrong_type(self):
        r = self._result({"should_interrupt": True, "confidence": 0.9, "reason": 123})
        with pytest.raises(ValueError, match="reason"):
            parse_assessment(r)

    def test_non_dict_structured(self):
        r = self._result("not a dict")
        with pytest.raises(ValueError, match="expected dict"):
            parse_assessment(r)

    def test_none_structured(self):
        r = self._result(None)
        with pytest.raises(ValueError, match="expected dict"):
            parse_assessment(r)

    def test_empty_dict(self):
        r = self._result({})
        with pytest.raises(ValueError):
            parse_assessment(r)

    def test_boundary_confidence_zero(self):
        r = self._result({"should_interrupt": False, "confidence": 0.0, "reason": "guessing"})
        a = parse_assessment(r)
        assert a.confidence == 0.0

    def test_boundary_confidence_one(self):
        r = self._result({"should_interrupt": True, "confidence": 1.0, "reason": "certain"})
        a = parse_assessment(r)
        assert a.confidence == 1.0


# ── AwarenessConfig validation ────────────────────────────────────────


class TestAwarenessConfig:
    def test_defaults_off(self):
        c = AwarenessConfig()
        assert c.enabled is False
        assert c.interval_seconds == 120
        assert c.cooldown_minutes == 30
        assert c.minimum_confidence == 0.7

    def test_interval_too_small(self):
        with pytest.raises(Exception, match="30"):
            AwarenessConfig(interval_seconds=10)

    def test_interval_too_large(self):
        with pytest.raises(Exception, match="3600"):
            AwarenessConfig(interval_seconds=7200)

    def test_interval_boundary_ok(self):
        AwarenessConfig(interval_seconds=30)
        AwarenessConfig(interval_seconds=3600)

    def test_cooldown_too_small(self):
        with pytest.raises(Exception, match="5"):
            AwarenessConfig(cooldown_minutes=1)

    def test_cooldown_too_large(self):
        with pytest.raises(Exception, match="480"):
            AwarenessConfig(cooldown_minutes=600)

    def test_cooldown_boundary_ok(self):
        AwarenessConfig(cooldown_minutes=5)
        AwarenessConfig(cooldown_minutes=480)

    def test_confidence_out_of_range(self):
        with pytest.raises(Exception):
            AwarenessConfig(minimum_confidence=-0.1)
        with pytest.raises(Exception):
            AwarenessConfig(minimum_confidence=1.5)

    def test_confidence_boundary_ok(self):
        AwarenessConfig(minimum_confidence=0.0)
        AwarenessConfig(minimum_confidence=1.0)

    def test_focus_policy_default(self):
        c = AwarenessConfig()
        assert "social media" in c.focus_policy.lower()

    def test_focus_policy_editable(self):
        c = AwarenessConfig(focus_policy="only interrupt for cat videos")
        assert c.focus_policy == "only interrupt for cat videos"


# ── opt-in gates ──────────────────────────────────────────────────────


class TestOptInGates:
    async def test_both_disabled(self, config, store, fake_screenshot, delivered):
        config.awareness.enabled = False
        config.screenshot_enabled = False
        assessor = FakeAssessor()
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        assert monitor._task is None  # never launched
        await monitor.stop()

    async def test_awareness_only(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = False
        assessor = FakeAssessor()
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        assert monitor._task is None
        await monitor.stop()

    async def test_screenshot_only(self, config, store, fake_screenshot):
        config.awareness.enabled = False
        config.screenshot_enabled = True
        assessor = FakeAssessor()
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        assert monitor._task is None
        await monitor.stop()

    async def test_headless_no_screenshot_fn(self, config, store):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessor = FakeAssessor()
        monitor = AwarenessMonitor(
            config, None, assessor, store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        assert monitor._task is None
        await monitor.stop()

    async def test_both_enabled_starts(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessor = FakeAssessor()
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        assert monitor._task is not None
        await monitor.stop()


# ── cooldown across monitor restarts ──────────────────────────────────


class TestCooldownPersistence:
    async def test_cooldown_prevents_repeat(self, config, store, fake_screenshot, delivered):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.awareness.cooldown_minutes = 30
        clock = FakeClock(datetime(2026, 7, 22, 10, 0))
        assessment = ScreenAssessment(True, 0.95, "TikTok detected")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()

        # First tick: should fire
        await monitor._tick()
        assert deliver_mock.call_count == 1
        assert "TikTok" in deliver_mock.call_args[0][0]

        # Second tick immediately: cooldown blocks
        await monitor._tick()
        assert deliver_mock.call_count == 1  # no second call

        await monitor.stop()

    async def test_cooldown_survives_restart(self, config, store, fake_screenshot, delivered):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.awareness.cooldown_minutes = 30
        clock = FakeClock(datetime(2026, 7, 22, 10, 0))
        assessment = ScreenAssessment(True, 0.95, "TikTok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        # First monitor instance
        monitor1 = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor1.start()
        await monitor1._tick()  # fires
        assert deliver_mock.call_count == 1
        await monitor1.stop()

        # New monitor instance (simulates restart)
        assessor2 = FakeAssessor(assessment)
        deliver_mock2 = AsyncMock()
        monitor2 = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor2, store,
            deliver_mock2, clock=clock,
        )
        await monitor2.start()
        await monitor2._tick()  # still in cooldown
        assert deliver_mock2.call_count == 0
        await monitor2.stop()

    async def test_cooldown_expires(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.awareness.cooldown_minutes = 15
        clock = FakeClock(datetime(2026, 7, 22, 10, 0))
        assessment = ScreenAssessment(True, 0.95, "TikTok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()  # first alert
        assert deliver_mock.call_count == 1

        # Advance past cooldown
        clock.advance(timedelta(minutes=16))
        await monitor._tick()  # cooldown expired, fires again
        assert deliver_mock.call_count == 2

        await monitor.stop()

    async def test_meta_key_format(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        clock = FakeClock(datetime(2026, 7, 22, 10, 0))
        assessment = ScreenAssessment(True, 0.95, "test")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()

        # Meta table should have the timestamp
        meta_val = store.get_meta(_META_LAST_ALERT)
        assert meta_val is not None
        assert float(meta_val) == clock.now().timestamp()

        await monitor.stop()


# ── work-hours context ────────────────────────────────────────────────


class TestWorkHoursContext:
    async def test_work_hours_status_included(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.reminders.work_hours.enabled = True
        config.reminders.work_hours.start = "09:00"
        config.reminders.work_hours.end = "17:00"
        config.reminders.work_hours.weekdays = [0, 1, 2, 3, 4]

        clock = FakeClock(datetime(2026, 7, 22, 10, 0))  # Wednesday 10:00
        assessment = ScreenAssessment(False, 0.9, "focused")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()
        await monitor.stop()

        assert len(assessor.calls) == 1
        assert "inside work hours" in assessor.calls[0]["work_hours_status"].lower()

    async def test_outside_work_hours(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.reminders.work_hours.enabled = True
        config.reminders.work_hours.start = "09:00"
        config.reminders.work_hours.end = "17:00"
        config.reminders.work_hours.weekdays = [0, 1, 2, 3, 4]

        clock = FakeClock(datetime(2026, 7, 22, 20, 0))  # Wednesday 20:00
        assessment = ScreenAssessment(False, 0.9, "after hours")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()
        await monitor.stop()

        assert len(assessor.calls) == 1
        assert "outside work hours" in assessor.calls[0]["work_hours_status"].lower()

    async def test_no_work_hours_configured(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.reminders.work_hours.enabled = False

        clock = FakeClock()
        assessment = ScreenAssessment(False, 0.9, "ok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()
        await monitor.stop()

        assert "not configured" in assessor.calls[0]["work_hours_status"].lower()

    async def test_task_overview_passed(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        store.add("finish report", deadline=datetime(2026, 7, 22, 12, 0))

        clock = FakeClock()
        assessment = ScreenAssessment(False, 0.9, "ok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()
        await monitor.stop()

        assert "finish report" in assessor.calls[0]["task_overview"]

    async def test_focus_policy_passed(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.awareness.focus_policy = "never interrupt for YouTube"

        clock = FakeClock()
        assessment = ScreenAssessment(False, 0.9, "ok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=clock,
        )
        await monitor.start()
        await monitor._tick()
        await monitor.stop()

        assert assessor.calls[0]["focus_policy"] == "never interrupt for YouTube"


# ── failure handling ──────────────────────────────────────────────────


class TestFailureHandling:
    async def test_screenshot_failure_skips_silently(self, config, store):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessment = ScreenAssessment(True, 0.95, "TikTok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: None,  # always fails
            assessor, store, deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        await monitor._tick()
        assert deliver_mock.call_count == 0
        assert len(assessor.calls) == 0  # assessor never called
        await monitor.stop()

    async def test_assessor_failure_skips_silently(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessor = FakeAssessor(ScreenAssessment(True, 0.95, "TikTok"))
        assessor.fail_once()
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        await monitor._tick()
        assert deliver_mock.call_count == 0
        await monitor.stop()

    async def test_low_confidence_skips(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.awareness.minimum_confidence = 0.8
        assessment = ScreenAssessment(True, 0.5, "maybe TikTok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        await monitor._tick()
        assert deliver_mock.call_count == 0
        await monitor.stop()

    async def test_no_interrupt_skips(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessment = ScreenAssessment(False, 0.95, "user is focused")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        await monitor._tick()
        assert deliver_mock.call_count == 0
        await monitor.stop()

    async def test_corrupted_meta_doesnt_crash(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        config.awareness.cooldown_minutes = 30
        store.set_meta(_META_LAST_ALERT, "not_a_number")
        assessment = ScreenAssessment(True, 0.95, "TikTok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        # Should not crash despite corrupted meta
        await monitor._tick()
        # Should fire because corrupted meta is treated as no cooldown
        assert deliver_mock.call_count == 1
        await monitor.stop()


# ── lifecycle ─────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_start_stop_no_error(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, FakeAssessor(), store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        assert monitor._task is not None
        await monitor.stop()
        assert monitor._task is None

    async def test_stop_is_idempotent(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, FakeAssessor(), store,
            AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        await monitor.stop()
        await monitor.stop()  # second stop should not raise

    async def test_start_without_gates_is_idempotent(self, config, store):
        config.awareness.enabled = False
        config.screenshot_enabled = False
        monitor = AwarenessMonitor(
            config, None, FakeAssessor(), store, AsyncMock(), clock=FakeClock(),
        )
        await monitor.start()
        await monitor.start()  # no-op
        await monitor.stop()

    async def test_config_change_during_loop(self, config, store, fake_screenshot):
        """If awareness is disabled mid-loop, the next tick is a no-op."""
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessment = ScreenAssessment(True, 0.95, "TikTok")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        await monitor._tick()  # fires
        assert deliver_mock.call_count == 1

        # Disable awareness mid-loop
        config.awareness.enabled = False
        await monitor._tick()  # should be no-op
        assert deliver_mock.call_count == 1

        await monitor.stop()

    async def test_interrupt_message_format(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        assessment = ScreenAssessment(True, 0.95, "You're on TikTok during work hours")
        assessor = FakeAssessor(assessment)
        deliver_mock = AsyncMock()

        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store,
            deliver_mock, clock=FakeClock(),
        )
        await monitor.start()
        await monitor._tick()
        await monitor.stop()

        msg = deliver_mock.call_args[0][0]
        assert msg.startswith("\U0001f434 ")  # horse emoji
        assert "TikTok" in msg


# ── transparent activity audit ────────────────────────────────────────


class TestAwarenessActivityAudit:
    def _activity(self, store):
        from clipponyai.accountability import get_stores

        return get_stores(store)["activity"]

    async def test_records_non_intervention(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        activity = self._activity(store)
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot,
            FakeAssessor(ScreenAssessment(False, 0.93, "focused work")),
            store, AsyncMock(), clock=FakeClock(), activity_store=activity,
        )
        await monitor._tick()
        row = activity.recent()[-1]
        assert row.action == "screen_assessed"
        assert "no interrupt" in row.detail
        assert "0.93" in row.detail

    async def test_records_actual_intervention(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        activity = self._activity(store)
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot,
            FakeAssessor(ScreenAssessment(True, 0.95, "rule breached")),
            store, AsyncMock(), clock=FakeClock(), activity_store=activity,
        )
        await monitor._tick()
        entries = activity.recent()
        assessment = next(e for e in entries if e.action == "screen_assessed")
        assert "intervened" in assessment.detail
        assert any(e.action == "awareness_intervention" for e in entries)

    async def test_records_assessment_failure(self, config, store, fake_screenshot):
        config.awareness.enabled = True
        config.screenshot_enabled = True
        activity = self._activity(store)
        assessor = FakeAssessor()
        assessor.fail_once()
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, assessor, store, AsyncMock(),
            clock=FakeClock(), activity_store=activity,
        )
        await monitor._tick()
        row = activity.recent()[-1]
        assert row.action == "screen_assessment_failed"
        assert "RuntimeError" in row.detail

    async def test_disabled_does_not_log(self, config, store, fake_screenshot):
        activity = self._activity(store)
        monitor = AwarenessMonitor(
            config, lambda: fake_screenshot, FakeAssessor(), store, AsyncMock(),
            clock=FakeClock(), activity_store=activity,
        )
        await monitor._tick()
        assert activity.recent() == []


# ── PonyBrainAssessor (with fake brain) ───────────────────────────────


class TestPonyBrainAssessor:
    def test_assessor_sends_image_to_vision_lane(self, config, store):
        from clipponyai.brain import PonyBrain

        clients = []

        def factory(spec):
            from conftest import FakeClient

            client = FakeClient(spec, {
                "pony-vision": {"should_interrupt": True, "confidence": 0.9, "reason": "TikTok"},
            })
            clients.append(client)
            return client

        brain = PonyBrain(config, store, client_factory=factory)
        assessor = PonyBrainAssessor(brain)

        result = assessor.assess(
            b"\x89PNG fake",
            work_hours_status="inside work hours",
            task_overview="(none)",
            focus_policy="interrupt on social media",
        )

        assert result.should_interrupt is True
        assert result.confidence == 0.9
        assert result.reason == "TikTok"

        # Verify the vision lane received an image
        vision_clients = [c for c in clients if c.spec.agent_id == "pony-vision"]
        assert len(vision_clients) == 1
        call = vision_clients[0].calls[0]
        rich_messages = [
            message["content"]
            for message in call["messages"]
            if isinstance(message.get("content"), list)
        ]
        assert any(
            part.get("type") == "image_url"
            for content in rich_messages
            for part in content
        )

    def test_assessor_rejects_invalid_output(self, config, store):
        from clipponyai.brain import PonyBrain

        def factory(spec):
            from conftest import FakeClient

            return FakeClient(spec, {
                "pony-vision": {"bad": "output"},  # missing required fields
            })

        brain = PonyBrain(config, store, client_factory=factory)
        assessor = PonyBrainAssessor(brain)

        with pytest.raises(ValueError):
            assessor.assess(
                b"\x89PNG fake",
                work_hours_status="inside work hours",
                task_overview="(none)",
                focus_policy="interrupt on social media",
            )


# ── settings roundtrip ────────────────────────────────────────────────


class TestSettingsRoundtrip:
    def _providers(self):
        return sorted(Config().llm.providers)

    def test_read_form_includes_awareness_defaults(self):
        config = Config()
        form = read_form(config)
        assert form.awareness_enabled is False
        assert form.awareness_interval_seconds == 120
        assert form.awareness_cooldown_minutes == 30
        assert form.awareness_minimum_confidence == 0.7
        assert "social media" in form.awareness_focus_policy.lower()

    def test_read_form_custom_awareness(self):
        config = Config()
        config.awareness.enabled = True
        config.awareness.interval_seconds = 60
        config.awareness.cooldown_minutes = 60
        config.awareness.minimum_confidence = 0.5
        config.awareness.focus_policy = "custom policy text"

        form = read_form(config)
        assert form.awareness_enabled is True
        assert form.awareness_interval_seconds == 60
        assert form.awareness_cooldown_minutes == 60
        assert form.awareness_minimum_confidence == 0.5
        assert form.awareness_focus_policy == "custom policy text"

    def test_apply_to_config_awareness(self):
        config = Config()
        form = read_form(config)
        form.awareness_enabled = True
        form.awareness_interval_seconds = 90
        form.awareness_cooldown_minutes = 45
        form.awareness_minimum_confidence = 0.8
        form.awareness_focus_policy = "new policy"

        apply_to_config(form, config)
        assert config.awareness.enabled is True
        assert config.awareness.interval_seconds == 90
        assert config.awareness.cooldown_minutes == 45
        assert config.awareness.minimum_confidence == 0.8
        assert config.awareness.focus_policy == "new policy"

    def test_full_awareness_roundtrip(self, tmp_path):
        config = Config()
        config.awareness.enabled = True
        config.awareness.interval_seconds = 180
        config.awareness.cooldown_minutes = 60
        config.awareness.minimum_confidence = 0.85
        config.awareness.focus_policy = "never interrupt on Monday"

        path = tmp_path / "config.yaml"
        config.save(path)

        loaded = Config.load(path)
        assert loaded.awareness.enabled is True
        assert loaded.awareness.interval_seconds == 180
        assert loaded.awareness.cooldown_minutes == 60
        assert loaded.awareness.minimum_confidence == 0.85
        assert loaded.awareness.focus_policy == "never interrupt on Monday"

    def test_validate_awareness_interval_too_small(self):
        form = SettingsForm(awareness_interval_seconds=10)
        errors = validate(form, available_providers=self._providers())
        assert any("awareness" in e.lower() and "interval" in e.lower() for e in errors)

    def test_validate_awareness_interval_too_large(self):
        form = SettingsForm(awareness_interval_seconds=7200)
        errors = validate(form, available_providers=self._providers())
        assert any("awareness" in e.lower() and "interval" in e.lower() for e in errors)

    def test_validate_awareness_cooldown_too_small(self):
        form = SettingsForm(awareness_cooldown_minutes=1)
        errors = validate(form, available_providers=self._providers())
        assert any("awareness" in e.lower() and "cooldown" in e.lower() for e in errors)

    def test_validate_awareness_cooldown_too_large(self):
        form = SettingsForm(awareness_cooldown_minutes=600)
        errors = validate(form, available_providers=self._providers())
        assert any("awareness" in e.lower() and "cooldown" in e.lower() for e in errors)

    def test_validate_awareness_confidence_bad(self):
        form = SettingsForm(awareness_minimum_confidence=-0.5)
        errors = validate(form, available_providers=self._providers())
        assert any("awareness" in e.lower() and "confidence" in e.lower() for e in errors)

    def test_validate_awareness_valid(self):
        form = SettingsForm(
            awareness_enabled=True,
            awareness_interval_seconds=60,
            awareness_cooldown_minutes=15,
            awareness_minimum_confidence=0.5,
            awareness_focus_policy="test policy",
        )
        errors = validate(form, available_providers=self._providers())
        assert not any("awareness" in e.lower() for e in errors)

    def test_detect_changes_awareness(self):
        old = SettingsForm()
        new = SettingsForm(awareness_enabled=True)
        from clipponyai.settings_apply import detect_changes
        assert "awareness_enabled" in detect_changes(old, new)

    def test_form_modify_apply_save_awareness(self, tmp_path):
        config = Config()
        form = read_form(config)
        form.awareness_enabled = True
        form.awareness_interval_seconds = 90
        form.awareness_cooldown_minutes = 45
        form.awareness_minimum_confidence = 0.8
        form.awareness_focus_policy = "focus policy from settings"

        errors = validate(form, available_providers=sorted(config.llm.providers))
        assert errors == []

        apply_to_config(form, config)
        path = tmp_path / "config.yaml"
        config.save(path)

        loaded = Config.load(path)
        assert loaded.awareness.enabled is True
        assert loaded.awareness.interval_seconds == 90
        assert loaded.awareness.cooldown_minutes == 45
        assert loaded.awareness.minimum_confidence == 0.8
        assert loaded.awareness.focus_policy == "focus policy from settings"


# ── in_work_hours pure function tests ─────────────────────────────────


class TestInWorkHours:
    def test_inside_work_hours(self):
        wh = type("WH", (), {
            "enabled": True, "start": "09:00", "end": "17:00", "weekdays": [0, 1, 2, 3, 4],
        })()
        now = datetime(2026, 7, 22, 12, 0)  # Wednesday noon
        assert in_work_hours(now, wh)

    def test_outside_work_hours(self):
        wh = type("WH", (), {
            "enabled": True, "start": "09:00", "end": "17:00", "weekdays": [0, 1, 2, 3, 4],
        })()
        now = datetime(2026, 7, 22, 20, 0)  # Wednesday evening
        assert not in_work_hours(now, wh)

    def test_weekend_outside(self):
        wh = type("WH", (), {
            "enabled": True, "start": "09:00", "end": "17:00", "weekdays": [0, 1, 2, 3, 4],
        })()
        now = datetime(2026, 7, 25, 12, 0)  # Saturday
        assert not in_work_hours(now, wh)

    def test_disabled(self):
        wh = type("WH", (), {
            "enabled": False, "start": "09:00", "end": "17:00", "weekdays": [0, 1, 2, 3, 4],
        })()
        now = datetime(2026, 7, 22, 12, 0)
        assert not in_work_hours(now, wh)
