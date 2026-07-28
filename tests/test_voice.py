"""Tests for rewriting sensor notes into the active character's voice."""

from clipponyai.characters import PROACTIVE_BASE, get_character


def test_voice_uses_proactive_persona_and_returns_model_output(make_brain):
    brain = make_brain({"pony-voice": "Easy there — let's get you back on track."})

    spoken = brain.voice("The user is scrolling a video feed.", kind="awareness")

    assert spoken == "Easy there — let's get you back on track."
    client = next(c for c in brain._test_clients if c.spec.agent_id == "pony-voice")
    assert client.spec.tools == ()
    assert get_character(brain.character_slug).persona in client.spec.system_prompt
    assert PROACTIVE_BASE in client.spec.system_prompt
    sent = "\n".join(
        message["content"]
        for message in client.calls[0]["messages"]
        if isinstance(message["content"], str)
    )
    assert "[sensor note —" in sent
    assert "The user is scrolling a video feed." in sent
    assert "Say this to your friend in your own voice" in sent


def test_voice_falls_back_to_raw_note_when_model_raises(make_brain):
    def fail(_messages, _tools):
        raise RuntimeError("provider unavailable")

    brain = make_brain({"pony-voice": fail})
    note = "The user is looking at a distracting site."

    assert brain.voice(note, kind="awareness") == note


def test_voice_falls_back_to_raw_note_on_empty_output(make_brain):
    brain = make_brain({"pony-voice": ""})
    note = "The user is looking at a distracting site."

    assert brain.voice(note) == note
