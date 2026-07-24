from __future__ import annotations

from pathlib import Path

import pytest

from clipponyai.config import SkillsConfig
from clipponyai.providers import FAST, SLOW, VISION
from clipponyai.skills import SkillsError, SkillsLibrary

EMPTY_SENSE = {"done_task_ids": [], "maybe_done_task_ids": [], "commitments": []}


@pytest.fixture(autouse=True)
def isolated_skills_home(tmp_path, monkeypatch):
    """Keep skill discovery away from the real ~/.agents/skills catalog."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _write_skill(
    root: Path,
    name: str,
    *,
    declared_name: str | None = None,
    description: str = "Helps with a test task.",
    body: str = "Follow these test instructions.",
    metadata: str = "",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {declared_name or name}\n"
        f"description: {description}\n"
        f"{metadata}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_scan_catalog_validation_disabled_and_metadata(tmp_path):
    root = tmp_path / "extra-skills"
    valid = _write_skill(
        root,
        "valid-skill",
        metadata="metadata:\n  owner: tests\n",
    )
    _write_skill(root, "wrong-dir", declared_name="different-name")
    _write_skill(root, "disabled-skill")
    broken = root / "broken-yaml"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: broken-yaml\ndescription: [unterminated\n---\nbody",
        encoding="utf-8",
    )
    warnings: list[str] = []
    library = SkillsLibrary(
        SkillsConfig(dirs=[str(root)], disabled=["disabled-skill"]),
        warnings.append,
    )

    skills = library.scan()
    assert len(skills) == 1
    skill = skills[0]
    assert (skill.name, skill.description, skill.path, skill.metadata) == (
        "valid-skill",
        "Helps with a test task.",
        valid.resolve(),
        {"owner": "tests"},
    )
    assert library.catalog() == (
        "## Available skills\n"
        "Use the activate_skill tool to load a skill's full instructions when relevant.\n"
        "- valid-skill: Helps with a test task."
    )
    assert any("broken-yaml" in warning for warning in warnings)
    assert any("wrong-dir" in warning for warning in warnings)


def test_default_data_dir_and_tilde_extra_dir_are_scanned(
    tmp_path, monkeypatch
):
    default_root = tmp_path / "data" / "skills"
    _write_skill(default_root, "default-skill")
    home = tmp_path / "home"
    _write_skill(home / "custom-skills", "custom-skill")
    monkeypatch.setenv("HOME", str(home))

    library = SkillsLibrary(SkillsConfig(dirs=["~/custom-skills"]))

    assert library.names() == ["default-skill", "custom-skill"]


def test_first_directory_wins_on_name_collision(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first, "shared-skill", body="first body")
    _write_skill(second, "shared-skill", body="second body")
    warnings: list[str] = []
    library = SkillsLibrary(
        SkillsConfig(dirs=[str(first), str(second)]),
        warnings.append,
    )

    assert "first body" in library.load("shared-skill")
    assert "second body" not in library.load("shared-skill")
    assert any("duplicate skill" in warning for warning in warnings)


def test_load_strips_frontmatter_wraps_body_and_truncates(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "normal-skill", body="# Instructions\nDo the thing.")
    _write_skill(root, "large-skill", body="x" * 20_000)
    library = SkillsLibrary(SkillsConfig(dirs=[str(root)]))

    loaded = library.load("normal-skill")
    assert loaded == (
        '<skill name="normal-skill">\n'
        "# Instructions\nDo the thing.\n"
        "</skill>"
    )
    assert "description:" not in loaded

    large = library.load("large-skill")
    assert len(large) < 16_100
    assert "[truncated: content exceeded the size limit]" in large
    assert large.endswith("\n</skill>")


def test_read_file_happy_path_truncation_and_path_guards(tmp_path):
    root = tmp_path / "skills"
    skill = _write_skill(root, "file-skill")
    references = skill / "references"
    references.mkdir()
    (references / "notes.md").write_text("reference notes", encoding="utf-8")
    (references / "large.txt").write_text("y" * 40_000, encoding="utf-8")
    library = SkillsLibrary(SkillsConfig(dirs=[str(root)]))

    assert library.read_file("file-skill", "references/notes.md") == "reference notes"
    assert "[truncated: content exceeded the size limit]" in library.read_file(
        "file-skill", "references/large.txt"
    )
    with pytest.raises(SkillsError, match="may not contain"):
        library.read_file("file-skill", "../outside.txt")
    with pytest.raises(SkillsError, match="must be relative"):
        library.read_file("file-skill", str((tmp_path / "outside.txt").resolve()))
    with pytest.raises(SkillsError, match="not found"):
        library.read_file("file-skill", "references/missing.md")


def test_disabled_library_is_empty_and_brain_tools_fail_gracefully(
    tmp_path, make_brain
):
    root = tmp_path / "skills"
    _write_skill(root, "hidden-skill")
    library = SkillsLibrary(SkillsConfig(enabled=False, dirs=[str(root)]))
    brain = make_brain({}, skills_library=library)

    assert library.scan() == []
    assert library.catalog() is None
    assert brain._tool_activate_skill({"name": "hidden-skill"}) == (
        "ERROR: skills are disabled"
    )
    assert brain._tool_read_skill_file(
        {"skill": "hidden-skill", "path": "references/notes.md"}
    ) == "ERROR: skills are disabled"


def test_unknown_skill_lists_current_catalog(tmp_path, make_brain):
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    brain = make_brain(
        {},
        skills_library=SkillsLibrary(SkillsConfig(dirs=[str(root)])),
    )

    assert brain._tool_activate_skill({"name": "missing-skill"}) == (
        "ERROR: unknown skill missing-skill. Available: known-skill"
    )


def test_skill_tools_only_appear_in_fast_lane(make_brain):
    brain = make_brain({})
    fast_tools = {tool.name for tool in brain._spec(FAST).tools}

    assert {"activate_skill", "read_skill_file"} <= fast_tools
    assert brain._spec(SLOW).tools == ()
    assert brain._spec(VISION).tools == ()


def test_documented_example_skill_is_valid_and_loadable():
    example_root = (
        Path(__file__).parents[1] / "docs" / "examples" / "skills"
    )
    library = SkillsLibrary(SkillsConfig(dirs=[str(example_root)]))

    assert library.names() == ["commit-messages"]
    assert "Write an imperative subject" in library.load("commit-messages")
    assert "feat: add keyboard navigation" in library.read_file(
        "commit-messages", "references/examples.md"
    )


async def test_brain_catalog_and_activation_tool_round_trip(tmp_path, make_brain):
    root = tmp_path / "skills"
    _write_skill(
        root,
        "planning-skill",
        description="Plans a small project.",
        body="Break the project into verified stages.",
    )
    library = SkillsLibrary(SkillsConfig(dirs=[str(root)]))
    brain = make_brain(
        {
            "pony": [
                ("tool", "activate_skill", {"name": "planning-skill"}),
                "I used the skill.",
            ],
            "message-sensor": EMPTY_SENSE,
        },
        skills_library=library,
    )

    assert await brain.respond("plan this") == "I used the skill."
    pony = [client for client in brain._test_clients if client.spec.agent_id == "pony"][-1]
    assert "## Available skills" in pony.spec.system_prompt
    assert "- planning-skill: Plans a small project." in pony.spec.system_prompt
    tool_messages = [
        message
        for message in pony.calls[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert "Break the project into verified stages." in tool_messages[0]["content"]
