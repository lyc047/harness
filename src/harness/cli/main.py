"""CLI entry point: interactive REPL for the harness agent framework.

Usage::

    harness --help
    harness chat [--session <id>]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from harness.cli.commands import handle_command
from harness.cli.render import render_stream_event
from harness.config import Settings
from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.run_result import RunPaused
from harness.core.runner import Runner, default_executor
from harness.llm.registry import get_provider
from harness.memory.preferences import make_remember_preference_tool
from harness.memory.store import Store
from harness.observability.logging import get_logger, setup_logging
from harness.planning.planner import Planner
from harness.safety.approver import ApprovalExecutor
from harness.safety.permissions import Permissions
from harness.sandbox import SandboxedExecutor, build_sandbox
from harness.skills.loader import make_create_skill_tool
from harness.skills.registry import SkillRegistry
from harness.tools.builtin import builtin_registry
from harness.tools.mcp.client import MCPClientManager

logger = get_logger("cli")


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
    chat.add_argument(
        "--subagents",
        action="store_true",
        help="Enable built-in researcher/coder subagents (manager pattern).",
    )
    return parser


def _default_agent(settings: Settings) -> Agent:
    return Agent(
        name="assistant",
        instructions=DEFAULT_INSTRUCTIONS,
        tools=builtin_registry(),
        model=settings.model,
        max_turns=settings.max_turns,
    )


def _load_permissions(settings: Settings) -> Permissions:
    """Load the TOML policy file, falling back to safe defaults."""
    path = Path(settings.permissions_file)
    if path.exists():
        try:
            return Permissions.from_config(path)
        except Exception as exc:  # noqa: BLE001 — a broken policy must not crash the REPL
            logger.warning("failed to load %s (%s); using defaults", path, exc)
    return Permissions.default_harness()


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

    provider = get_provider(settings)
    agent = _default_agent(settings)

    # Self-evolving skills + user preferences: expose the tools and inject any
    # discovered skills into the agent's system prompt so they apply from turn 1.
    skill_registry = SkillRegistry(settings.skills_dir)
    skill_registry.discover()
    agent.tools.register(make_create_skill_tool(skill_registry))
    agent.tools.register(make_remember_preference_tool(store.preferences))
    agent.instructions = skill_registry.inject(agent.instructions)

    # Sandbox: bash runs through the configured provider (local dev default,
    # remote SSH for isolation). Approval wraps it so humans see commands first.
    try:
        sandbox = build_sandbox(settings)
    except ValueError as exc:
        console.print(f"[red]Sandbox config error:[/] {exc}")
        return 1
    sandboxed = SandboxedExecutor(default_executor, sandbox)

    # Human-in-the-loop: ASK-decided tools prompt the user; "p" pauses after
    # the current turn, saving a checkpoint the /resume command can restore.
    permissions = _load_permissions(settings)
    pause_after_turn: list[bool] = [False]
    approval = ApprovalExecutor(
        sandboxed,
        permissions,
        prompt=_make_approval_prompt(console),
        on_pause=lambda: pause_after_turn.__setitem__(0, True),
    )
    runner = Runner(
        provider,
        session_store=store.sessions,
        tool_executor=approval,
        pause_check=lambda _state: pause_after_turn[0],
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
    planner = Planner(provider, settings.model)
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

    await mcp.close()
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
