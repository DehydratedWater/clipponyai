"""Command line: run the pony, set things up, check your setup.

clipponyai              run the desktop pony (fetches sprites on first run)
clipponyai --headless   run without GUI (telegram + reminders only)
clipponyai init         write a default config.yaml and show its path
clipponyai fetch-sprites  download Desktop Ponies sprites now
clipponyai tasks        print the current task overview
clipponyai doctor       check config, provider keys, sprites, extras,
                        work-hours, logwatch, autostart, permissions
clipponyai check-llm          smoke-test the active LLM provider (returns 0/1)
clipponyai check-llm --vision  verify vision (image) capability
clipponyai check-mcp          check configured MCP server connections and tools
clipponyai autostart    enable/disable/check login autostart
clipponyai install-desktop  install .desktop entry (Linux) / explain (macOS)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config, config_path, db_path, sprites_dir
from .providers import missing_api_key


def _cmd_init(_args) -> int:
    path = config_path()
    if path.exists():
        print(f"config already exists: {path}")
        return 0
    Config().save()
    print(f"wrote default config: {path}")
    print("next steps:")
    print("  1. pick a provider: edit llm.active")
    print("     (openai/anthropic/openrouter/groq/ollama/qwen27b-vllm)")
    print("  2. export the matching API key env var (not needed for ollama or qwen27b-vllm)")
    print("  3. run `clipponyai check-llm` to verify your provider is reachable")
    print("  4. run `clipponyai` — sprites download on first run")
    return 0


def _cmd_fetch_sprites(_args) -> int:
    from .sprite_fetch import fetch_sprites

    print(f"downloading Desktop Ponies sprites to {sprites_dir()} …")
    missing = fetch_sprites()
    print("done!" if missing == 0 else f"done with {missing} file(s) missing (retry later)")
    return 0 if missing == 0 else 1


def _cmd_tasks(_args) -> int:
    from .tasks import TaskStore

    store = TaskStore(db_path())
    print(store.overview())
    store.close()
    return 0


def _cmd_doctor(_args) -> int:
    import platform as _platform

    config = Config.load()
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "✓" if good else "✗"
        print(f" {mark} {label}" + (f" — {hint}" if hint and not good else ""))
        ok = ok and good

    def age_text(value: datetime | None) -> str:
        if value is None:
            return "never"
        seconds = max(0, int((datetime.now() - value).total_seconds()))
        if seconds < 120:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 120:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 48:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    def meta_datetime(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    print(f"clipponyai {__version__}")
    check(f"config: {config_path()}", config_path().exists(), "run `clipponyai init`")
    active = config.llm.active
    known = active in config.llm.providers
    check(f"provider: {active}", known, "llm.active must name an entry under llm.providers")
    if known:
        provider = config.llm.providers[active]
        missing = missing_api_key(provider)
        check(
            f"api key ({provider.api_key_env or 'not needed'})",
            missing is None,
            f"export {missing}=…",
        )
        check(
            f"models: fast={provider.fast_model} slow={provider.resolved_slow_model()} "
            f"vision={provider.resolved_vision_model()}",
            True,
        )
        # Local-provider health-check suggestion
        if provider.base_url and not provider.api_key_env:
            print(f"  (local endpoint {provider.base_url} — run `clipponyai check-llm` to verify)")
    from .sprite_fetch import have_sprites

    check(f"sprites: {sprites_dir()}", have_sprites(), "run `clipponyai fetch-sprites`")
    try:
        import PySide6  # noqa: F401

        check("PySide6 (desktop pony)", True)
    except ImportError:
        check("PySide6 (desktop pony)", False, "pip install PySide6 (or run --headless)")
    if config.telegram.enabled:
        try:
            import telegram  # noqa: F401

            has_lib = True
        except ImportError:
            has_lib = False
        check("telegram extra", has_lib, "pip install 'clipponyai[telegram]'")
        check(
            f"telegram token (${config.telegram.token_env})",
            bool(os.environ.get(config.telegram.token_env)),
            f"export {config.telegram.token_env}=… (from @BotFather)",
        )
        check(
            "telegram allowlist",
            bool(config.telegram.allowed_user_ids),
            "add your user id to telegram.allowed_user_ids or the bot answers nobody",
        )
    check(
        f"screen peeking: {'ON' if config.screenshot_enabled else 'off (private by default)'}", True
    )
    check(
        f"stay put: {'ON (pinned)' if config.ui.stay_put else 'off (she wanders and chases)'}", True
    )

    # Work-hours state
    wh = config.reminders.work_hours
    if wh.enabled:
        days = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        day_names = ", ".join(days[d] for d in wh.weekdays)
        print(
            f"  work hours: {wh.start}–{wh.end} ({day_names}), closing nudge={'on' if wh.closing_nudge else 'off'}"
        )
    else:
        print("  work hours: disabled")

    # Logwatch privacy/paths
    lw = config.logwatch
    if lw.enabled:
        print(
            f"  logwatch: enabled ({len(lw.files)} file(s), {lw.max_lines_per_file} lines/file, {lw.max_total_chars} chars total)"
        )
        for f in lw.files:
            exists = Path(f).exists()
            mark = "✓" if exists else "?"
            print(f"    {mark} {f} {'(found)' if exists else '(not found)'}")
    else:
        print("  logwatch: disabled (privacy-safe default)")

    # Autostart status
    from .install import autostart_status

    print(f"  autostart: {autostart_status()}")

    # Platform-specific screen permission guidance
    sys_name = _platform.system()
    if sys_name == "Darwin" and config.screenshot_enabled:
        print(
            "  macOS screen recording: grant permission in System Settings → Privacy & Security → Screen Recording"
        )
        print(
            "  macOS accessibility (cursor chase): grant in System Settings → Privacy & Security → Accessibility"
        )
    elif sys_name == "Darwin":
        print(
            "  macOS note: when you enable screen peeking, you will need to grant Screen Recording permission"
        )
        print(
            "  and Accessibility permission for cursor chasing in System Settings → Privacy & Security"
        )

    # Vision model capability report
    if known:
        vision_model = provider.resolved_vision_model()
        if provider.vision_model:
            print(f"  vision model: {vision_model} (multimodal)")
        else:
            print(f"  vision model: {vision_model} (inherited from slow/fast model)")

    # Proactive focus awareness status
    aw = config.awareness
    awareness_active = aw.enabled and config.screenshot_enabled
    if awareness_active:
        print(
            f"  awareness: on (interval={aw.interval_seconds}s, cooldown={aw.cooldown_minutes}m, confidence={aw.minimum_confidence})"
        )
    elif aw.enabled:
        print("  awareness: enabled but screenshot gate is off (screenshot_enabled=False)")
    else:
        print("  awareness: off (opt-in via awareness.enabled + screenshot_enabled)")

    from .accountability import get_stores
    from .tasks import _ECHO_REPAIR_META, TaskStore

    doctor_store = TaskStore(db_path())
    try:
        doctor_stores = get_stores(doctor_store)
        observation_store = doctor_stores["observations"]
        observation_count = observation_store.count()
        oldest_rows = observation_store.since(datetime.min, limit=1)
        newest = observation_store.latest()
        oldest = oldest_rows[0] if oldest_rows else None
        observation = config.observation
        if observation.enabled:
            print(
                "  screen observation: on "
                f"(sample={observation.sample_seconds}s, rows={observation_count}, "
                f"window titles={'on' if observation.capture_window_titles else 'off'})"
            )
        else:
            print(
                "  screen observation: off "
                f"(opt-in; rows retained={observation_count}, "
                f"window titles={'configured' if observation.capture_window_titles else 'off'})"
            )
        print(
            "    history: "
            f"oldest={age_text(oldest.started_at if oldest else None)}, "
            f"newest={age_text(newest.started_at if newest else None)}, "
            f"retention={observation.retention_days}d, max_rows={observation.max_rows}"
        )
        recent_observations = observation_store.recent(limit=300)
        if (
            observation.enabled
            and observation.capture_window_titles
            and recent_observations
            and all(not row.window_title for row in recent_observations)
        ):
            print(
                "    warning: window titles are enabled but recent rows are empty; "
                "grant macOS Screen Recording permission"
            )

        reflection = config.reflection
        last_run = meta_datetime(doctor_store.get_meta("reflection_last_run"))
        last_spoke = meta_datetime(doctor_store.get_meta("reflection_last_spoke"))
        if reflection.enabled:
            print(
                f"  reflection: on (interval={reflection.interval_minutes}m, "
                f"last run={age_text(last_run)}, last spoke={age_text(last_spoke)})"
            )
            print(
                "    fresh runs with an old last-spoke time are healthy: reflection is quiet by default"
            )
        else:
            print(
                "  reflection: off "
                f"(last run={age_text(last_run)}, last spoke={age_text(last_spoke)})"
            )

        repaired = doctor_store.get_meta(_ECHO_REPAIR_META)
        if repaired is None:
            print("  history repair: not yet run (happens on next start)")
        else:
            print(f"  history repair: done ({repaired} echoed/pre-persona rows removed)")
    finally:
        doctor_store.close()

    # First-run next steps
    if not have_sprites() or not config_path().exists():
        print("\nfirst-run next steps:")
        if not config_path().exists():
            print("  1. run `clipponyai init` to create your config")
        if not have_sprites():
            print("  2. run `clipponyai fetch-sprites` to download pony sprites")
        print("  3. edit llm.active in config and set your API key (or use ollama/qwen27b-vllm)")
        print("  4. run `clipponyai check-llm` to verify your provider")
        print("  5. run `clipponyai` to start the pony")

    print("all good ✨" if ok else "fix the ✗ items above")
    return 0 if ok else 1


def _cmd_autostart(args) -> int:
    from .install import autostart_status, disable_autostart, enable_autostart

    if args.action == "enable":
        msg = enable_autostart()
        # enable_autostart may return multiple semicolon-separated messages
        for line in msg.split("; "):
            print(line.strip())
    elif args.action == "disable":
        msg = disable_autostart()
        print(msg)
    else:
        msg = autostart_status()
        print(msg)
    return 0


def _cmd_install_desktop(_args) -> int:
    from .install import install_desktop

    msg = install_desktop()
    # install_desktop may return multiple semicolon-separated messages
    for line in msg.split("; "):
        print(line.strip())
    return 0


def _make_synthetic_vision_image() -> bytes:
    """Create a tiny in-memory PNG: left half red, right half blue.

    Uses only the stdlib — no Pillow, no disk I/O.
    Returns raw PNG bytes (64x32 pixels).
    """
    import struct
    import zlib

    width, height = 64, 32
    raw = b""
    for y in range(height):
        raw += b"\x00"  # filter byte (none)
        for x in range(width):
            if x < width // 2:
                raw += b"\xff\x00\x00"  # red
            else:
                raw += b"\x00\x00\xff"  # blue

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw))
    png += _chunk(b"IEND", b"")
    return png


def _cmd_check_llm(args) -> int:
    """Smoke-test the active LLM provider: send a minimal chat turn, return 0/1.

    With ``--vision``, sends a synthetic two-color image and verifies the model
    can read it (structured JSON output, deterministic comparison).
    """
    vision_mode = hasattr(args, "vision") and args.vision

    from open_agent_compiler import AgentDefinition, AgentHeader
    from open_agent_compiler.interactive import build_interactive_spec, run_interactive
    from open_agent_compiler.interactive.runner import OpenAICompatClient

    config = Config.load()
    active = config.llm.active
    if active not in config.llm.providers:
        print(f"ERROR: provider {active!r} not configured")
        return 1
    provider_cfg = config.llm.providers[active]
    missing = missing_api_key(provider_cfg)
    if missing is not None:
        print(f"ERROR: environment variable {missing} is not set")
        return 1

    from .providers import FAST, make_live_profile

    agent = AgentDefinition(
        header=AgentHeader(
            agent_id="check-llm",
            name="check-llm",
            description="smoke test",
        ),
        usage_explanation_short="smoke",
        usage_explanation_long="smoke test",
        system_prompt="You are a helpful assistant. Answer briefly.",
    )

    if vision_mode:
        return _check_llm_vision(active, provider_cfg, agent)

    spec = build_interactive_spec(
        agent=agent,
        live_profile=make_live_profile(active, provider_cfg, FAST),
    )
    client = OpenAICompatClient.from_spec(spec)
    try:
        result = run_interactive(
            spec,
            "Say ok in one word.",
            client=client,
            max_tool_rounds=0,
        )
        if result.error:
            print(f"ERROR: {result.error}")
            return 1
        text = (result.output_text or "").strip()
        if not text:
            print("ERROR: empty response")
            return 1
        print(f"ok — {active} ({provider_cfg.fast_model}) replied: {text[:120]}")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


def _cmd_check_mcp(args) -> int:
    """Connect to configured MCP servers, report their status, and return 0/1."""

    from .mcp import MCPManager, ServerStatus

    config = Config.load()
    if not config.mcp.enabled:
        print("MCP disabled")
        return 0
    if not config.mcp.servers:
        print("MCP disabled (no servers configured)")
        return 0

    if args.server is not None:
        server = config.mcp.servers.get(args.server)
        if server is None:
            print(f"ERROR: MCP server {args.server!r} is not configured")
            return 1
        mcp_config = config.mcp.model_copy(update={"servers": {args.server: server}})
    else:
        mcp_config = config.mcp

    manager = MCPManager(mcp_config)
    ready = False
    try:
        manager.start()
        ready = manager.wait_ready(timeout=15)
        states = manager.status()
        tools_by_server: dict[str, list[str]] = {name: [] for name in states}
        for tool in manager.tools():
            tools_by_server.setdefault(tool.server, []).append(tool.namespaced_name)

        print("NAME  STATE      TOOLS  INSTRUCTIONS  DETAILS")
        all_connected = True
        for name, state in sorted(states.items()):
            status = state.status
            message = state.last_error or ""
            if not ready and status is ServerStatus.CONNECTING:
                status = ServerStatus.ERROR
                message = "timed out waiting 15s for initial connection"
            enabled = mcp_config.servers[name].enabled
            if enabled and status is not ServerStatus.CONNECTED:
                all_connected = False

            tool_names = ", ".join(sorted(tools_by_server.get(name, ()))) or "-"
            details = message or tool_names
            print(
                f"{name}  {status.value.upper():<9}  {state.tool_count:<5}  "
                f"{'yes' if state.instructions else 'no':<12}  {details}"
            )
        return 0 if all_connected else 1
    finally:
        manager.stop()


def _check_llm_vision(active, provider_cfg, agent) -> int:
    """Send a synthetic image through the same structured VISION lane as awareness."""
    import base64

    from open_agent_compiler.interactive import build_interactive_spec, run_interactive
    from open_agent_compiler.interactive.runner import OpenAICompatClient

    from .providers import VISION, make_live_profile

    vision_model = provider_cfg.resolved_vision_model()
    encoded = base64.b64encode(_make_synthetic_vision_image()).decode()
    schema = {
        "type": "object",
        "properties": {
            "left_color": {"type": "string"},
            "right_color": {"type": "string"},
        },
        "required": ["left_color", "right_color"],
    }
    spec = build_interactive_spec(
        agent=agent,
        live_profile=make_live_profile(active, provider_cfg, VISION),
    ).model_copy(update={"output_schema": schema})
    client = OpenAICompatClient.from_spec(spec)
    message = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Identify the dominant color of each vertical half. Return only "
                        "the requested left_color and right_color JSON fields."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                },
            ],
        }
    ]

    try:
        result = run_interactive(spec, message, client=client, max_tool_rounds=0)
    except Exception as exc:
        print(f"ERROR: vision check failed: {exc}")
        return 1
    if result.error:
        print(f"ERROR: vision check failed: {result.error}")
        return 1
    if not isinstance(result.structured, dict):
        print("FAIL — vision model did not return the requested structured result")
        return 1

    left = str(result.structured.get("left_color", "")).casefold().strip()
    right = str(result.structured.get("right_color", "")).casefold().strip()
    left_words = left.replace("-", " ").split()
    right_words = right.replace("-", " ").split()
    if "red" in left_words and "blue" in right_words:
        print(f"ok — {active} ({vision_model}) vision confirmed (left={left}, right={right})")
        return 0
    print(
        f"FAIL — {active} ({vision_model}) did not read the image correctly "
        f"(left={left!r}, right={right!r})"
    )
    return 1


def _cmd_run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    config = Config.load()
    if not config_path().exists():
        config.save()
        print(f"first run — wrote default config to {config_path()}")

    if args.headless:
        from .app import run_headless

        asyncio.run(run_headless(config))
        return 0

    from .sprite_fetch import fetch_sprites, have_sprites

    if not have_sprites():
        print("first run — downloading Desktop Ponies sprites (CC BY-NC-SA, personal use)…")
        fetch_sprites()

    from .app import run_gui

    return run_gui(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clipponyai",
        description="a cute desktop pony assistant — tasks, reminders, any LLM",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="run the assistant (default)")
    for p in (parser, run_parser):
        p.add_argument("--headless", action="store_true", help="no GUI: telegram + reminders only")
        p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("init", help="write a default config.yaml")
    sub.add_parser("fetch-sprites", help="download Desktop Ponies sprites")
    sub.add_parser("tasks", help="print the task overview")
    sub.add_parser("doctor", help="check your setup")
    check_parser = sub.add_parser("check-llm", help="smoke-test the active LLM provider")
    check_parser.add_argument(
        "--vision", action="store_true", help="test vision (image) capability"
    )
    mcp_parser = sub.add_parser("check-mcp", help="check configured MCP servers and tools")
    mcp_parser.add_argument("--server", metavar="NAME", help="check only one configured server")

    # autostart subcommand
    auto_parser = sub.add_parser("autostart", help="enable/disable/check autostart")
    auto_parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["enable", "disable", "status"],
        help="action (default: status)",
    )

    sub.add_parser("install-desktop", help="install desktop entry (Linux) / explain (macOS)")

    args = parser.parse_args(argv)
    commands = {
        None: _cmd_run,
        "run": _cmd_run,
        "init": _cmd_init,
        "fetch-sprites": _cmd_fetch_sprites,
        "tasks": _cmd_tasks,
        "doctor": _cmd_doctor,
        "check-llm": _cmd_check_llm,
        "check-mcp": _cmd_check_mcp,
        "autostart": _cmd_autostart,
        "install-desktop": _cmd_install_desktop,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
