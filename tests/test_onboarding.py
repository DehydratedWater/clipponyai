"""Tests for onboarding manager: persisted state, transitions, categories, context notes."""

from __future__ import annotations


import pytest

from clipponyai.onboarding import (
    INITIAL_PROMPT,
    ALL_CATEGORIES,
    OnboardingManager,
)


@pytest.fixture
def store(tmp_path):
    from clipponyai.tasks import TaskStore

    s = TaskStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def mgr(store):
    return OnboardingManager(store)


class TestOnboardingStatus:
    def test_new_by_default(self, mgr):
        assert mgr.status() == "new"
        assert mgr.is_new()
        assert not mgr.is_in_progress()
        assert not mgr.is_done()

    def test_begin_transitions_to_in_progress(self, mgr):
        prompt = mgr.begin()
        assert mgr.status() == "in_progress"
        assert mgr.is_in_progress()
        assert not mgr.is_new()
        assert not mgr.is_done()
        assert prompt == INITIAL_PROMPT

    def test_begin_is_idempotent(self, mgr):
        mgr.begin()
        mgr.begin()
        assert mgr.status() == "in_progress"

    def test_begin_does_not_override_completed(self, mgr):
        mgr.begin()
        mgr.complete()
        mgr.begin()
        assert mgr.status() == "completed"

    def test_complete(self, mgr):
        mgr.begin()
        mgr.complete()
        assert mgr.status() == "completed"
        assert mgr.is_done()
        assert not mgr.is_in_progress()

    def test_complete_noop_when_not_in_progress(self, mgr):
        mgr.complete()
        assert mgr.status() == "new"

    def test_skip(self, mgr):
        mgr.skip()
        assert mgr.status() == "skipped"
        assert mgr.is_done()

    def test_skip_from_in_progress(self, mgr):
        mgr.begin()
        mgr.skip()
        assert mgr.status() == "skipped"

    def test_skip_noop_when_done(self, mgr):
        mgr.begin()
        mgr.complete()
        mgr.skip()
        assert mgr.status() == "completed"

    def test_reset(self, mgr):
        mgr.begin()
        mgr.complete()
        mgr.reset()
        assert mgr.status() == "new"
        assert mgr.is_new()


class TestOnboardingPersistence:
    def test_status_survives_new_manager(self, store):
        mgr1 = OnboardingManager(store)
        mgr1.begin()
        mgr1.complete()
        mgr2 = OnboardingManager(store)
        assert mgr2.status() == "completed"

    def test_started_at_recorded(self, store):
        mgr = OnboardingManager(store)
        assert mgr.started_at() is None
        mgr.begin()
        assert mgr.started_at() is not None

    def test_prompt_was_delivered(self, store):
        mgr = OnboardingManager(store)
        assert not mgr.prompt_was_delivered()
        mgr.begin()
        mgr.record_prompt("hello")
        assert mgr.prompt_was_delivered()

    def test_first_launch_only_once(self, store):
        """Simulating two app restarts: prompt should only fire once."""
        mgr1 = OnboardingManager(store)
        assert mgr1.status() == "new"
        mgr1.begin()
        mgr1.record_prompt(INITIAL_PROMPT)

        mgr2 = OnboardingManager(store)
        assert mgr2.status() == "in_progress"
        assert mgr2.prompt_was_delivered()


class TestCollectedCategories:
    def test_empty_by_default(self, mgr):
        assert mgr.get_collected() == []
        assert set(mgr.missing_categories()) == set(ALL_CATEGORIES)

    def test_mark_collected(self, mgr):
        mgr.begin()
        mgr.mark_collected("name_style")
        assert "name_style" in mgr.get_collected()
        assert "name_style" not in mgr.missing_categories()

    def test_mark_collected_idempotent(self, mgr):
        mgr.begin()
        mgr.mark_collected("name_style")
        mgr.mark_collected("name_style")
        assert mgr.get_collected().count("name_style") == 1

    def test_mark_collected_invalid_category(self, mgr):
        mgr.begin()
        mgr.mark_collected("not_a_real_category")
        assert mgr.get_collected() == []

    def test_missing_categories(self, mgr):
        mgr.begin()
        mgr.mark_collected("name_style")
        mgr.mark_collected("work_hours")
        missing = mgr.missing_categories()
        assert "name_style" not in missing
        assert "work_hours" not in missing
        assert "goals" in missing

    def test_collected_survives_restart(self, store):
        mgr1 = OnboardingManager(store)
        mgr1.begin()
        mgr1.mark_collected("name_style")
        mgr1.mark_collected("goals")
        mgr2 = OnboardingManager(store)
        assert set(mgr2.get_collected()) == {"name_style", "goals"}

    def test_all_collected(self, mgr):
        mgr.begin()
        for cat in ALL_CATEGORIES:
            mgr.mark_collected(cat)
        assert mgr.missing_categories() == []


class TestContextNote:
    def test_none_when_not_active(self, mgr):
        assert mgr.context_note() is None

    def test_none_when_completed(self, mgr):
        mgr.begin()
        mgr.complete()
        assert mgr.context_note() is None

    def test_none_when_skipped(self, mgr):
        mgr.skip()
        assert mgr.context_note() is None

    def test_present_when_in_progress(self, mgr):
        mgr.begin()
        note = mgr.context_note()
        assert note is not None
        assert "onboarding" in note.lower()

    def test_lists_missing_categories(self, mgr):
        mgr.begin()
        mgr.mark_collected("name_style")
        note = mgr.context_note()
        assert note is not None
        assert "work hours" in note.lower() or "work_hours" in note.lower()

    def test_all_collected_message(self, mgr):
        mgr.begin()
        for cat in ALL_CATEGORIES:
            mgr.mark_collected(cat)
        note = mgr.context_note()
        assert note is not None
        assert "all initial information collected" in note.lower()

    def test_directs_to_tools(self, mgr):
        mgr.begin()
        note = mgr.context_note()
        assert note is not None
        assert "add_routine" in note
        assert "add_goal" in note
        assert "complete_onboarding" in note


class TestInitialPrompt:
    def test_prompt_mentions_partial_answers(self):
        assert "all of these" in INITIAL_PROMPT or "some" in INITIAL_PROMPT

    def test_prompt_mentions_skip(self):
        assert "skip" in INITIAL_PROMPT.lower()

    def test_prompt_covers_name_style(self):
        assert "call you" in INITIAL_PROMPT.lower() or "name" in INITIAL_PROMPT.lower()

    def test_prompt_covers_work_hours(self):
        assert "work hours" in INITIAL_PROMPT.lower()

    def test_prompt_covers_recurring(self):
        assert "recurring" in INITIAL_PROMPT.lower() or "daily" in INITIAL_PROMPT.lower()

    def test_prompt_covers_goals(self):
        assert "goal" in INITIAL_PROMPT.lower()

    def test_prompt_covers_boundaries(self):
        assert "quiet" in INITIAL_PROMPT.lower() or "boundary" in INITIAL_PROMPT.lower()
