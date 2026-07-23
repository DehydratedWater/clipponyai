"""Cross-platform user-level installation helpers.

Manages XDG autostart + .desktop files on Linux and LaunchAgent plist on
macOS.  All writes are idempotent (identical content is a no-op) and only
touch user-level paths.  Uses ``sys.executable -m clipponyai`` as the
entrypoint so it works from venvs, pipx, and system installs alike.

Design principles
=================
- **Pure and testable**: file I/O is done through ``_idempotent_write`` which
  can be monkeypatched.  Platform detection uses ``platform.system()`` which
  is also monkeypatchable.
- **User-level only**: ``~/.config/autostart``, ``~/.local/share/applications``,
  ``~/Library/LaunchAgents`` — never touches system directories.
- **Idempotent**: writing the same content twice is a no-op.
- **Robust entrypoint**: ``sys.executable -m clipponyai`` works from any install
  method (venv, pipx, system pip, …).
"""

from __future__ import annotations

import os
import platform
import plistlib
import sys
from pathlib import Path

from .config import APP_NAME, data_dir

# ── platform detection ─────────────────────────────────────────────────

_SYSTEM = platform.system()  # "Linux", "Darwin", "Windows", …


def _is_linux() -> bool:
    return _SYSTEM == "Linux"


def _is_macos() -> bool:
    return _SYSTEM == "Darwin"


# ── entrypoint helpers ─────────────────────────────────────────────────


def _entrypoint() -> str:
    """Return a robust exec command: ``sys.executable -m clipponyai``."""
    return f"{sys.executable} -m clipponyai"


# ── idempotent file write ──────────────────────────────────────────────


def _idempotent_write(path: Path, content: str) -> bool:
    """Write *content* to *path* only if it differs from the current file.

    Creates parent directories.  Returns ``True`` if the file was written
    (new or changed), ``False`` if unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _idempotent_write_bytes(path: Path, content: bytes) -> bool:
    """Write binary *content* to *path* only if it differs.

    Creates parent directories.  Returns ``True`` if written, ``False`` if
    unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return False
    path.write_bytes(content)
    return True


# ── Linux XDG paths ────────────────────────────────────────────────────


def _xdg_autostart_dir() -> Path:
    """User-level XDG autostart directory."""
    override = os.environ.get("XDG_CONFIG_HOME")
    if override:
        return Path(override) / "autostart"
    return Path.home() / ".config" / "autostart"


def _xdg_applications_dir() -> Path:
    """User-level XDG applications directory."""
    override = os.environ.get("XDG_DATA_HOME")
    if override:
        return Path(override) / "applications"
    return Path.home() / ".local" / "share" / "applications"


def _linux_autostart_path() -> Path:
    return _xdg_autostart_dir() / f"{APP_NAME}.desktop"


def _linux_desktop_path() -> Path:
    return _xdg_applications_dir() / f"{APP_NAME}.desktop"


def _linux_icon_path() -> Path:
    """Path where the icon PNG lives (or will be generated)."""
    return data_dir() / "icon.png"


# ── Linux .desktop file generation ─────────────────────────────────────


