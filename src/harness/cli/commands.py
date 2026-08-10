"""Slash-command handling for the REPL."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel

from harness.cli.render import render_stream_event
from harness.core.agent import Agent
from harness.core.messages import Message
from harness.core.runner import Runner
from harness.memory.store import Store
from harness.planning.executor import PlanDone, PlanExecutor, PlanRevised, StepEnd, StepStart
from harness.planning.planner import Planner
from harness.tools.mcp.client import MCPClientManager, MCPServerConfig
from harness.tools.mcp.manager import register_mcp_server, unregister_mcp_server

HELP_TEXT = """\
Commands:
  /help            show this help
  /exit, /quit     leave the REPL
  /new             start a fresh session (new conversation)
  /session         list saved sessions
  /session <id>    resume a saved session
  /tools           list tools available to the agent
  /clear           wipe the current session's history
  /mcp add stdio <name> <command> args...     connect a stdio MCP server
  /mcp add http <name> <url>                  connect an HTTP MCP server
  /mcp list         list connected MCP servers
  /mcp remove <name>  disconnect an MCP server
  /plan <goal>      plan and execute a multi-step task step by step
"""


async def handle_command(
    line: str,
    *,
    console: Console,
    store: Store,
    agent: Agent,
    current_session: list[str | None],
    mcp: MCPClientManager,
    planner: Planner,
    runner: Runner,
) -> bool:
    """Run a slash command. Returns True if the REPL should exit."""
    cmd, _, arg = line.partition(" ")
    cmd = cmd.strip().lower()
    arg = arg.strip()

    if cmd in {"/exit", "/quit"}:
        return True

    if cmd == "/help":
        console.print(HELP_TEXT)

    elif cmd == "/new":
        session = await store.sessions.create_session()
        current_session[0] = session.id
        console.print(f"[green]New session:[/] {session.id}")

    elif cmd == "/session":
        if arg:
            existing = await store.sessions.get_session(arg)
            if existing is None:
                console.print(f"[red]No such session:[/] {arg}")
            else:
                current_session[0] = arg
                console.print(f"[green]Resumed session:[/] {arg}")
        else:
            sessions = await store.sessions.list_sessions()
            if not sessions:
                console.print("No saved sessions.")
            else:
                for s in sessions:
                    marker = "*" if s.id == current_session[0] else " "
                    console.print(f" {marker} {s.id}  (updated {s.updated_at})")

    elif cmd == "/tools":
        for name in agent.tools.names():
            tool = agent.tools.get(name)
            desc = (tool.description.splitlines()[0] if tool else "") or ""
            console.print(f"  [cyan]{name}[/] — {desc}")

    elif cmd == "/clear":
        if current_session[0]:
            await store.sessions.save_messages(
                current_session[0], [Message.system(agent.instructions)]
            )
            console.print("[green]Cleared[/] session history (system prompt kept).")
        else:
            console.print("No active session to clear.")

    elif cmd == "/mcp":
        await _mcp_command(arg, console=console, agent=agent, mcp=mcp)

    elif cmd == "/plan":
        await _plan_command(
            arg,
            console=console,
            agent=agent,
            planner=planner,
            runner=runner,
            session_id=current_session[0],
        )

    else:
        console.print(f"[yellow]Unknown command:[/] {line}  (try /help)")

    return False


async def _mcp_command(
    arg: str, *, console: Console, agent: Agent, mcp: MCPClientManager
) -> None:
    """Handle the ``/mcp`` subcommands (add stdio|http, list, remove)."""
    sub, _, subarg = arg.partition(" ")
    sub = sub.strip().lower()
    subarg = subarg.strip()

    if sub == "add":
        transport, _, rest = subarg.partition(" ")
        transport = transport.strip().lower()
        name, _, tail = rest.partition(" ")
        name = name.strip()
        tail = tail.strip()

        if transport == "stdio" and name and tail:
            parts = tail.split()
            command, args = parts[0], parts[1:]
            if command.endswith(".py"):
                # Windows can't exec a .py directly; run it with the current
                # interpreter so `/mcp add stdio demo python path/server.py`
                # and `/mcp add stdio demo path/server.py` both work.
                command, args = sys.executable, [parts[0], *parts[1:]]
            config = MCPServerConfig(name=name, transport="stdio", command=command, args=args)
        elif transport == "http" and name and tail:
            config = MCPServerConfig(name=name, transport="http", url=tail)
        else:
            console.print(
                "[yellow]Usage:[/] /mcp add stdio <name> <command> args...  |  "
                "/mcp add http <name> <url>"
            )
            return

        try:
            names = await register_mcp_server(mcp, config, agent.tools)
        except Exception as exc:  # noqa: BLE001 — show connect failures clearly
            console.print(f"[red]Failed to connect MCP server:[/] {type(exc).__name__}: {exc}")
            return
        console.print(
            f"[green]Connected[/] {name} — {len(names)} tools: [cyan]{', '.join(names)}[/]"
        )

    elif sub == "list":
        if not mcp.servers:
            console.print(
                "No MCP servers connected. Use /mcp add stdio <name> <command> "
                "or /mcp add http <name> <url>."
            )
            return
        for server_name in mcp.servers:
            tools = [t for t in agent.tools.all() if getattr(t, "server", None) == server_name]
            console.print(
                f"  [cyan]{server_name}[/] — {len(tools)} tools: "
                f"{', '.join(t.name for t in tools)}"
            )

    elif sub == "remove":
        if not subarg:
            console.print("[yellow]Usage:[/] /mcp remove <name>")
        elif not mcp.is_connected(subarg):
            console.print(f"[yellow]Not connected:[/] {subarg}")
        else:
            unregister_mcp_server(subarg, agent.tools)
            try:
                await mcp.remove_server(subarg)
            except Exception as exc:  # noqa: BLE001 — removal must not crash the REPL
                console.print(f"[red]Failed to remove:[/] {type(exc).__name__}: {exc}")
                return
            console.print(f"[green]Removed[/] {subarg}")

    else:
        console.print("[yellow]Usage:[/] /mcp add|list|remove  (see /help)")


async def _plan_command(
    arg: str,
    *,
    console: Console,
    agent: Agent,
    planner: Planner,
    runner: Runner,
    session_id: str | None,
) -> None:
    """Handle ``/plan <goal>``: generate a plan, then execute it step by step."""
    goal = arg.strip()
    if not goal:
        console.print("[yellow]Usage:[/] /plan <goal>")
        return

    console.print("[cyan]Generating plan…[/]")
    try:
        plan = await planner.plan(goal)
    except Exception as exc:  # noqa: BLE001 — show planning failures clearly
        console.print(f"[red]Planning failed:[/] {type(exc).__name__}: {exc}")
        return

    console.print(Panel(plan.summary(), title="Plan", border_style="cyan"))

    executor = PlanExecutor(runner, planner)
    async for event in executor.execute_streamed(agent, plan, session_id=session_id):
        if isinstance(event, StepStart):
            console.print(f"\n[bold cyan]==> {event.step.id}. {event.step.title}[/]")
        elif isinstance(event, StepEnd):
            state = "[green]done[/]" if event.step.status == "done" else "[red]failed[/]"
            console.print(f"[{state}] {event.step.title}")
        elif isinstance(event, PlanRevised):
            console.print("\n[magenta]Plan revised:[/]")
            console.print(plan.summary())
        elif isinstance(event, PlanDone):
            console.print("\n[bold green]Plan complete:[/]")
            console.print(plan.summary())
        else:
            render_stream_event(event, console)
