# ruff: noqa: DTZ001, DTZ005
"""Tests for proactive context-gap questions.

Covers:
- All gates independently (config, onboarding, quiet hours, silence, gap, agenda, tick)
- Exact 4h boundary
- Silence/resume
- One batch max
- No repeat within gap
- Agenda/routine gate
- Scheduler last-only
- Deterministic questions based on missing context
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from clipponyai.accountability import get_stores
from clipponyai.config import ProactiveQuestionsConfig
from clipponyai.context_questions import ProactiveQuestioner
from clipponyai.onboarding import OnboardingManager
from clipponyai.tasks import TaskStore


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "test.db")
    get_stores(s)  # ensure schema
    yield s
    s.close()


@pytest.fixture
def config():
    return ProactiveQuestionsConfig()


@pytest.fixture
def mgr(store):
    return OnboardingManager(store)


@pytest.fixture
def questioner(store, config, mgr):
    return ProactiveQuestioner(
        config=config,
        store=store,
        onboarding=mgr,
        quiet_hours_start=23,
        quiet_hours_end=8,
    )


NOW = datetime(2026, 7, 22, 14, 0)
NIGHT = datetime(2026, 7, 22, 23, 30)


class TestConfigGate:
    async def test_disabled_config(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        questioner.config.enabled = False
        result = await questioner.tick(NOW)
        assert result is None

    async def test_enabled_config(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        # Should produce questions since no routines/goals/rules exist
        assert result is not None


class TestOnboardingGate:
    async def test_blocks_when_new(self, questioner):
        result = await questioner.tick(NOW)
        assert result is None

    async def test_blocks_when_in_progress(self, questioner, mgr):
        mgr.begin()
        result = await questioner.tick(NOW)
        assert result is None

    async def test_allows_when_completed(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        assert result is not None

    async def test_allows_when_skipped(self, questioner, mgr):
        mgr.skip()
        result = await questioner.tick(NOW)
        assert result is not None


class TestQuietHoursGate:
    async def test_blocks_during_night(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NIGHT)
        assert result is None

    async def test_allows_during_day(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        assert result is not None


class TestSilenceGate:
    async def test_blocks_when_silenced(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        questioner.silence(24)
        result = await questioner.tick(NOW)
        assert result is None

    async def test_allows_after_silence_expires(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        # Set silence to expire 1 hour ago
        until = NOW - timedelta(hours=1)
        from clipponyai.tasks import ISO
        questioner.store.set_meta("proactive_silence_until", until.strftime(ISO))
        result = await questioner.tick(NOW)
        assert result is not None

    async def test_resume_clears_silence(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        questioner.silence(24)
        assert questioner.is_silenced(NOW)
        questioner.resume()
        assert not questioner.is_silenced(NOW)

    async def test_silenced_until_returns_datetime(self, questioner):
        questioner.silence(24)
        until = questioner.silenced_until()
        assert until is not None
        assert isinstance(until, datetime)

    async def test_silenced_until_none_by_default(self, questioner):
        assert questioner.silenced_until() is None


class TestMinGapGate:
    async def test_blocks_within_gap(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        # Set last_asked to 2 hours ago (less than 4h default)
        from clipponyai.tasks import ISO
        two_hours_ago = NOW - timedelta(hours=2)
        questioner.store.set_meta("proactive_last_asked", two_hours_ago.strftime(ISO))
        result = await questioner.tick(NOW)
        assert result is None

    async def test_allows_at_exact_boundary(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        from clipponyai.tasks import ISO
        four_hours_ago = NOW - timedelta(hours=4)
        questioner.store.set_meta("proactive_last_asked", four_hours_ago.strftime(ISO))
        result = await questioner.tick(NOW)
        assert result is not None

    async def test_allows_after_gap(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        from clipponyai.tasks import ISO
        five_hours_ago = NOW - timedelta(hours=5)
        questioner.store.set_meta("proactive_last_asked", five_hours_ago.strftime(ISO))
        result = await questioner.tick(NOW)
        assert result is not None

    async def test_custom_gap_hours(self, store, mgr, config):
        config.min_gap_hours = 6
        q = ProactiveQuestioner(config, store, mgr)
        mgr.begin()
        mgr.complete()
        from clipponyai.tasks import ISO
        five_hours_ago = NOW - timedelta(hours=5)
        store.set_meta("proactive_last_asked", five_hours_ago.strftime(ISO))
        result = await q.tick(NOW)
        assert result is None  # 5h < 6h gap


class TestDeliveredThisTickGate:
    async def test_blocks_when_other_delivered(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        questioner.mark_delivered_this_tick()
        result = await questioner.tick(NOW)
        assert result is None

    async def test_clears_each_tick(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        questioner.mark_delivered_this_tick()
        questioner.clear_tick()
        result = await questioner.tick(NOW)
        assert result is not None


class TestAgendaGate:
    async def test_blocks_with_pending_tasks(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        questioner.store.add("pending task")
        result = await questioner.tick(NOW)
        assert result is None

    async def test_allows_with_no_tasks(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        assert result is not None

    async def test_require_empty_agenda_false(self, store, mgr, config):
        config.require_empty_agenda = False
        q = ProactiveQuestioner(config, store, mgr)
        mgr.begin()
        mgr.complete()
        store.add("pending task")
        result = await q.tick(NOW)
        # Should ask even with tasks when require_empty_agenda is False
        assert result is not None


class TestQuestionBuilding:
    async def test_ask_about_missing_routines(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        assert result is not None
        assert "routine" in result.lower() or "habits" in result.lower()

    async def test_ask_about_missing_goals(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        assert result is not None
        assert "goal" in result.lower()

    async def test_ask_about_missing_rules(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW)
        assert result is not None
        assert "rule" in result.lower() or "remind" in result.lower()

    async def test_no_questions_when_everything_exists(self, questioner, mgr, store):
        from clipponyai.accountability import (
            AccountabilityRuleStore, GoalStore, RoutineStore,
        )
        mgr.begin()
        mgr.complete()
        RoutineStore(store).add("Morning run", cadence="daily")
        GoalStore(store).add("Fitness goal")
        AccountabilityRuleStore(store).add("Break rule", rule_type="time", condition="after 22:00")
        result = await questioner.tick(NOW)
        assert result is None

    async def test_batch_capped_at_max(self, store, mgr, config):
        config.max_questions_per_batch = 1
        q = ProactiveQuestioner(config, store, mgr)
        mgr.begin()
        mgr.complete()
        result = await q.tick(NOW)
        assert result is not None
        # Should contain at most one question (no double newlines separating multiple)
        assert result.count("\n\n") == 0


class TestNoRepeat:
    async def test_no_repeat_within_gap(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result1 = await questioner.tick(NOW)
        assert result1 is not None
        # Same time, should be blocked by gap
        result2 = await questioner.tick(NOW)
        assert result2 is None

    async def test_repeat_after_gap(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result1 = await questioner.tick(NOW)
        assert result1 is not None
        # After gap
        later = NOW + timedelta(hours=4)
        result2 = await questioner.tick(later)
        assert result2 is not None


class TestAllowDelivery:
    async def test_no_delivery_when_blocked(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        result = await questioner.tick(NOW, allow_delivery=False)
        assert result is None

    async def test_persists_last_asked_on_delivery(self, questioner, mgr):
        mgr.begin()
        mgr.complete()
        await questioner.tick(NOW)
        raw = questioner.store.get_meta("proactive_last_asked")
        assert raw is not None


class TestActivityRecording:
    async def test_records_activity(self, questioner, mgr, store):
        mgr.begin()
        mgr.complete()
        await questioner.tick(NOW)
        from clipponyai.accountability import ActivityStore
        activity = ActivityStore(store)
        entries = activity.recent()
        assert any(e.action == "proactive_questions_asked" for e in entries)


class TestIntegration:
    async def test_full_flow(self, store, config, mgr):
        """Full lifecycle: new -> complete -> ask -> silence -> resume -> ask again."""
        q = ProactiveQuestioner(config, store, mgr)

        # New state blocks
        assert await q.tick(NOW) is None

        # Complete onboarding
        mgr.begin()
        mgr.complete()

        # First ask
        result = await q.tick(NOW)
        assert result is not None

        # Gap blocks
        assert await q.tick(NOW + timedelta(hours=2)) is None

        # Silence blocks
        q.silence(24)
        assert await q.tick(NOW + timedelta(hours=5)) is None

        # Resume allows
        q.resume()
        result = await q.tick(NOW + timedelta(hours=5))
        assert result is not None

    async def test_scheduler_last_only(self, store, config, mgr):
        """Proactive questions only fire when nothing else delivered this tick."""
        q = ProactiveQuestioner(config, store, mgr)
        mgr.begin()
        mgr.complete()
        q.mark_delivered_this_tick()
        result = await q.tick(NOW)
        assert result is None

    async def test_first_launch_prompt_once(self, store, config, mgr):
        """Onboarding prompt delivered only once across restarts."""
        # First "launch"
        mgr1 = OnboardingManager(store)
        assert mgr1.status() == "new"
        prompt = mgr1.begin()
        mgr1.record_prompt(prompt)

        # Second "launch" -- should not re-deliver
        mgr2 = OnboardingManager(store)
        assert mgr2.status() == "in_progress"
        assert mgr2.prompt_was_delivered()
