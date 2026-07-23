"""Tests for clipponyai.install — cross-platform install helpers.

All tests monkeypatch platform.system() and every path function so nothing
writes outside tmp_path.  No real autostart/desktop files are touched.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Fake $HOME so Path.home() resolves inside tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Patch pathlib.Path.home directly
    monkeypatch.setattr(Path, "home", lambda: home, raising=True)
    return home


def _patch_linux(monkeypatch, tmp_path, fake_home):
    """Make the install module think it is on Linux with tmp_path dirs."""
    monkeypatch.setattr("clipponyai.install._SYSTEM", "Linux")
    monkeypatch.setattr("clipponyai.install.platform.system", lambda: "Linux")
    # XDG overrides so paths land in tmp_path
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    return tmp_path


def _patch_macos(monkeypatch, tmp_path, fake_home):
    """Make the install module think it is on macOS."""
    monkeypatch.setattr("clipponyai.install._SYSTEM", "Darwin")
    monkeypatch.setattr("clipponyai.install.platform.system", lambda: "Darwin")
    return tmp_path


# ── Linux: .desktop entry generation ───────────────────────────────────


def test_linux_desktop_entry_contains_exec(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import _linux_desktop_entry

    content = _linux_desktop_entry()
    assert "Exec=" in content
    assert "-m clipponyai" in content
    assert "[Desktop Entry]" in content
    assert "Type=Application" in content
    assert "Name=clipponyai" in content


def test_linux_desktop_entry_uses_sys_executable(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import _linux_desktop_entry

    content = _linux_desktop_entry()
    assert sys.executable in content
    assert f"Exec={sys.executable} -m clipponyai" in content


def test_linux_desktop_entry_custom_icon(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import _linux_desktop_entry

    icon = tmp_path / "my_icon.png"
    content = _linux_desktop_entry(icon)
    assert f"Icon={icon}" in content


# ── Linux: autostart enable/disable/status ─────────────────────────────


def test_linux_enable_autostart_creates_file(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import enable_autostart, _linux_autostart_path

    msg = enable_autostart()
    path = _linux_autostart_path()
    assert path.exists()
    assert "installed autostart" in msg


def test_linux_enable_autostart_idempotent(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import enable_autostart

    msg1 = enable_autostart()
    assert "installed" in msg1
    msg2 = enable_autostart()
    assert "already present" in msg2


def test_linux_disable_autostart_removes_file(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import disable_autostart, enable_autostart, _linux_autostart_path

    enable_autostart()
    assert _linux_autostart_path().exists()
    msg = disable_autostart()
    assert not _linux_autostart_path().exists()
    assert "removed" in msg


def test_linux_disable_autostart_when_missing(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import disable_autostart

    msg = disable_autostart()
    assert "not installed" in msg or "already disabled" in msg


def test_linux_autostart_status_enabled(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import autostart_status, enable_autostart

    enable_autostart()
    msg = autostart_status()
    assert "enabled" in msg


def test_linux_autostart_status_disabled(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import autostart_status

    msg = autostart_status()
    assert "disabled" in msg


# ── Linux: desktop entry install/uninstall/status ──────────────────────


def test_linux_install_desktop_creates_file(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    # Mock generate_icon_png so it doesn't need QApplication
    monkeypatch.setattr(
        "clipponyai.install.generate_icon_png",
        lambda path=None: path or tmp_path / "icon.png",
    )
    (tmp_path / "icon.png").touch()  # pretend icon exists
    # Also need to mock _linux_icon_path to return our fake icon
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: tmp_path / "icon.png")

    from clipponyai.install import install_desktop, _linux_desktop_path

    msg = install_desktop()
    assert _linux_desktop_path().exists()
    assert "installed desktop entry" in msg


def test_linux_install_desktop_idempotent(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    monkeypatch.setattr(
        "clipponyai.install.generate_icon_png",
        lambda path=None: path or tmp_path / "icon.png",
    )
    (tmp_path / "icon.png").touch()
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: tmp_path / "icon.png")

    from clipponyai.install import install_desktop

    install_desktop()
    msg = install_desktop()
    assert "already present" in msg


def test_linux_uninstall_desktop(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    monkeypatch.setattr(
        "clipponyai.install.generate_icon_png",
        lambda path=None: path or tmp_path / "icon.png",
    )
    (tmp_path / "icon.png").touch()
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: tmp_path / "icon.png")

    from clipponyai.install import install_desktop, uninstall_desktop, _linux_desktop_path

    install_desktop()
    assert _linux_desktop_path().exists()
    msg = uninstall_desktop()
    assert not _linux_desktop_path().exists()
    assert "removed" in msg


def test_linux_desktop_status(monkeypatch, tmp_path, fake_home):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    monkeypatch.setattr(
        "clipponyai.install.generate_icon_png",
        lambda path=None: path or tmp_path / "icon.png",
    )
    (tmp_path / "icon.png").touch()
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: tmp_path / "icon.png")

    from clipponyai.install import desktop_status, install_desktop

    assert "not installed" in desktop_status()
    install_desktop()
    assert "installed" in desktop_status()


# ── Linux: autostart .desktop content validation ───────────────────────


def test_linux_autostart_desktop_entry_valid(monkeypatch, tmp_path, fake_home):
    """The .desktop file written for autostart has proper structure."""
    _patch_linux(monkeypatch, tmp_path, fake_home)
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: tmp_path / "icon.png")
    (tmp_path / "icon.png").touch()

    from clipponyai.install import enable_autostart, _linux_autostart_path

    enable_autostart()
    content = _linux_autostart_path().read_text()
    lines = content.splitlines()
    # Verify key fields exist
    keys = [line.split("=")[0] for line in lines if "=" in line]
    for required in ("Version", "Type", "Name", "Exec", "Icon"):
        assert required in keys, f"missing {required} in desktop entry"


# ── macOS: plist generation ────────────────────────────────────────────


def test_macos_plist_dict_valid_structure(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import _macos_plist_dict

    d = _macos_plist_dict()
    assert d["Label"] == "clipponyai"
    assert d["ProgramArguments"] == [sys.executable, "-m", "clipponyai"]
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] is False


def test_macos_plist_bytes_roundtrips(monkeypatch, tmp_path, fake_home):
    """plistlib.dumps output can be parsed back with plistlib.loads."""
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import _macos_plist_bytes, _macos_plist_dict

    raw = _macos_plist_bytes()
    parsed = plistlib.loads(raw)
    expected = _macos_plist_dict()
    assert parsed == expected


def test_macos_plist_bytes_is_xml(monkeypatch, tmp_path, fake_home):
    """plistlib.dumps produces XML by default."""
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import _macos_plist_bytes

    raw = _macos_plist_bytes()
    text = raw.decode()
    assert text.startswith("<?xml")


# ── macOS: autostart enable/disable/status ─────────────────────────────


def test_macos_enable_autostart_creates_plist(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import enable_autostart, _macos_plist_path

    msg = enable_autostart()
    path = _macos_plist_path()
    assert path.exists()
    assert "installed autostart" in msg


def test_macos_enable_autostart_idempotent(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import enable_autostart

    msg1 = enable_autostart()
    assert "installed" in msg1
    msg2 = enable_autostart()
    assert "already present" in msg2


def test_macos_disable_autostart_removes_plist(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import disable_autostart, enable_autostart, _macos_plist_path

    enable_autostart()
    assert _macos_plist_path().exists()
    msg = disable_autostart()
    assert not _macos_plist_path().exists()
    assert "removed" in msg


def test_macos_disable_autostart_when_missing(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import disable_autostart

    msg = disable_autostart()
    assert "not installed" in msg or "already disabled" in msg


def test_macos_autostart_status_enabled(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import autostart_status, enable_autostart

    enable_autostart()
    msg = autostart_status()
    assert "enabled" in msg


def test_macos_autostart_status_disabled(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import autostart_status

    msg = autostart_status()
    assert "disabled" in msg


# ── macOS: plist content validation ────────────────────────────────────


def test_macos_plist_file_valid_plistlib(monkeypatch, tmp_path, fake_home):
    """The written plist file can be parsed by plistlib."""
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import enable_autostart, _macos_plist_path

    enable_autostart()
    path = _macos_plist_path()
    raw = path.read_bytes()
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == "clipponyai"
    assert "ProgramArguments" in parsed
    assert sys.executable in parsed["ProgramArguments"]


# ── macOS: desktop entry explains LaunchAgents ─────────────────────────


def test_macos_install_desktop_explains(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import install_desktop

    msg = install_desktop()
    assert "macOS" in msg
    assert "LaunchAgent" in msg


def test_macos_desktop_status_not_applicable(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import desktop_status

    msg = desktop_status()
    assert "LaunchAgents" in msg


def test_macos_uninstall_desktop_noop(monkeypatch, tmp_path, fake_home):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.install import uninstall_desktop

    msg = uninstall_desktop()
    assert "macOS" in msg


# ── unsupported platform ───────────────────────────────────────────────


def test_unsupported_platform_autostart(monkeypatch, tmp_path, fake_home):
    monkeypatch.setattr("clipponyai.install._SYSTEM", "Windows")
    monkeypatch.setattr("clipponyai.install.platform.system", lambda: "Windows")
    from clipponyai.install import autostart_status, enable_autostart, disable_autostart

    assert "not supported" in autostart_status()
    assert "not supported" in enable_autostart()
    assert "not supported" in disable_autostart()


def test_unsupported_platform_desktop(monkeypatch, tmp_path, fake_home):
    monkeypatch.setattr("clipponyai.install._SYSTEM", "Windows")
    monkeypatch.setattr("clipponyai.install.platform.system", lambda: "Windows")
    from clipponyai.install import install_desktop, uninstall_desktop, desktop_status

    assert "not supported" in install_desktop()
    assert "not supported" in uninstall_desktop()
    assert "not supported" in desktop_status()


# ── idempotent write helper ────────────────────────────────────────────


def test_idempotent_write_new_file(monkeypatch, tmp_path):
    from clipponyai.install import _idempotent_write

    path = tmp_path / "sub" / "file.txt"
    changed = _idempotent_write(path, "hello")
    assert changed is True
    assert path.read_text() == "hello"


def test_idempotent_write_unchanged(monkeypatch, tmp_path):
    from clipponyai.install import _idempotent_write

    path = tmp_path / "file.txt"
    _idempotent_write(path, "hello")
    changed = _idempotent_write(path, "hello")
    assert changed is False


def test_idempotent_write_changed(monkeypatch, tmp_path):
    from clipponyai.install import _idempotent_write

    path = tmp_path / "file.txt"
    _idempotent_write(path, "hello")
    changed = _idempotent_write(path, "world")
    assert changed is True
    assert path.read_text() == "world"


def test_idempotent_write_bytes_new(monkeypatch, tmp_path):
    from clipponyai.install import _idempotent_write_bytes

    path = tmp_path / "sub" / "data.bin"
    changed = _idempotent_write_bytes(path, b"\x00\x01")
    assert changed is True
    assert path.read_bytes() == b"\x00\x01"


def test_idempotent_write_bytes_unchanged(monkeypatch, tmp_path):
    from clipponyai.install import _idempotent_write_bytes

    path = tmp_path / "data.bin"
    _idempotent_write_bytes(path, b"\x00\x01")
    changed = _idempotent_write_bytes(path, b"\x00\x01")
    assert changed is False


# ── entrypoint helper ──────────────────────────────────────────────────


def test_entrypoint_uses_sys_executable():
    from clipponyai.install import _entrypoint

    ep = _entrypoint()
    assert ep.startswith(sys.executable)
    assert "-m clipponyai" in ep


# ── CLI integration ────────────────────────────────────────────────────


def test_cli_autostart_status(monkeypatch, tmp_path, fake_home, capsys):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.cli import main

    code = main(["autostart", "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "disabled" in out


def test_cli_autostart_enable(monkeypatch, tmp_path, fake_home, capsys):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.cli import main

    code = main(["autostart", "enable"])
    assert code == 0
    out = capsys.readouterr().out
    assert "installed autostart" in out or "already" in out


def test_cli_autostart_disable(monkeypatch, tmp_path, fake_home, capsys):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.cli import main

    # enable first
    main(["autostart", "enable"])
    capsys.readouterr()
    code = main(["autostart", "disable"])
    assert code == 0
    out = capsys.readouterr().out
    assert "removed" in out


def test_cli_autostart_default_is_status(monkeypatch, tmp_path, fake_home, capsys):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    from clipponyai.cli import main

    code = main(["autostart"])
    assert code == 0
    out = capsys.readouterr().out
    assert "disabled" in out


def test_cli_install_desktop_linux(monkeypatch, tmp_path, fake_home, capsys):
    _patch_linux(monkeypatch, tmp_path, fake_home)
    monkeypatch.setattr(
        "clipponyai.install.generate_icon_png",
        lambda path=None: path or tmp_path / "icon.png",
    )
    (tmp_path / "icon.png").touch()
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: tmp_path / "icon.png")

    from clipponyai.cli import main

    code = main(["install-desktop"])
    assert code == 0
    out = capsys.readouterr().out
    assert "desktop entry" in out


def test_cli_install_desktop_macos(monkeypatch, tmp_path, fake_home, capsys):
    _patch_macos(monkeypatch, tmp_path, fake_home)
    from clipponyai.cli import main

    code = main(["install-desktop"])
    assert code == 0
    out = capsys.readouterr().out
    assert "macOS" in out


# ── icon generation (mocked Qt) ────────────────────────────────────────


def test_generate_icon_png_saves_file(monkeypatch, tmp_path, fake_home):
    """generate_icon_png saves a PNG when given a path, with Qt mocked."""
    from pathlib import Path as RealPath

    # Fake QPixmap.save to just create the file
    def fake_save(self, path, fmt="PNG"):
        RealPath(path).parent.mkdir(parents=True, exist_ok=True)
        RealPath(path).write_bytes(b"fake-png")

    fake_pixmap = type("FakePixmap", (), {"save": fake_save})()

    def fake_pixmap_method(self, w, h):
        return fake_pixmap

    fake_icon = type("FakeIcon", (), {"pixmap": fake_pixmap_method})()

    monkeypatch.setattr(
        "clipponyai.sprites.app_icon",
        lambda: fake_icon,
    )
    # Prevent QApplication instantiation
    monkeypatch.setattr(
        "PySide6.QtWidgets.QApplication.instance",
        lambda: "existing",
    )

    from clipponyai.install import generate_icon_png

    out_path = tmp_path / "my_icon.png"
    result = generate_icon_png(out_path)
    assert result == out_path
    assert out_path.exists()


def test_ensure_icon_skips_when_exists(monkeypatch, tmp_path, fake_home):
    """ensure_icon returns 'already present' when icon exists."""
    icon_path = tmp_path / "icon.png"
    icon_path.touch()
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: icon_path)

    from clipponyai.install import ensure_icon

    msg = ensure_icon()
    assert "already present" in msg


def test_ensure_icon_generates_when_missing(monkeypatch, tmp_path, fake_home):
    """ensure_icon calls generate_icon_png when icon is missing."""
    icon_path = tmp_path / "icon.png"
    monkeypatch.setattr("clipponyai.install._linux_icon_path", lambda: icon_path)

    generated = []

    def fake_generate(path=None):
        generated.append(path or icon_path)
        icon_path.touch()
        return icon_path

    monkeypatch.setattr("clipponyai.install.generate_icon_png", fake_generate)

    from clipponyai.install import ensure_icon

    msg = ensure_icon()
    assert "generated icon" in msg
    assert len(generated) == 1
