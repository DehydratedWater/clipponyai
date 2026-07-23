"""In-app settings dialog: pony-friendly, validated, saves to config.yaml.

Wraps the pure logic in ``settings_apply.py`` with a PySide6 QTabWidget UI.
Every field maps to an existing Config setting.  Validation runs on Apply;
errors are shown inline (red helper text) and in a summary banner.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import Config
from .settings_apply import (
    SettingsForm,
    apply_to_config,
    detect_changes,
    needs_restart,
    read_form,
    validate,
)

# ── pony palette ──────────────────────────────────────────────────────

_DIALOG_STYLE = """
    QDialog { background: #1e1a2e; color: #e8e4f5; }
    QTabWidget::pane { background: #1e1a2e; border: 1px solid #3a3355; border-radius: 6px; }
    QTabBar::tab { background: #262138; color: #c9b7f5; padding: 6px 14px;
                   border-radius: 6px 6px 0 0; margin-right: 2px; min-width: 80px; }
    QTabBar::tab:selected { background: #3a3355; color: #efeaff; font-weight: 600; }
    QGroupBox { color: #c9b7f5; font-weight: 600; border: 1px solid #3a3355;
                border-radius: 6px; margin-top: 8px; padding-top: 10px; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
    QFormLayout { spacing: 6px; }
    QLabel { color: #e8e4f5; }
    QLabel[error="true"] { color: #f77; font-weight: 600; font-size: 12px; }
    QLineEdit { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                border-radius: 4px; padding: 4px 6px; }
    QLineEdit:focus { border-color: #b28ff2; }
    QComboBox { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
                border-radius: 4px; padding: 4px; }
    QSpinBox { background: #262138; color: #e8e4f5; border: 1px solid #3a3355;
               border-radius: 4px; padding: 4px; }
    QCheckBox { color: #e8e4f5; spacing: 6px; }
    QPushButton { background: #b28ff2; color: #191430; border: none;
                  border-radius: 6px; padding: 8px 18px; font-weight: 600; }
    QPushButton:hover { background: #c4a8f7; }
    QPushButton:disabled { background: #5a5270; color: #8d86a8; }
    QListWidget { background: #262138; border: 1px solid #3a3355; border-radius: 4px; }
    QListWidget::item { padding: 2px; }
    #errorBanner { background: #3a1a1a; color: #f77; border: 1px solid #6a3333;
                   border-radius: 6px; padding: 8px; font-size: 13px; }
    #restartBanner { background: #2a2a1a; color: #f0e68c; border: 1px solid #5a5a33;
                     border-radius: 6px; padding: 8px; font-size: 13px; }
"""

# ── helper widgets ─────────────────────────────────────────────────────


class ErrorLabel(QLabel):
    """Label that shows inline validation errors in red."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setProperty("error", True)
        self.setVisible(False)

    def set_error(self, msg: str | None) -> None:
        self.setText(msg or "")
        self.setVisible(bool(msg))


# ── tab builders ───────────────────────────────────────────────────────
# Each returns a QWidget with _apply() and optionally _set_errors(mapping).


def _add_log_path(paths_list: QListWidget, path_line: QLineEdit) -> None:
    """Add a log path after expanding ~ and rejecting blanks."""
    from pathlib import Path as _Path

    raw = path_line.text().strip()
    if not raw:
        return
    expanded = str(_Path(raw).expanduser())
    paths_list.addItem(expanded)
    path_line.clear()


def _build_privacy_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("Privacy")
    fl = QFormLayout()

    chk_screenshot = QCheckBox("Allow pony to peek at your screen")
    chk_screenshot.setChecked(form.screenshot_enabled)

    chk_auto = QCheckBox("Auto-track passing promises (e.g. 'I'll call mom later')")
    chk_auto.setChecked(form.auto_track_commitments)

    fl.addRow(chk_screenshot)
    fl.addRow(chk_auto)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.screenshot_enabled = chk_screenshot.isChecked()
        form.auto_track_commitments = chk_auto.isChecked()

    page._apply = apply  # type: ignore[attr-defined]
    return page


def _build_pony_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("Pony Appearance & Behaviour")
    fl = QFormLayout()

    char_combo = QComboBox()
    _CHAR_DISPLAY = {
        "twilight": "Twilight Sparkle",
        "twilight-alicorn": "Princess Twilight",
        "rainbow-dash": "Rainbow Dash",
        "pinkie-pie": "Pinkie Pie",
        "fluttershy": "Fluttershy",
        "rarity": "Rarity",
        "applejack": "Applejack",
        "clippy": "Clippy",
        "orb": "Orb",
    }
    for slug, name in _CHAR_DISPLAY.items():
        char_combo.addItem(name, slug)
    char_combo.setCurrentText(_CHAR_DISPLAY.get(form.character, form.character))

    scale_spin = QSpinBox()
    scale_spin.setRange(30, 300)
    scale_spin.setSingleStep(1)
    scale_spin.setSuffix("%")
    scale_spin.setValue(int(form.pony_scale * 100))

    attn_spin = QSpinBox()
    attn_spin.setRange(5, 300)
    attn_spin.setSuffix(" s")
    attn_spin.setValue(form.pony_attention_seconds)

    chk_wander = QCheckBox("Wander around when idle")
    chk_wander.setChecked(form.pony_idle_wander)

    err_char = ErrorLabel()
    err_scale = ErrorLabel()
    err_attn = ErrorLabel()

    fl.addRow("Character:", char_combo)
    fl.addRow("", err_char)
    fl.addRow("Size:", scale_spin)
    fl.addRow("", err_scale)
    fl.addRow("Attention chase duration:", attn_spin)
    fl.addRow("", err_attn)
    fl.addRow("", chk_wander)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.character = char_combo.currentData()
        form.pony_scale = scale_spin.value() / 100.0
        form.pony_attention_seconds = attn_spin.value()
        form.pony_idle_wander = chk_wander.isChecked()

    def set_errors(errors: dict[str, str]) -> None:
        err_char.set_error(errors.get("character"))
        err_scale.set_error(errors.get("pony_scale"))
        err_attn.set_error(errors.get("pony_attention_seconds"))

    page._apply = apply  # type: ignore[attr-defined]
    page._set_errors = set_errors  # type: ignore[attr-defined]
    return page


def _build_reminders_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("Reminders")
    fl = QFormLayout()

    chk_enabled = QCheckBox("Enable reminders")
    chk_enabled.setChecked(form.reminders_enabled)

    interval_spin = QSpinBox()
    interval_spin.setRange(10, 3600)
    interval_spin.setSuffix(" s")
    interval_spin.setValue(form.reminders_check_interval)

    quiet_start_spin = QSpinBox()
    quiet_start_spin.setRange(0, 23)
    quiet_start_spin.setSuffix(" :00")
    quiet_start_spin.setValue(form.reminders_quiet_start)

    quiet_end_spin = QSpinBox()
    quiet_end_spin.setRange(0, 23)
    quiet_end_spin.setSuffix(" :00")
    quiet_end_spin.setValue(form.reminders_quiet_end)

    gaps_line = QLineEdit()
    gaps_line.setText(", ".join(str(g) for g in form.reminders_nudge_gaps))
    gaps_line.setPlaceholderText("e.g. 30, 60, 120, 240, 360")

    max_nudges_spin = QSpinBox()
    max_nudges_spin.setRange(1, 50)
    max_nudges_spin.setValue(form.reminders_max_nudges)

    batch_spin = QSpinBox()
    batch_spin.setRange(1, 20)
    batch_spin.setValue(form.reminders_batch_limit)

    err_interval = ErrorLabel()
    err_quiet = ErrorLabel()
    err_gaps = ErrorLabel()
    err_max = ErrorLabel()
    err_batch = ErrorLabel()

    fl.addRow("", chk_enabled)
    fl.addRow("Check every:", interval_spin)
    fl.addRow("", err_interval)
    fl.addRow("Quiet hours start:", quiet_start_spin)
    fl.addRow("Quiet hours end:", quiet_end_spin)
    fl.addRow("", err_quiet)
    fl.addRow("Nudge gaps (minutes):", gaps_line)
    fl.addRow("", err_gaps)
    fl.addRow("Max nudges per task:", max_nudges_spin)
    fl.addRow("", err_max)
    fl.addRow("Batch limit (tasks per nudge):", batch_spin)
    fl.addRow("", err_batch)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.reminders_enabled = chk_enabled.isChecked()
        form.reminders_check_interval = interval_spin.value()
        form.reminders_quiet_start = quiet_start_spin.value()
        form.reminders_quiet_end = quiet_end_spin.value()
        form.reminders_max_nudges = max_nudges_spin.value()
        form.reminders_batch_limit = batch_spin.value()
        raw = gaps_line.text().strip()
        if raw:
            try:
                form.reminders_nudge_gaps = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError:
                pass  # caught by validate

    def set_errors(errors: dict[str, str]) -> None:
        err_interval.set_error(errors.get("reminders_check_interval"))
        err_quiet.set_error(errors.get("reminders_quiet"))
        err_gaps.set_error(errors.get("reminders_nudge_gaps"))
        err_max.set_error(errors.get("reminders_max_nudges"))
        err_batch.set_error(errors.get("reminders_batch_limit"))

    page._apply = apply  # type: ignore[attr-defined]
    page._set_errors = set_errors  # type: ignore[attr-defined]
    return page


def _build_workhours_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("Work Hours")
    fl = QFormLayout()

    chk_enabled = QCheckBox("Enable work-hours mode")
    chk_enabled.setChecked(form.work_hours_enabled)

    start_line = QLineEdit()
    start_line.setText(form.work_hours_start)
    start_line.setPlaceholderText("HH:MM")
    start_line.setFixedWidth(80)

    end_line = QLineEdit()
    end_line.setText(form.work_hours_end)
    end_line.setPlaceholderText("HH:MM")
    end_line.setFixedWidth(80)

    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday_checks: list[QCheckBox] = []
    for i, name in enumerate(weekday_names):
        chk = QCheckBox(name)
        chk.setChecked(i in form.work_hours_weekdays)
        weekday_checks.append(chk)

    wkday_layout = QHBoxLayout()
    for chk in weekday_checks:
        wkday_layout.addWidget(chk)

    chk_closing = QCheckBox("Closing nudge at end of workday")
    chk_closing.setChecked(form.work_hours_closing_nudge)

    chk_suppress = QCheckBox("Suppress ordinary reminders outside work hours")
    chk_suppress.setChecked(form.work_hours_suppress_off_hours)

    err_time = ErrorLabel()
    err_weekday = ErrorLabel()

    fl.addRow("", chk_enabled)
    fl.addRow("Start:", start_line)
    fl.addRow("End:", end_line)
    fl.addRow("", err_time)
    fl.addRow("Active days:", wkday_layout)
    fl.addRow("", err_weekday)
    fl.addRow("", chk_closing)
    fl.addRow("", chk_suppress)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.work_hours_enabled = chk_enabled.isChecked()
        form.work_hours_start = start_line.text().strip()
        form.work_hours_end = end_line.text().strip()
        form.work_hours_closing_nudge = chk_closing.isChecked()
        form.work_hours_suppress_off_hours = chk_suppress.isChecked()
        form.work_hours_weekdays = sorted(
            i for i, chk in enumerate(weekday_checks) if chk.isChecked()
        )

    def set_errors(errors: dict[str, str]) -> None:
        err_time.set_error(errors.get("work_hours_time"))
        err_weekday.set_error(errors.get("work_hours_weekdays"))

    page._apply = apply  # type: ignore[attr-defined]
    page._set_errors = set_errors  # type: ignore[attr-defined]
    return page


def _build_logwatch_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("Log Watch")
    fl = QFormLayout()

    chk_enabled = QCheckBox("Enable log watching")
    chk_enabled.setChecked(form.logwatch_enabled)

    # paths as a list widget
    paths_list = QListWidget()
    for p in form.logwatch_paths:
        paths_list.addItem(p)
    paths_list.setAccessibleDescription("Configured absolute log file paths")

    add_path_row = QHBoxLayout()
    path_line = QLineEdit()
    path_line.setPlaceholderText("/path/to/logfile.log")
    path_line.setFixedWidth(200)

    from PySide6.QtWidgets import QPushButton as QPushButton_

    add_btn = QPushButton_("+")
    add_btn.setFixedWidth(30)
    add_btn.clicked.connect(lambda: _add_log_path(paths_list, path_line))
    add_path_row.addWidget(path_line)
    add_path_row.addWidget(add_btn)
    add_path_row.addStretch()

    remove_btn = QPushButton_("- remove")
    remove_btn.clicked.connect(lambda: paths_list.takeItem(paths_list.currentRow()) if paths_list.currentRow() >= 0 else None)

    lines_spin = QSpinBox()
    lines_spin.setRange(1, 10000)
    lines_spin.setValue(form.logwatch_max_lines)

    chars_spin = QSpinBox()
    chars_spin.setRange(100, 100000)
    chars_spin.setValue(form.logwatch_max_chars)

    err_paths = ErrorLabel()
    err_bounds = ErrorLabel()

    fl.addRow("", chk_enabled)
    fl.addRow("Log file paths:", paths_list)
    fl.addRow("", add_path_row)
    fl.addRow("", remove_btn)
    fl.addRow("", err_paths)
    fl.addRow("Max lines per file:", lines_spin)
    fl.addRow("Max total chars:", chars_spin)
    fl.addRow("", err_bounds)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.logwatch_enabled = chk_enabled.isChecked()
        from pathlib import Path as _Path
        raw_paths = [paths_list.item(i).text() for i in range(paths_list.count())]
        form.logwatch_paths = [str(_Path(p).expanduser()) for p in raw_paths]
        form.logwatch_max_lines = lines_spin.value()
        form.logwatch_max_chars = chars_spin.value()

    def set_errors(errors: dict[str, str]) -> None:
        err_paths.set_error(errors.get("logwatch_paths"))
        err_bounds.set_error(errors.get("logwatch_bounds"))

    page._apply = apply  # type: ignore[attr-defined]
    page._set_errors = set_errors  # type: ignore[attr-defined]
    return page


def _build_llm_tab(form: SettingsForm, available_providers: list[str]) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("LLM Provider")
    fl = QFormLayout()

    prov_combo = QComboBox()
    for name in sorted(available_providers):
        prov_combo.addItem(name, name)
    prov_combo.setCurrentText(form.active_provider)

    base_url_line = QLineEdit(form.provider_base_url)
    base_url_line.setPlaceholderText("https://api.example.com/v1  (blank = OpenAI default)")

    key_env_line = QLineEdit(form.provider_api_key_env)
    key_env_line.setPlaceholderText("MY_API_KEY  (env var name; blank = no key needed)")

    fast_line = QLineEdit(form.provider_fast_model)
    fast_line.setPlaceholderText("gpt-4o-mini  (chat turns + small sensors)")

    slow_line = QLineEdit(form.provider_slow_model)
    slow_line.setPlaceholderText("leave blank to reuse fast model (deep_think lane)")

    vision_line = QLineEdit(form.provider_vision_model)
    vision_line.setPlaceholderText("leave blank to reuse slow model (screenshots)")

    err_models = ErrorLabel()

    hint = QLabel(
        "Editing a provider here updates its config in-place.\n"
        "Export the matching key env var separately (e.g. export MY_API_KEY=...).\n"
        "Provider/model changes take effect after restart for the active conversation."
    )
    hint.setWordWrap(True)
    hint.setStyleSheet("color: #8d86a8; font-size: 11px;")

    fl.addRow("Active provider:", prov_combo)
    fl.addRow("Base URL:", base_url_line)
    fl.addRow("API key env var:", key_env_line)
    fl.addRow("Fast model:", fast_line)
    fl.addRow("Slow model:", slow_line)
    fl.addRow("Vision model:", vision_line)
    fl.addRow("", err_models)
    fl.addRow("", hint)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.active_provider = prov_combo.currentData()
        form.provider_base_url = base_url_line.text().strip()
        form.provider_api_key_env = key_env_line.text().strip()
        form.provider_fast_model = fast_line.text().strip()
        form.provider_slow_model = slow_line.text().strip()
        form.provider_vision_model = vision_line.text().strip()

    def set_errors(errors: dict[str, str]) -> None:
        err_models.set_error(errors.get("provider_models"))

    page._apply = apply  # type: ignore[attr-defined]
    page._set_errors = set_errors  # type: ignore[attr-defined]
    return page


def _build_misc_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    box = QGroupBox("Autostart")
    fl = QFormLayout()

    chk_auto = QCheckBox("Start clipponyai on login")
    chk_auto.setChecked(form.autostart_enabled)

    fl.addRow(chk_auto)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.autostart_enabled = chk_auto.isChecked()

    page._apply = apply  # type: ignore[attr-defined]
    return page


def _build_awareness_tab(form: SettingsForm) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(8, 8, 8, 8)

    # Privacy warning banner
    warning = QLabel(
        "\u26a0\ufe0f Privacy: This feature periodically sends screenshots to your LLM "
        "to detect distractions. Both screen peeking AND awareness must be enabled "
        "in settings for this to work."
    )
    warning.setObjectName("restartBanner")  # reuse the yellow banner style
    lay.addWidget(warning)

    box = QGroupBox("Proactive Focus Awareness")
    fl = QFormLayout()

    chk_enabled = QCheckBox("Enable proactive focus/distraction awareness")
    chk_enabled.setChecked(form.awareness_enabled)

    interval_spin = QSpinBox()
    interval_spin.setRange(30, 3600)
    interval_spin.setSuffix(" s")
    interval_spin.setValue(form.awareness_interval_seconds)

    cooldown_spin = QSpinBox()
    cooldown_spin.setRange(5, 480)
    cooldown_spin.setSuffix(" min")
    cooldown_spin.setValue(form.awareness_cooldown_minutes)

    conf_spin = QSpinBox()
    conf_spin.setRange(0, 100)
    conf_spin.setSuffix("%")
    conf_spin.setValue(int(form.awareness_minimum_confidence * 100))

    policy_edit = QPlainTextEdit()
    policy_edit.setPlainText(form.awareness_focus_policy)
    policy_edit.setFixedHeight(80)
    policy_edit.setPlaceholderText(
        "Natural-language policy, e.g. 'During work hours, interrupt on social media.'"
    )

    err_interval = ErrorLabel()
    err_cooldown = ErrorLabel()
    err_confidence = ErrorLabel()

    fl.addRow("", chk_enabled)
    fl.addRow("Check interval:", interval_spin)
    fl.addRow("", err_interval)
    fl.addRow("Cooldown after alert:", cooldown_spin)
    fl.addRow("", err_cooldown)
    fl.addRow("Minimum confidence:", conf_spin)
    fl.addRow("", err_confidence)
    fl.addRow("Focus policy:", policy_edit)
    box.setLayout(fl)
    lay.addWidget(box)
    lay.addStretch()

    def apply() -> None:
        form.awareness_enabled = chk_enabled.isChecked()
        form.awareness_interval_seconds = interval_spin.value()
        form.awareness_cooldown_minutes = cooldown_spin.value()
        form.awareness_minimum_confidence = conf_spin.value() / 100.0
        form.awareness_focus_policy = policy_edit.toPlainText().strip()

    def set_errors(errors: dict[str, str]) -> None:
        err_interval.set_error(errors.get("awareness_interval"))
        err_cooldown.set_error(errors.get("awareness_cooldown"))
        err_confidence.set_error(errors.get("awareness_confidence"))

    page._apply = apply  # type: ignore[attr-defined]
    page._set_errors = set_errors  # type: ignore[attr-defined]
    return page


# ── main dialog ────────────────────────────────────────────────────────


class SettingsDialog(QDialog):
    """Editable in-app settings dialog.

    Reads current ``Config`` into a flat form, validates on Apply, and
    persists back to ``config.yaml``.  Autostart operations only fire when
    the checkbox value actually changed.

    Emits ``applied`` after every successful save (both Apply and Apply & Close).
    """

    applied = Signal()

    def __init__(
        self,
        config: Config,
        *,
        available_providers: list[str],
        autostart_enabled: bool = False,
        enable_autostart_fn: Callable[[], str] | None = None,
        disable_autostart_fn: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Dialog)
        self.config = config
        self.available_providers = available_providers
        self._enable_autostart_fn = enable_autostart_fn
        self._disable_autostart_fn = disable_autostart_fn

        self.setWindowTitle("Settings")
        self.setMinimumSize(480, 520)
        self.setStyleSheet(_DIALOG_STYLE)

        # read current values — autostart_enabled must be set before building tabs
        # so the Misc tab's checkbox reflects actual state
        self.form = read_form(config, autostart_enabled=autostart_enabled)
        self._original = read_form(config, autostart_enabled=autostart_enabled)  # snapshot for diff

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(12, 12, 12, 12)

        # error banner (hidden by default)
        self._error_banner = QLabel()
        self._error_banner.setObjectName("errorBanner")
        self._error_banner.setVisible(False)
        main_lay.addWidget(self._error_banner)

        # restart banner (hidden by default)
        self._restart_banner = QLabel()
        self._restart_banner.setObjectName("restartBanner")
        self._restart_banner.setVisible(False)
        main_lay.addWidget(self._restart_banner)

        # tabs
        self._tabs = QTabWidget()
        self._tabs.addTab(_build_privacy_tab(self.form), "Privacy")
        self._tabs.addTab(_build_pony_tab(self.form), "Pony")
        self._tabs.addTab(_build_reminders_tab(self.form), "Reminders")
        self._tabs.addTab(_build_workhours_tab(self.form), "Work Hours")
        self._tabs.addTab(_build_logwatch_tab(self.form), "Log Watch")
        self._tabs.addTab(_build_awareness_tab(self.form), "Awareness")
        self._tabs.addTab(_build_llm_tab(self.form, available_providers), "LLM")
        self._tabs.addTab(_build_misc_tab(self.form), "Misc")
        main_lay.addWidget(self._tabs)

        # buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.Apply | QDialogButtonBox.Cancel | QDialogButtonBox.Ok
        )
        btn_box.button(QDialogButtonBox.Ok).setText("Apply & Close")
        btn_box.button(QDialogButtonBox.Apply).setText("Apply")
        btn_box.accepted.connect(self._apply_and_close)
        btn_box.rejected.connect(self.reject)
        btn_box.button(QDialogButtonBox.Apply).clicked.connect(self._apply_only)
        main_lay.addWidget(btn_box)

    # ── apply logic ──────────────────────────────────────────────────

    def _collect_tab_values(self) -> None:
        """Read current widget values into self.form."""
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if hasattr(tab, "_apply"):
                tab._apply()

    def _clear_tab_errors(self) -> None:
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if hasattr(tab, "_set_errors"):
                tab._set_errors({})

    def _show_tab_errors(self, errors: list[str]) -> None:
        """Map validation error messages to inline labels on the right tabs."""
        self._clear_tab_errors()
        mapping: dict[str, str] = {}
        for err in errors:
            err_lower = err.lower()
            if "scale" in err_lower:
                mapping["pony_scale"] = err
            elif "attention" in err_lower:
                mapping["pony_attention_seconds"] = err
            elif "character" in err_lower:
                mapping["character"] = err
            elif "interval" in err_lower:
                mapping["reminders_check_interval"] = err
            elif "quiet" in err_lower:
                mapping["reminders_quiet"] = err
            elif "nudge gap" in err_lower or "nudge" in err_lower and "gap" in err_lower:
                mapping["reminders_nudge_gaps"] = err
            elif "max nudges" in err_lower:
                mapping["reminders_max_nudges"] = err
            elif "batch" in err_lower:
                mapping["reminders_batch_limit"] = err
            elif "work" in err_lower and ("hh:mm" in err_lower or "hours" in err_lower or "minutes" in err_lower):
                mapping["work_hours_time"] = err
            elif "weekday" in err_lower:
                mapping["work_hours_weekdays"] = err
            elif "log path" in err_lower:
                mapping["logwatch_paths"] = err
            elif "lines" in err_lower or "chars" in err_lower:
                mapping["logwatch_bounds"] = err
            elif "provider" in err_lower:
                mapping["active_provider"] = err
            elif "awareness" in err_lower and "interval" in err_lower:
                mapping["awareness_interval"] = err
            elif "awareness" in err_lower and "cooldown" in err_lower:
                mapping["awareness_cooldown"] = err
            elif "awareness" in err_lower and "confidence" in err_lower:
                mapping["awareness_confidence"] = err

        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if hasattr(tab, "_set_errors"):
                tab._set_errors(mapping)

    def _apply_only(self) -> None:
        """Validate, apply, and show feedback without closing."""
        self._collect_tab_values()
        errors = validate(self.form, available_providers=self.available_providers)
        if errors:
            self._error_banner.setText("Please fix: " + "; ".join(errors))
            self._error_banner.setVisible(True)
            self._show_tab_errors(errors)
            return
        self._error_banner.setVisible(False)
        self._do_apply()

    def _apply_and_close(self) -> None:
        self._apply_only()
        if not self._error_banner.isVisible():
            self.accept()

    def _do_apply(self) -> None:
        """Apply validated form to config and persist."""
        changes = detect_changes(self._original, self.form)

        # Autostart is the only setting with an OS side effect. Run it only
        # when the checkbox value actually changed and surface failures in the dialog.
        if changes.get("autostart_enabled"):
            try:
                if self.form.autostart_enabled:
                    if self._enable_autostart_fn:
                        self._enable_autostart_fn()
                else:
                    if self._disable_autostart_fn:
                        self._disable_autostart_fn()
            except (OSError, RuntimeError) as exc:
                self._error_banner.setText(f"Could not update autostart: {exc}")
                self._error_banner.setVisible(True)
                return

        apply_to_config(self.form, self.config)
        self.config.save()

        # restart warnings
        restart_reasons = needs_restart(changes)
        if restart_reasons:
            self._restart_banner.setText(
                "Some changes need a restart: " + " ".join(restart_reasons)
            )
            self._restart_banner.setVisible(True)
        else:
            self._restart_banner.setVisible(False)

        # Future Apply clicks compare against what was actually persisted, so
        # an unchanged autostart checkbox never repeats the OS operation.
        self._original = read_form(
            self.config, autostart_enabled=self.form.autostart_enabled
        )
        self.applied.emit()
