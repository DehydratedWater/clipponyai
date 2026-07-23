"""Wiring: one Core (brain + tasks + scheduler + channels) and two shells.

The Core is GUI-free — it runs fine headless (telegram-only on a server).
The Qt shell adds the pony overlay and chat window on top. Both share the
same single conversation and the same reminder delivery chokepoint.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .accountability import get_stores
from .awareness import AwarenessMonitor, PonyBrainAssessor
from .brain import PonyBrain
from .channels import Channel
from .characters import get_character
from .config import Config, config_path, data_dir, db_path
from .logwatch import read_recent_logs
from .scheduler import ReminderScheduler
from .tasks import TaskStore

log = logging.getLogger("clipponyai.app")

Observer = Callable[[str, str, str], Awaitable[None]]  # (source, user_text, reply)
Deliver = Callable[[str], Awaitable[None]]


class Core:
    """Everything except pixels."""

    def __init__(self, config: Config, *, screenshot_fn=None, client_factory=None) -> None:
        self.config = config
        self.store = TaskStore(db_path())
        # Accountability stores (routines, goals, activity, …)
        self.accountability = get_stores(self.store)
        self.brain = PonyBrain(
            config, self.store, screenshot_fn=screenshot_fn,
            log_fn=lambda: read_recent_logs(config.logwatch),
            client_factory=client_factory,
        )
        # Wire RoutineEngine into the scheduler
        routine_engine = self._make_routine_engine()
        self.scheduler = ReminderScheduler(
            self.store, config.reminders, self._deliver_nudge,
            work_hours=config.reminders.work_hours,
            routine_engine=routine_engine,
        )
        self.awareness_monitor = AwarenessMonitor(
            config, screenshot_fn, self._make_assessor(), self.store, self._deliver_nudge,
        )
        self.channels: list[Channel] = []
        self.observers: list[Observer] = []      # GUI mirrors exchanges live
        self.nudge_hooks: list[Deliver] = []     # GUI shows nudges (bubble + chase)
        self._tasks: list[asyncio.Task] = []

    # ── conversation ─────────────────────────────────────────────────
    async def handle_message(self, text: str, source: str = "desktop") -> str:
        reply = await self.brain.respond(text, source)
        for observer in self.observers:
            try:
                await observer(source, text, reply)
            except Exception:
                log.exception("observer failed")
        return reply

    def overview(self) -> str:
        return self.store.overview()

    def set_character(self, slug: str) -> str:
        character = get_character(slug)
        self.brain.set_character(character.slug)
        self.config.ui.character = character.slug
        self.config.save()
        return f"✨ you're talking to {character.name} now"

    def set_provider(self, name: str) -> None:
        self.brain.set_provider(name)
        self.config.llm.active = name
        self.config.save()

    def set_screenshot_enabled(self, on: bool) -> None:
        self.config.screenshot_enabled = on
        self.config.save()
        try:
            asyncio.get_running_loop().create_task(self.awareness_monitor.refresh())
        except RuntimeError:
            # Configuration can also be changed before the app event loop starts.
            pass

    def _make_assessor(self) -> PonyBrainAssessor:
        return PonyBrainAssessor(self.brain)

    def _make_routine_engine(self):
        """Build the RoutineEngine wired into the scheduler."""
        from .routines import RoutineEngine as RE

        acct = self.accountability
        return RE(
            routine_store=acct["routines"],
            completion_store=acct["routine_completions"],
            task_store=self.store,
            deliver=self._deliver_nudge,
            activity_store=acct["activity"],
        )

    # ── reminders ────────────────────────────────────────────────────
    async def _deliver_nudge(self, message: str) -> None:
        # the nudge is part of the one conversation — the brain must know it
        # already pinged (and the user may answer "done" to it)
        self.store.save_message("assistant", message, source="reminder")
        for hook in self.nudge_hooks:
            try:
                await hook(message)
            except Exception:
                log.exception("nudge hook failed")
        for channel in self.channels:
            try:
                await channel.send(message)
            except Exception:
                log.exception("nudge via %s failed", channel.name)

    # ── lifecycle ────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.config.telegram.enabled:
            from .telegram_channel import TelegramChannel

            channel = TelegramChannel(
                self.config.telegram,
                data_dir(),
                lambda text: self.handle_message(text, source="telegram"),
                self.overview,
                self.set_character,
            )
            try:
                await channel.start()
                self.channels.append(channel)
            except RuntimeError as e:
                log.error("telegram channel not started: %s", e)
        self._tasks.append(asyncio.create_task(self.scheduler.run()))
        await self.awareness_monitor.start()

    async def stop(self) -> None:
        self.scheduler.stop()
        await self.awareness_monitor.stop()
        for task in self._tasks:
            task.cancel()
        for channel in self.channels:
            try:
                await channel.stop()
            except Exception:
                log.exception("stopping %s failed", channel.name)
        self.store.close()


# ── headless shell ────────────────────────────────────────────────────
async def run_headless(config: Config) -> None:
    """No pixels: brain + reminders + channels (telegram). Ctrl-C to stop."""
    core = Core(config)
    await core.start()
    if not core.channels:
        log.warning(
            "headless with no channels — enable telegram in %s to actually talk",
            config_path(),
        )
    log.info("clipponyai core running (headless)")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await core.stop()


# ── settings dialog helper ───────────────────────────────────────────
def _open_settings(pony, core: Core, config: Config, chat=None) -> None:
    from PySide6.QtWidgets import QDialog

    from .install import autostart_status, disable_autostart, enable_autostart
    from .settings_dialog import SettingsDialog

    auto_status = autostart_status()
    auto_on = "enabled" in auto_status.lower() or "installed" in auto_status.lower()

    dialog = SettingsDialog(
        config,
        available_providers=sorted(config.llm.providers),
        autostart_enabled=auto_on,
        enable_autostart_fn=enable_autostart,
        disable_autostart_fn=disable_autostart,
        parent=pony,
    )

    def _on_applied() -> None:
        """Apply live settings after every successful save."""
        pony.screenshot_enabled = config.screenshot_enabled
        pony.set_idle_wander(config.ui.idle_wander)
        if pony.character != config.ui.character:
            note = core.set_character(config.ui.character)
            pony.set_character(config.ui.character)
            if chat is not None:
                chat.set_pony_name(get_character(config.ui.character).name)
            pony.say(note, msec=4000)
        new_scale = config.ui.scale
        if abs((pony.size_px / 128) - new_scale) > 0.01:
            pony.set_scale(new_scale)
        try:
            asyncio.get_running_loop().create_task(core.awareness_monitor.refresh())
        except RuntimeError:
            pass

    dialog.applied.connect(_on_applied)

    result = dialog.exec()
    if result == QDialog.Accepted:
        pony.say("settings saved!", msec=4000)


# ── Qt shell ──────────────────────────────────────────────────────────
def run_gui(config: Config) -> int:
    import qasync
    from PySide6.QtWidgets import QApplication

    from .capture import take_screenshot
    from .chat_window import ChatWindow
    from .overlay import PonyWindow
    from .sprites import app_icon

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    core = Core(config, screenshot_fn=take_screenshot)
    pony = PonyWindow(
        character=config.ui.character,
        scale=config.ui.scale,
        idle_wander=config.ui.idle_wander,
    )
    pony.provider_names = core.brain.provider_names()
    pony.active_provider = core.brain.provider_name
    pony.screenshot_enabled = config.screenshot_enabled
    chat = ChatWindow(pony_name=get_character(config.ui.character).name)

    # ── user talks (chat window) ─────────────────────────────────────
    async def on_send(text: str) -> None:
        chat.add_message("user", text)
        chat.show_typing(True)
        pony.set_state("read" if "read" in _states() else "idle")
        try:
            reply = await core.handle_message(text, source="desktop")
        except Exception as e:
            log.exception("turn failed")
            reply = f"*trips over own hooves* something broke: {e}"
        finally:
            chat.show_typing(False)
            pony.set_state("idle")
        chat.add_message("assistant", reply)
        pony.say(reply)

    def _states() -> set[str]:
        from .sprites import character_states
        return character_states(pony.character)

    chat.send_text.connect(lambda t: asyncio.ensure_future(on_send(t)))
    chat.tasks_clicked.connect(lambda: chat.add_message("system", core.overview()))

    # ── exchanges from other surfaces mirror into the GUI ────────────
    async def mirror(source: str, text: str, reply: str) -> None:
        if source == "desktop":
            return
        if chat.isVisible():
            chat.add_message("user", f"(via {source}) {text}")
            chat.add_message("assistant", reply)

    core.observers.append(mirror)

    # ── reminders: bubble + cursor chase ─────────────────────────────
    async def nudge_gui(message: str) -> None:
        pony.say(message)
        pony.seek_attention(config.ui.attention_seconds * 1000)
        if chat.isVisible():
            chat.add_message("assistant", message)

    core.nudge_hooks.append(nudge_gui)

    # ── menu wiring ──────────────────────────────────────────────────
    def place_chat_near_pony() -> None:
        """Anchor the chat window to the pony, staying fully on one screen."""
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication

        margin = 16
        pony_pos = pony.pos()
        pony_right = pony_pos.x() + pony.width()
        pony_bottom = pony_pos.y() + pony.height()

        screen = QApplication.screenAt(pony_pos) or QApplication.primaryScreen()
        area = screen.availableGeometry()

        # Prefer to the right of the pony; fall back to the left if it won't fit.
        x = pony_right + margin
        if x + chat.width() > area.right():
            x = pony_pos.x() - chat.width() - margin
        # Prefer below the pony; fall back to above if it would run off the bottom.
        y = pony_bottom + margin
        if y + chat.height() > area.bottom():
            y = pony_pos.y() - chat.height() - margin
        # Clamp into the usable area so the window can never end up off-screen.
        x = max(area.left(), min(x, area.right() - chat.width()))
        y = max(area.top(), min(y, area.bottom() - chat.height()))
        chat.move(QPoint(x, y))

    def toggle_chat() -> None:
        if chat.isVisible():
            chat.hide()
        else:
            chat.load_history(core.store.recent_messages(60))
            place_chat_near_pony()
            chat.show()
            chat.input.setFocus()

    def on_character(slug: str) -> None:
        note = core.set_character(slug)
        pony.set_character(slug)
        chat.set_pony_name(get_character(slug).name)
        pony.say(note, msec=4000)

    def on_provider(name: str) -> None:
        core.set_provider(name)
        pony.active_provider = name
        pony.say(f"🧠 brain switched to {name}", msec=4000)

    def on_peek(on: bool) -> None:
        core.set_screenshot_enabled(on)
        pony.screenshot_enabled = on
        pony.say("👀 I can peek at your screen now (ask me!)" if on
                 else "🙈 screen peeking off", msec=4000)

    pony.clicked.connect(toggle_chat)
    pony.character_selected.connect(on_character)
    pony.provider_selected.connect(on_provider)
    pony.screenshot_toggled.connect(on_peek)
    pony.tasks_requested.connect(lambda: (pony.say(core.overview(), msec=20000)))
    pony.settings_requested.connect(lambda: _open_settings(pony, core, config, chat))
    pony.hide_requested.connect(lambda: toggle_pony())

    def toggle_pony() -> None:
        """Show or hide the pony — always recoverable via tray or menu."""
        if pony.isVisible():
            pony.hide()
        else:
            pony.show()
            pony.raise_()

    def quit_app() -> None:
        async def _shutdown() -> None:
            await core.stop()
            tray.hide()
            app.quit()
        asyncio.ensure_future(_shutdown())

    pony.quit_requested.connect(quit_app)

    # ── system tray: a persistent handle even when the pony is hidden ──
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    tray = QSystemTrayIcon(app_icon(), app)
    tray.setToolTip("clipponyai")
    tray_menu = QMenu()
    act_show = tray_menu.addAction("🦄 show / hide pony")
    act_show.triggered.connect(toggle_pony)
    tray_menu.addAction("💬 chat", toggle_chat)
    tray_menu.addAction("📋 tasks", lambda: pony.say(core.overview(), msec=20000))
    tray_menu.addAction("⚙ settings", lambda: _open_settings(pony, core, config, chat))
    tray_menu.addSeparator()
    tray_menu.addAction("✖ quit", quit_app)
    tray.setContextMenu(tray_menu)
    # left-click on the tray also brings the pony back
    tray.activated.connect(
        lambda reason: toggle_pony() if reason == QSystemTrayIcon.Trigger else None
    )
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        log.warning("no system tray detected; hiding the pony is still reversible "
                    "by clicking the tray area if one appears later")

    pony.show()
    with loop:
        loop.run_until_complete(core.start())
        greeting = get_character(config.ui.character).name
        pony.say(f"✨ {greeting} reporting for duty! click me to chat.", msec=6000)
        loop.run_forever()
    return 0
