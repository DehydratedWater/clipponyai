import asyncio
import json

from clipponyai.cli import main
from clipponyai.config import Config, config_path
from clipponyai.telegram_channel import TelegramChannel
from clipponyai.markdown import md_to_html


# ── CLI ───────────────────────────────────────────────────────────────
def test_init_writes_config(capsys):
    assert main(["init"]) == 0
    assert config_path().exists()
    out = capsys.readouterr().out
    assert "wrote default config" in out
    # second run does not clobber
    assert main(["init"]) == 0
    assert "already exists" in capsys.readouterr().out


def test_tasks_command_prints_overview(capsys):
    assert main(["tasks"]) == 0
    assert "nothing tracked" in capsys.readouterr().out


def test_doctor_reports_missing_key(capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    Config().save()
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert "export OPENAI_API_KEY" in out
    assert code == 1  # missing key + sprites → doctor complains


def test_version(capsys):
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0


# ── telegram channel plumbing (no network) ────────────────────────────
def _channel(tmp_path, config=None):
    async def handle(text):
        return f"echo: {text}"

    from clipponyai.config import TelegramConfig

    return TelegramChannel(
        config or TelegramConfig(), tmp_path, handle, lambda: "overview", lambda s: f"now {s}"
    )


def test_chat_id_persistence_roundtrip(tmp_path):
    channel = _channel(tmp_path)
    channel._chat_ids = {123, 456}
    channel._save_chats()
    fresh = _channel(tmp_path)
    assert fresh._chat_ids == {123, 456}
    assert json.loads((tmp_path / "telegram_chats.json").read_text()) == [123, 456]


def test_corrupt_chat_file_ignored(tmp_path):
    (tmp_path / "telegram_chats.json").write_text("not json{")
    assert _channel(tmp_path)._chat_ids == set()


def test_start_without_token_raises(tmp_path, monkeypatch):
    from clipponyai.config import TelegramConfig

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    channel = _channel(tmp_path, TelegramConfig(enabled=True))
    try:
        asyncio.get_event_loop_policy()
        import pytest

        with pytest.raises(RuntimeError, match="BotFather"):
            asyncio.run(channel.start())
    finally:
        pass


def test_send_before_start_is_noop(tmp_path):
    channel = _channel(tmp_path)
    asyncio.run(channel.send("hello"))  # must not raise


# ── markdown helper ───────────────────────────────────────────────────
def test_md_to_html_escapes_and_formats():
    html = md_to_html("**bold** *it* `code` <script> https://x.example\n\n- item")
    assert "<b>bold</b>" in html and "<i>it</i>" in html and "<code>code</code>" in html
    assert "&lt;script&gt;" in html
    assert '<a href="https://x.example"' in html
    assert "<li>item</li>" in html  # real list (blank line separates it)


def test_md_balances_orphaned_emote_asterisk():
    """A stray action-emote opener (common LLM output) renders as emphasis."""
    html = md_to_html("*Giggles and trots\n\nHi there!")
    assert "*Giggles" not in html  # no literal asterisk leaked
    assert "<i>Giggles and trots</i>" in html


def test_md_leaves_balanced_emotes_and_bullets_alone():
    html = md_to_html("*fine* and *also fine*\n\n- bullet")
    assert html.count("<i>") == 2
    assert "<li>bullet</li>" in html


def test_md_code_block_preserves_newlines():
    html = md_to_html("```\ndef hi():\n    pass\n```")
    assert "<pre><code>" in html
    assert "<br>" not in html  # no stray <br> injected inside the code block


# ── headless core wiring ──────────────────────────────────────────────
def test_core_delivers_nudges_to_channels_and_history(tmp_path):
    from clipponyai.app import Core

    config = Config()
    core = Core(config)
    sent = []

    class FakeChannel:
        name = "fake"

        async def send(self, text):
            sent.append(text)

        async def start(self):
            pass

        async def stop(self):
            pass

    core.channels.append(FakeChannel())
    asyncio.run(core._deliver_nudge("⏰ do the thing"))
    assert sent == ["⏰ do the thing"]
    # the nudge is part of the one conversation
    assert core.store.recent_messages(1) == [
        {"role": "assistant", "content": "⏰ do the thing"}
    ]
    core.store.close()


def test_core_set_character_persists(tmp_path):
    from clipponyai.app import Core

    config = Config()
    core = Core(config)
    note = core.set_character("rarity")
    assert "Rarity" in note
    assert Config.load().ui.character == "rarity"
    core.store.close()
