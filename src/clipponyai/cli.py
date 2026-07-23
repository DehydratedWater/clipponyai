"""Command line: run the pony, set things up, check your setup.

    clipponyai              run the desktop pony (fetches sprites on first run)
    clipponyai --headless   run without GUI (telegram + reminders only)
    clipponyai init         write a default config.yaml and show its path
    clipponyai fetch-sprites  download Desktop Ponies sprites now
    clipponyai tasks        print the current task overview
    clipponyai doctor       check config, provider keys, sprites, extras
    clipponyai check-llm    smoke-test the active LLM provider (returns 0/1)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

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
    print("  1. pick a provider: edit llm.active (openai/anthropic/openrouter/groq/ollama)")
    print("  2. export the matching API key env var (not needed for ollama)")
    print("  3. run `clipponyai` — sprites download on first run")
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
    config = Config.load()
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "✓" if good else "✗"
        print(f" {mark} {label}" + (f" — {hint}" if hint and not good else ""))
        ok = ok and good

    print(f"clipponyai {__version__}")
    check(f"config: {config_path()}", config_path().exists(), "run `clipponyai init`")
    active = config.llm.active
    known = active in config.llm.providers
    check(f"provider: {active}", known, "llm.active must name an entry under llm.providers")
    if known:
        provider = config.llm.providers[active]
        missing = missing_api_key(provider)
        check(
            f"api key ({provider.api_key_env or 'not needed'})", missing is None,
            f"export {missing}=…",
        )
        check(f"models: fast={provider.fast_model} slow={provider.resolved_slow_model()} "
              f"vision={provider.resolved_vision_model()}", True)
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
        check("telegram allowlist", bool(config.telegram.allowed_user_ids),
              "add your user id to telegram.allowed_user_ids or the bot answers nobody")
    check(f"screen peeking: {'ON' if config.screenshot_enabled else 'off (private by default)'}",
          True)
    print("all good ✨" if ok else "fix the ✗ items above")
    return 0 if ok else 1


def _cmd_autostart(args) -> int:
    from .install import autostart_status, disable_autostart, enable_autostart

    if args.action == "enable":
        msg = enable_autostart()
    elif args.action == "disable":
        msg = disable_autostart()
    else:
        msg = autostart_status()
    print(msg)
    return 0


def _cmd_install_desktop(_args) -> int:
    from .install import install_desktop

    msg = install_desktop()
    print(msg)
    return 0


def _cmd_check_llm(_args) -> int:
    """Smoke-test the active LLM provider: send a minimal chat turn, return 0/1."""
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
    spec = build_interactive_spec(
        agent=agent,
        live_profile=make_live_profile(active, provider_cfg, FAST),
    )
    client = OpenAICompatClient.from_spec(spec)
    try:
        result = run_interactive(
            spec, "Say ok in one word.", client=client, max_tool_rounds=0,
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
        p.add_argument("--headless", action="store_true",
                       help="no GUI: telegram + reminders only")
        p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("init", help="write a default config.yaml")
    sub.add_parser("fetch-sprites", help="download Desktop Ponies sprites")
    sub.add_parser("tasks", help="print the task overview")
    sub.add_parser("doctor", help="check your setup")
    sub.add_parser("check-llm", help="smoke-test the active LLM provider")

    # autostart subcommand
    auto_parser = sub.add_parser("autostart", help="enable/disable/check autostart")
    auto_parser.add_argument(
        "action", nargs="?", default="status",
        choices=["enable", "disable", "status"],
        help="action (default: status)",
    )

    sub.add_parser("install-desktop", help="install desktop entry (Linux) / explain (macOS)")

    args = parser.parse_args(argv)
    commands = {
        None: _cmd_run, "run": _cmd_run, "init": _cmd_init,
        "fetch-sprites": _cmd_fetch_sprites, "tasks": _cmd_tasks,
        "doctor": _cmd_doctor, "check-llm": _cmd_check_llm,
        "autostart": _cmd_autostart, "install-desktop": _cmd_install_desktop,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
