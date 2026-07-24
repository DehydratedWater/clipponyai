"""Telegram channel: talk to your pony from your phone.

Needs the `telegram` extra (``pip install clipponyai[telegram]``) and a bot
token from @BotFather in the env var named by ``telegram.token_env``. The
allowlist is empty by default — the bot answers NOBODY until you put your own
numeric Telegram user id in ``telegram.allowed_user_ids`` (message the bot
once and check the logs, or ask @userinfobot for your id).

Commands are deterministic (no LLM): /tasks renders the store verbatim,
/pony switches character. Everything else goes to the shared brain — the
same single conversation as the desktop bubble.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from .channels import Channel, HandleMessage
from .config import TelegramConfig

log = logging.getLogger("clipponyai.telegram")

HELP_TEXT = """\
🦄 pony's little command book:
/tasks — everything I'm tracking for you, verbatim
/pony <name> — switch who you're talking to (twilight, rainbow-dash, pinkie-pie, fluttershy, rarity, applejack, clippy, orb)
/help — this list
anything else — just talk to me! ✨"""


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        state_dir: Path,
        handle_message: HandleMessage,
        overview_fn: Callable[[], str],
        set_character_fn: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(handle_message)
        self.config = config
        self.overview_fn = overview_fn
        self.set_character_fn = set_character_fn
        self._chats_file = state_dir / "telegram_chats.json"
        self._chat_ids: set[int] = self._load_chats()
        self._app = None

    # ── chat-id persistence (so reminders reach you after restarts) ──
    def _load_chats(self) -> set[int]:
        try:
            return set(json.loads(self._chats_file.read_text()))
        except (OSError, ValueError):
            return set()

    def _save_chats(self) -> None:
        try:
            self._chats_file.parent.mkdir(parents=True, exist_ok=True)
            self._chats_file.write_text(json.dumps(sorted(self._chat_ids)))
        except OSError:
            log.warning("could not persist telegram chat ids", exc_info=True)

    # ── lifecycle ────────────────────────────────────────────────────
    async def start(self) -> None:
        token = os.environ.get(self.config.token_env, "")
        if not token:
            raise RuntimeError(
                f"telegram enabled but ${self.config.token_env} is not set — "
                f"get a token from @BotFather and export it"
            )

        try:
            from telegram import Update
            from telegram.ext import (
                ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters,
            )
        except ImportError as e:
            raise RuntimeError(
                "telegram extra not installed — pip install 'clipponyai[telegram]'"
            ) from e

        async def _guard(update: Update) -> bool:
            user = update.effective_user
            if user is None or user.id not in self.config.allowed_user_ids:
                uid = user.id if user else "?"
                log.warning(
                    "ignoring telegram message from non-allowlisted user id %s "
                    "(add it to telegram.allowed_user_ids to allow)", uid,
                )
                return False
            if update.effective_chat:
                if update.effective_chat.id not in self._chat_ids:
                    self._chat_ids.add(update.effective_chat.id)
                    self._save_chats()
            return True

        async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not await _guard(update) or not update.message or not update.message.text:
                return
            reply = await self.handle_message(update.message.text)
            await update.message.reply_text(reply)

        async def on_tasks(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if await _guard(update) and update.message:
                await update.message.reply_text(self.overview_fn())

        async def on_pony(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not await _guard(update) or not update.message:
                return
            if self.set_character_fn is None or not ctx.args:
                await update.message.reply_text("usage: /pony <name>")
                return
            await update.message.reply_text(self.set_character_fn(ctx.args[0]))

        async def on_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if await _guard(update) and update.message:
                await update.message.reply_text(HELP_TEXT)

        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler(["start", "help"], on_help))
        app.add_handler(CommandHandler("tasks", on_tasks))
        app.add_handler(CommandHandler("pony", on_pony))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        self._app = app
        log.info("telegram channel up (allowlist: %s)", self.config.allowed_user_ids or "EMPTY")

    async def send(self, text: str) -> None:
        if self._app is None or not self._chat_ids:
            return
        for chat_id in list(self._chat_ids):
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                log.warning("telegram send to %s failed", chat_id, exc_info=True)

    async def stop(self) -> None:
        if self._app is not None:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
