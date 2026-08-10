"""CLI entry point: interactive REPL for the harness agent framework.

Usage::

    harness --help
    harness chat [--session <id>]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.panel import Panel

from harness.cli.commands import handle_command
from harness.config import Settings
from harness.core.agent import Agent
from harness.core.runner import RunDone, Runner, ToolResultEvent
from harness.llm.base import StreamReasoning, StreamText, StreamToolCall
from harness.llm.registry import get_provider
from harness.memory.store import Store
from harness.observability.logging import get_logger, setup_logging
from harness.tools.builtin import builtin_registry

logger = get_logger("cli")


def _force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8.

    On Chinese/Japanese Windows the console defaults to a GBK/Shift-JIS
    codec, and rich's legacy renderer crashes on non-ASCII glyphs (e.g. the
    panel bullet). Forcing UTF-8 fixes both piped and interactive output.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass  # not a TextIOWrapper (already replaced) or unsupported


DEFAULT_INSTRUCTIONS = """\
You are a capable AI assistant running inside the 'harness' agent framework.
You have access to tools for reading, writing, searching and running shell
commands. Use them when they help answer the user's question.
Be concise, accurate, and prefer existing files over re-creating them.
"""


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
    return parser


def _default_agent(settings: Settings) -> Agent:
    return Agent(
        name="assistant",
        instructions=DEFAULT_INSTRUCTIONS,
        tools=builtin_registry(),
        model=settings.model,
        max_turns=settings.max_turns,
    )


async def _aprompt(console: Console, prompt: str) -> str:
    """Prompt on stdin without blocking the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


def _render_stream_event(event: object, console: Console) -> None:
    if isinstance(event, StreamText):
        console.print(event.text, end="")
    elif isinstance(event, StreamReasoning):
        console.print(f"[dim italic]{event.text}[/]", end="")
    elif isinstance(event, StreamToolCall) and event.tool_call:
        tc = event.tool_call
        console.print(f"\n[bold cyan]▶ {tc.name}[/]({tc.arguments})")
    elif isinstance(event, ToolResultEvent):
        body = event.result.content
        if len(body) > 800:
            body = body[:800] + f"\n… [dim](truncated {len(event.result.content)} chars)[/]"
        color = "red" if event.result.is_error else "green"
        console.print(
            Panel(body, title=f"← {event.tool_call.name}", border_style=color, expand=False)
        )


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

    provider = get_provider(settings)
    agent = _default_agent(settings)
    runner = Runner(provider, session_store=store.sessions)

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
                                    current_session=holder):
                break
            session_id = holder[0]
            continue

        # Run the agent, streaming events.
        async for event in runner.run_streamed(agent, line, session_id=session_id):
            if isinstance(event, RunDone):
                console.print("")
            else:
                _render_stream_event(event, console)

    await store.close()
    return 0


async def _main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Only pass --env when given; passing None would skip the default .env.
    settings = Settings.load(env_path=args.env) if args.env else Settings.load()
    if args.model:
        settings = settings.replace(model=args.model)
    setup_logging(settings.log_level, settings.log_file)

    if args.command == "chat":
        return await _run_chat(args, settings)
    parser.error(f"unknown command: {args.command}")
    return 2


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
