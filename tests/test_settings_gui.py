"""Headless constructor smoke test for the real Qt settings dialog."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipponyai.config import Config
from clipponyai.settings_dialog import SettingsDialog


def test_settings_dialog_constructs_with_every_tab():
    app = QApplication.instance() or QApplication([])
    config = Config()
    dialog = SettingsDialog(
        config,
        available_providers=sorted(config.llm.providers),
        enable_autostart_fn=lambda: "enabled",
        disable_autostart_fn=lambda: "disabled",
    )

    assert dialog.windowTitle() == "Settings"
    assert dialog._tabs.count() == 7
    assert dialog._tabs.tabText(4) == "Log Watch"

    dialog.close()
    app.processEvents()
