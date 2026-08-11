"""CLI entry point: interactive REPL for the harness agent framework.

Usage::

    harness --help
    harness chat [--session <id>]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import TextIO

from rich.console import Console
from rich.panel import Panel

from harness.cli.commands import handle_command
from harness.cli.render import render_stream_event
from harness.config import Settings
from harness.core.compose import build_core_stack
from harness.core.messages import ToolCall
from harness.core.run_result import RunPaused
from harness.memory.store import Store
from harness.observability.logging import setup_logging
from harness.observability.tracing import Tracer
from harness.tools.mcp.client import MCPClientManager


def _force_utf8_stdio() -> None:
    """Reconfigure stdin/stdout/stderr to UTF-8.

    On Chinese/Japanese Windows the console defaults to a GBK/Shift-JIS
    codec, and rich's legacy renderer crashes on non-ASCII glyphs (e.g. the
    panel bullet). Forcing UTF-8 fixes both piped and interactive output.

    Piped stdin is the tricky one: a pipe decodes with the locale codec plus
    ``errors="surrogateescape"``, so UTF-8 bytes arrive as mojibake containing
    lone surrogates (``\\udca8``) — writing those to SQLite crashes with
    ``UnicodeEncodeError``. Interactive stdin uses the console API and is fine,
    so we only touch it when it is not a TTY.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass  # not a TextIOWrapper (already replaced) or unsupported
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Python production-grade AI agent harness.",
    )
    parser.add_argument("--env", default=None, help="Path to an env file (default: ./.env)")
    parser.add_argument("--model", default=None, help="Override the LLM model.")
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="Start an interactive REPL session.")
    chat.add_argument("--session", default=None, help="Session id to resume.")
    chat.add_argument(
        "--subagents",
        action="store_true",
        help="Enable built-in researcher/coder subagents (manager pattern).",
    )
    serve = sub.add_parser("serve", help="Run the Codex-style web UI.")
    serve.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    serve.add_argument("--port", type=int, default=8000, help="Port to bind.")
    serve.add_argument(
        "--reload", action="store_true", help="Auto-reload on source changes (dev)."
    )
    serve.add_argument("--model", default=None, help="Override the LLM model.")
    return parser


def _open_trace_file(path: str) -> TextIO:
    """Open the JSONL trace file for appending (sync; called outside the loop)."""
    return open(path, "a", encoding="utf-8")


def _make_approval_prompt(console: Console) -> Callable[[ToolCall], Awaitable[str]]:
    """Interactive y/n/a/e/p prompt for ASK-decided tool calls."""

    async def prompt(tool_call: ToolCall) -> str:
        console.print(
            f"\n[bold yellow]⚠️  Approval required:[/] [cyan]{tool_call.name}[/]"
            f"({tool_call.arguments})"
        )
        console.print(
            "[dim]  y = allow once   n = deny   a = allow for session  "
            "e = edit args   p = allow + pause after this turn[/]"
        )
        try:
            return await _aprompt(console, "  > ")
        except (EOFError, KeyboardInterrupt):
            return "n"  # interrupted -> fail closed

    return prompt