def _linux_desktop_entry(icon_path: Path | None = None) -> str:
    """Build a .desktop file for clipponyai.

    Follows the Desktop Entry Specification (version 1.1).
    The ``Exec`` value is safely formed — no shell metacharacters beyond
    what ``sys.executable`` and ``-m clipponyai`` produce.
    """
    entrypoint = _entrypoint()
    icon = str(icon_path or _linux_icon_path())
    return (
        "[Desktop Entry]\n"
        "Version=1.1\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "GenericName=Desktop Assistant\n"
        "Comment=A cute desktop pony assistant — tasks, reminders, any LLM\n"
        f"Exec={entrypoint}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )


# ── macOS LaunchAgent paths ────────────────────────────────────────────


def _macos_launchagents_dir() -> Path:
    """User-level LaunchAgents directory."""
    return Path.home() / "Library" / "LaunchAgents"


def _macos_plist_path() -> Path:
    return _macos_launchagents_dir() / f"{APP_NAME}.plist"


# ── macOS plist generation ─────────────────────────────────────────────


def _macos_plist_dict(data_directory: Path | None = None) -> dict:
    """Build a valid LaunchAgent plist dictionary.

    *data_directory* defaults to ``data_dir()`` when ``None``.
    """
    data_directory = data_directory or data_dir()
    return {
        "Label": APP_NAME,
        "ProgramArguments": [sys.executable, "-m", "clipponyai"],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(data_directory / "stdout.log"),
        "StandardErrorPath": str(data_directory / "stderr.log"),
    }


def _macos_plist_bytes(data_directory: Path | None = None) -> bytes:
    """Serialize the LaunchAgent plist to XML bytes."""
    return plistlib.dumps(_macos_plist_dict(data_directory))


# ── public API: autostart ──────────────────────────────────────────────


def enable_autostart() -> str:
    """Enable user-level autostart for the current platform.

    On Linux, ensures the app icon is generated before writing the
    .desktop file so the Icon= line is valid.

    Returns a human-readable status message.
    """
    if _is_linux():
        # Ensure icon exists before referencing it from the .desktop file
        try:
            icon_msg = ensure_icon()
        except Exception as e:
            return f"failed to generate icon: {e}"
        path = _linux_autostart_path()
        content = _linux_desktop_entry()
        written = _idempotent_write(path, content)
        if written:
            return f"{icon_msg}; installed autostart: {path}"
        return f"{icon_msg}; autostart already present: {path}"
    if _is_macos():
        # Ensure the data directory (parent of log paths) exists so the
        # launchd process can write stdout/stderr without failing.
        data_dir().mkdir(parents=True, exist_ok=True)
        path = _macos_plist_path()
        content_bytes = _macos_plist_bytes()
        written = _idempotent_write_bytes(path, content_bytes)
        if written:
            return f"installed autostart: {path}"
        return f"autostart already present: {path}"
    return f"autostart not supported on {platform.system()}"


def disable_autostart() -> str:
    """Disable user-level autostart for the current platform.

    Returns a human-readable status message.
    """
    if _is_linux():
        path = _linux_autostart_path()
        if path.exists():
            path.unlink()
            return f"removed autostart: {path}"
        return f"autostart not installed: {path}"
    if _is_macos():
        path = _macos_plist_path()
        if path.exists():
            path.unlink()
            return f"removed autostart: {path}"
        return f"autostart not installed: {path}"
    return f"autostart not supported on {platform.system()}"


def autostart_status() -> str:
    """Report whether autostart is enabled.

    Returns a human-readable status string.
    """
    if _is_linux():
        path = _linux_autostart_path()
        if path.exists():
            return f"enabled — {path}"
        return f"disabled ({path} not found)"
    if _is_macos():
        path = _macos_plist_path()
        if path.exists():
            return f"enabled — {path}"
        return f"disabled ({path} not found)"
    return f"not supported on {platform.system()}"


# ── public API: desktop entry (Linux) / app launcher ───────────────────


def install_desktop() -> str:
    """Install a desktop entry (Linux) or explain app launch (macOS).

    On Linux, ensures the app icon is generated, then writes a .desktop
    file to the user applications directory so the app shows in the
    application menu.

    On macOS, explains that app launch is handled by the LaunchAgent and
    does not create a separate launcher.

    Returns a human-readable status message.
    """
    if _is_linux():
        # Ensure icon exists before referencing it from the .desktop file
        try:
            icon_msg = ensure_icon()
        except Exception as e:
            return f"failed to generate icon: {e}"
        path = _linux_desktop_path()
        content = _linux_desktop_entry()
        written = _idempotent_write(path, content)
        if written:
            return f"{icon_msg}; installed desktop entry: {path}"
        return f"{icon_msg}; desktop entry already present: {path}"
    if _is_macos():
        return (
            "macOS uses LaunchAgents for app launch.  "
            f"Run `clipponyai autostart --enable` to create "
            f"{_macos_plist_path()}."
        )
    return f"desktop entry not supported on {platform.system()}"


def uninstall_desktop() -> str:
    """Remove the desktop entry (Linux) or no-op (macOS).

    Returns a human-readable status message.
    """
    if _is_linux():
        path = _linux_desktop_path()
        if path.exists():
            path.unlink()
            return f"removed desktop entry: {path}"
        return "no desktop entry found (already removed)"
    if _is_macos():
        return "macOS does not use .desktop files — nothing to remove"
    return f"desktop entry not supported on {platform.system()}"


def desktop_status() -> str:
    """Return a human-readable desktop entry status.

    Returns a string describing whether a desktop entry is
    installed, missing or not supported.
    """
    if _is_linux():
        path = _linux_desktop_path()
        if path.exists():
            return f"installed — {path}"
        return "not installed"
    if _is_macos():
        return "not applicable (macOS uses LaunchAgents)"
    return f"not supported on {platform.system()}"


# ── public API: icon generation ────────────────────────────────────────


def generate_icon_png(path: Path | None = None) -> Path:
    """Generate the app icon PNG and save it to *path*.

    Requires a running QApplication (PySide6).  If *path* is ``None``,
    saves to ``data_dir() / "icon.png"``.

    Returns the path where the icon was saved.
    """
    from PySide6.QtWidgets import QApplication

    path = path or _linux_icon_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # QApplication may already exist (e.g., during normal GUI runs)
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    from .sprites import app_icon

    icon = app_icon()
    pixmap = icon.pixmap(64, 64)
    pixmap.save(str(path), "PNG")
    return path


def ensure_icon() -> str:
    """Ensure the app icon PNG exists in the data directory.

    Generates it if missing.  Returns a human-readable message.
    """
    icon_path = _linux_icon_path()
    if icon_path.exists():
        return f"icon already present: {icon_path}"
    generate_icon_png(icon_path)
    return f"generated icon: {icon_path}"


# ── public API: full install/uninstall ─────────────────────────────────


def full_install() -> list[str]:
    """Run all applicable install steps for the current platform.

    Returns a list of human-readable status messages (one per step).
    """
    messages: list[str] = []
    messages.append(ensure_icon())
    messages.append(install_desktop())
    messages.append(enable_autostart())
    return messages


def full_uninstall() -> list[str]:
    """Run all applicable uninstall steps for the current platform.

    Returns a list of human-readable status messages (one per step).
    """
    messages: list[str] = []
    messages.append(uninstall_desktop())
    messages.append(disable_autostart())
    return messages
