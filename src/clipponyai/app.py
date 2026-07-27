"""Wiring: one Core (brain + tasks + scheduler + channels) and two shells.

The Core is GUI-free — it runs fine headless (telegram-only on a server).
The Qt shell adds the pony overlay and chat window on top. Both share the
same single conversation and the same reminder delivery chokepoint.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

from .accountability import get_stores
from .awareness import AwarenessMonitor, PonyBrainAssessor
from .brain import PonyBrain
from .channels import Channel
from .characters import get_character
from .config import Config, config_path, data_dir, db_path
from .context_questions import ProactiveQuestioner
from .goals import GoalEngine
from .logwatch import read_recent_logs
from .onboarding import OnboardingManager
from .rules import RuleEngine
from .scheduler import ReminderScheduler
from .tasks import TaskStore

log = logging.getLogger("clipponyai.app")

Observer = Callable[[str, str, str], Awaitable[None]]  # (source, user_text, reply)
Deliver = Callable[[str], Awaitable[None]]

PONY_HIDE_UNTIL_META = "pony_hide_until"


def temporary_hide_remaining_seconds(value: str | None, now: float | None = None) -> int:
    """Return whole seconds left in a persisted temporary hide, or zero."""
    if not value:
        return 0
    try:
        remaining = float(value) - (time.time() if now is None else now)
    except (TypeError, ValueError):
        return 0
    return max(0, math.ceil(remaining))


class Core:
    """Everything except pixels."""

    def __init__(self, config: Config, *, screenshot_fn=None, client_factory=None) -> None:
        self.config = config
        self.store = TaskStore(db_path())
        # Accountability stores (routines, goals, activity, …)
        self.accountability = get_stores(self.store)
        # Wire token-usage callback into the brain
        token_usage_store = self.accountability["token_usage"]

        def _token_callback(
            lane: str,
            purpose: str,
            provider: str,
            model: str,
            prompt_tokens: int,
            completion_tokens: int,
            estimated: bool,
        ) -> None:
            token_usage_store.record(
                lane=lane,
                purpose=purpose,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated=estimated,
            )

        self.brain = PonyBrain(
            config, self.store, screenshot_fn=screenshot_fn,
            log_fn=lambda: read_recent_logs(config.logwatch),
            client_factory=client_factory,
            token_callback=_token_callback,
        )
        # Wire RoutineEngine into the scheduler
        routine_engine = self._make_routine_engine()
        # Wire GoalEngine
        goal_engine = self._make_goal_engine()
        # Wire RuleEngine
        rule_engine = self._make_rule_engine()
        # Inject engines into brain for tool handlers
        self.brain._routine_engine = routine_engine
        self.brain._goal_engine = goal_engine
        self.brain._rule_engine = rule_engine
        self.brain._activity_store = self.accountability["activity"]
        self.brain._acct_stores = self.accountability

        # Wire OnboardingManager
        self.onboarding = OnboardingManager(self.store)

        # Wire ProactiveQuestioner
        self.proactive_questioner = ProactiveQuestioner(
            config=config.proactive_questions,
            store=self.store,
            onboarding=self.onboarding,
            quiet_hours_start=config.reminders.quiet_hours_start,
            quiet_hours_end=config.reminders.quiet_hours_end,
            activity_store=self.accountability["activity"],
        )
        # Inject into brain for tool handlers
        self.brain._set_proactive_questioner(self.proactive_questioner)

        self.scheduler = ReminderScheduler(
            self.store, config.reminders, self._deliver_nudge,
            work_hours=config.reminders.work_hours,
            routine_engine=routine_engine,
            goal_engine=goal_engine,
            rule_engine=rule_engine,
            activity_store=self.accountability["activity"],
            proactive_questioner=self.proactive_questioner,
        )
        self.awareness_monitor = AwarenessMonitor(
            config, screenshot_fn, self._make_assessor(), self.store, self._deliver_nudge,
            activity_store=self.accountability["activity"],
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

    def _make_goal_engine(self):
        """Build the GoalEngine wired into the scheduler."""
        acct = self.accountability
        return GoalEngine(
            goal_store=acct["goals"],
            progress_store=acct["goal_progress"],
            routine_store=acct["routines"],
            completion_store=acct["routine_completions"],
            activity_store=acct["activity"],
        )

    def _make_rule_engine(self):
        """Build the RuleEngine wired into the scheduler."""
        acct = self.accountability

        async def _rule_delivery(message: str, rule_id: int) -> None:
            await self._deliver_nudge(message)

        return RuleEngine(
            rule_store=acct["rules"],
            activity_store=acct["activity"],
            delivery=_rule_delivery,
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

    async def start_onboarding_if_needed(self) -> None:
        """Initiate first-run onboarding once, after GUI hooks are available.

        Delivers the initial prompt through existing nudge hooks / chat history.
        Safe to call multiple times -- only fires once (persisted via meta key).
        """
        from .onboarding import OnboardingManager

        mgr = OnboardingManager(self.store)
        if not self.config.onboarding.enabled:
            return
        if mgr.status() != "new":
            return  # already started, completed, or skipped
        prompt = mgr.begin()
        mgr.record_prompt(prompt)
        # Deliver through existing nudge hooks (bubble + chat)
        await self._deliver_nudge(prompt)

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
    # Onboarding: deliver through existing channels (telegram, etc.)
    await core.start_onboarding_if_needed()
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
        # Update proactive questioner's config reference in-place
        if core.proactive_questioner is not None:
            core.proactive_questioner.config = config.proactive_questions
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
    from PySide6.QtCore import QTimer
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

    # Onboarding is scheduled after the startup greeting below.  Scheduling
    # it here lets the greeting race with and overwrite the actual questions.

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

    # ── dashboard (lazy singleton) ──────────────────────────────────
    _dashboard = None

    def _get_dashboard():
        nonlocal _dashboard
        if _dashboard is None:
            from .dashboard import DashboardWindow
            _dashboard = DashboardWindow(core)
        return _dashboard

    def _show_dashboard():
        d = _get_dashboard()
        d.show()
        d.raise_()
        d.activateWindow()

    def _show_dashboard_tasks():
        d = _get_dashboard()
        d.show_tasks_tab()

    pony.clicked.connect(toggle_chat)
    pony.character_selected.connect(on_character)
    pony.provider_selected.connect(on_provider)
    pony.screenshot_toggled.connect(on_peek)
    pony.tasks_requested.connect(lambda: _show_dashboard_tasks())
    pony.settings_requested.connect(lambda: _open_settings(pony, core, config, chat))
    pony.dashboard_requested.connect(lambda: _show_dashboard())

    hide_timer = QTimer(pony)
    hide_timer.setSingleShot(True)

    def _clear_temporary_hide() -> None:
        hide_timer.stop()
        core.store.set_meta(PONY_HIDE_UNTIL_META, "")

    def show_pony() -> None:
        """Show immediately, cancelling any temporary hide."""
        _clear_temporary_hide()
        pony.show()
        pony.raise_()

    def hide_pony_for(seconds: int) -> None:
        """Hide until the duration expires, including across app restarts."""
        if seconds <= 0:
            return
        core.store.set_meta(PONY_HIDE_UNTIL_META, str(time.time() + seconds))
        pony.bubble.hide()
        pony.hide()
        hide_timer.start(seconds * 1000)

    def toggle_pony() -> None:
        """Toggle from the tray; showing early cancels a temporary hide."""
        if pony.isVisible():
            pony.bubble.hide()
            pony.hide()
        else:
            show_pony()

    hide_timer.timeout.connect(show_pony)
    pony.hide_for_requested.connect(hide_pony_for)
    pony.hide_requested.connect(toggle_pony)

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
    tray_menu.addAction("📊 planner & activity", _show_dashboard)
    tray_menu.addAction("📋 tasks", _show_dashboard_tasks)
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

    hidden_for = temporary_hide_remaining_seconds(
        core.store.get_meta(PONY_HIDE_UNTIL_META)
    )
    if hidden_for:
        pony.hide()
        hide_timer.start(hidden_for * 1000)
    else:
        _clear_temporary_hide()
        pony.show()

    with loop:
        loop.run_until_complete(core.start())
        if pony.isVisible():
            greeting = get_character(config.ui.character).name
            pony.say(f"✨ {greeting} reporting for duty! click me to chat.", msec=6000)
        # Run after the greeting is shown so an unanswered first-run prompt is
        # the final, pinned bubble instead of being immediately overwritten.
        loop.create_task(core.start_onboarding_if_needed())
        loop.run_forever()
    return 0