async def _aprompt(console: Console, prompt: str) -> str:
    """Prompt on stdin without blocking the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


async def _run_chat(args: argparse.Namespace, settings: Settings) -> int:
    console = Console()
    if not settings.api_key:
        console.print(
            "[red]No API key configured.[/]\n"
            "  Create a [bold].env[/] with [cyan]DEEPSEEK_API_KEY=sk-...[/] "
            "(see [bold].env.example[/]), or pass [cyan]--env <path>[/]."
        )
        return 1

    store = Store(settings)
    await store.initialize()

    # Mutable holder so "p" (allow + pause) can flag the next turn boundary.
    pause_after_turn: list[bool] = [False]
    # JSONL trace of turn/tool events (harness.trace.jsonl by default).
    trace_stream = _open_trace_file(settings.trace_file)
    tracer = Tracer(trace_stream)

    try:
        stack = await build_core_stack(
            settings,
            store=store,
            prompt=_make_approval_prompt(console),
            on_pause=lambda: pause_after_turn.__setitem__(0, True),
            pause_check=lambda _state: pause_after_turn[0],
            hooks=tracer.make_hooks(),
        )
    except ValueError as exc:
        tracer.close()
        console.print(f"[red]Sandbox config error:[/] {exc}")
        return 1

    agent, runner, planner = stack.agent, stack.runner, stack.planner
    skill_registry, permissions, sandbox = (
        stack.skill_registry,
        stack.permissions,
        stack.sandbox,
    )

    if settings.sandbox_mode != "local":
        available = await sandbox.check_available()
        if not available:
            console.print(
                f"[yellow]Sandbox unavailable ({settings.sandbox_mode}); "
                "bash calls will fail until a host is reachable.[/]"
            )
        else:
            console.print(f"[dim]sandbox: {settings.sandbox_mode}[/]")
    mcp = MCPClientManager()

    if args.subagents:
        from harness.agents.examples import example_subagents
        from harness.agents.orchestrator import add_subagents

        add_subagents(agent, runner, example_subagents())
        console.print(
            "[dim]subagents enabled: delegate_to_researcher, delegate_to_coder[/]"
        )

    session_id: str | None = None
    if args.session:
        if await store.sessions.get_session(args.session):
            session_id = args.session
        else:
            console.print(f"[yellow]No such session {args.session!r}; starting fresh.[/]")
    if session_id is None:
        session_id = (await store.sessions.create_session()).id

    # Mutable holder so slash-commands can swap the active session id.
    holder: list[str | None] = [session_id]

    console.print(
        Panel.fit(
            f"harness {agent.name} • model {agent.model}\n"
            f"session [cyan]{session_id}[/] • type /help for commands",
            border_style="cyan",
        )
    )

    while True:
        try:
            raw = await _aprompt(console, "> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye.")
            break

        line = raw.strip()
        if not line:
            continue

        if line.startswith("/"):
            if await handle_command(line, console=console, store=store, agent=agent,
                                    current_session=holder, mcp=mcp, planner=planner,
                                    runner=runner, permissions=permissions,
                                    skills=skill_registry):
                break
            session_id = holder[0]
            continue

        # Run the agent, streaming events. A paused run saves a checkpoint the
        # user can restore later with /resume <id>.
        pause_after_turn[0] = False
        try:
            async for event in runner.run_streamed(agent, line, session_id=session_id):
                render_stream_event(event, console)
        except RunPaused as exc:
            checkpoint_id = await store.sessions.save_checkpoint(exc.state)
            console.print(
                f"\n[yellow]⏸ Paused. Checkpoint saved:[/] [cyan]{checkpoint_id}[/] "
                "(resume with /resume <id>)"
            )

    tracer.close()
    await mcp.close()
    await store.close()
    return 0


async def _run_chat_main(args: argparse.Namespace) -> int:
    """Load settings, configure logging, and run the interactive REPL."""
    # Only pass --env when given; passing None would skip the default .env.
    settings = Settings.load(env_path=args.env) if args.env else Settings.load()
    if args.model:
        settings = settings.replace(model=args.model)
    setup_logging(settings.log_level, settings.log_file)
    return await _run_chat(args, settings)


def _run_serve(args: argparse.Namespace) -> int:
    """Boot uvicorn for the web UI (blocks; called OUTSIDE asyncio.run).

    The import-string + ``factory=True`` form is what makes ``--reload`` work:
    uvicorn re-imports the zero-arg factory in a reloader subprocess. ``--model``
    is exported to the environment so the factory's ``Settings.load()`` picks it
    up (uvicorn builds the app, not us).
    """
    import uvicorn

    if args.env:
        Settings.load(env_path=args.env)  # loads the env file into os.environ
    if args.model:
        os.environ["DEEPSEEK_MODEL"] = args.model
    uvicorn.run(
        "harness.web.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def _main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        return _run_serve(args)
    return asyncio.run(_run_chat_main(args))


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
