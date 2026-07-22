from clipponyai.characters import (
    BASE_PROMPT, CHARACTERS, FORMS, build_system_prompt, character_slugs, get_character,
)


def test_every_character_has_a_distinct_persona():
    personas = [c.persona for c in [*CHARACTERS, *FORMS]]
    assert len(personas) == len(set(personas))
    assert len(CHARACTERS) == 7 and len(FORMS) == 2


def test_system_prompt_combines_persona_and_duties():
    for character in [*CHARACTERS, *FORMS]:
        prompt = build_system_prompt(character)
        assert character.persona in prompt
        assert BASE_PROMPT in prompt
        assert "add_task" in prompt  # the duties mention the tools


def test_get_character_fallback():
    assert get_character("nonexistent").slug == "twilight"
    assert get_character("rarity").name == "Rarity"
    assert get_character("clippy").procedural
    assert not get_character("applejack").procedural


def test_sprite_manifests_have_core_states():
    for character in CHARACTERS:
        assert {"idle", "walk"} <= set(character.states), character.slug
        for state, pair in character.states.items():
            assert len(pair) == 2, f"{character.slug}/{state}"


def test_character_slugs_excludes_forms():
    slugs = character_slugs()
    assert "twilight" in slugs and "clippy" not in slugs
