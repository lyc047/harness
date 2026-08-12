"""Slash-command handling for the REPL."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel

from harness.cli.render import render_stream_event
from harness.core.agent import Agent
from harness.core.messages import Message
from harness.core.runner import Runner
from harness.memory.store import Store
from harness.planning.executor import PlanDone, PlanExecutor, PlanRevised, StepEnd, StepStart
from harness.planning.planner import Planner
from harness.safety.permissions import Permissions
from harness.skills.registry import SkillRegistry
from harness.tools.mcp.client import MCPClientManager
from harness.tools.mcp.manager import build_mcp_config, register_mcp_server, unregister_mcp_server

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
  /checkpoints      list saved run checkpoints
  /resume <id>      resume a paused run from a checkpoint
  /permissions      show the active permission policy
  /skills           list discovered skills
  /skill load <name>  inject a skill into the current session
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
    permissions: Permissions | None = None,
    skills: SkillRegistry | None = None,
    concurrent: bool = False,
    subagent_budget: Any | None = None,
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

    elif cmd == "/checkpoints":
        await _checkpoints_command(console=console, store=store)

    elif cmd == "/resume":
        await _resume_command(
            arg,
            console=console,
            store=store,
            agent=agent,
            runner=runner,
            current_session=current_session,
            concurrent=concurrent,
            subagent_budget=subagent_budget,
        )

    elif cmd == "/permissions":
        if permissions is None:
            console.print("[yellow]No permission policy attached.[/]")
        else:
            console.print(f"[cyan]default={permissions.default.value}[/]")
            console.print(permissions.to_toml(), markup=False)

    elif cmd == "/skills":
        if skills is None:
            console.print("[yellow]No skill registry attached.[/]")
        else:
            skills.refresh()
            names = skills.names()
            if not names:
                console.print(
                    f"No skills found in {skills.directory}. The agent can create "
                    "one with the create_skill tool."
                )
            else:
                for name in names:
                    skill = skills.get(name)
                    desc = skill.description if skill else ""
                    console.print(f"  [cyan]{name}[/] — {desc}")

    elif cmd == "/skill":
        await _skill_command(
            arg, console=console, store=store, agent=agent, skills=skills,
            current_session=current_session,
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
        name, _, tail = rest.partition(" ")
        config = build_mcp_config(transport.strip().lower(), name.strip(), tail.strip())
        if config is None:
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


async def _checkpoints_command(*, console: Console, store: Store) -> None:
    """Handle ``/checkpoints``: list saved run checkpoints."""
    checkpoints = await store.sessions.list_checkpoints()
    if not checkpoints:
        console.print("No saved checkpoints. Pause a run (p at an approval prompt) to create one.")
        return
    for cid, created in checkpoints:
        console.print(f"  [cyan]{cid}[/]  (saved {created})")


async def _resume_command(
    arg: str,
    *,
    console: Console,
    store: Store,
    agent: Agent,
    runner: Runner,
    current_session: list[str | None],
    concurrent: bool = False,
    subagent_budget: Any | None = None,
) -> None:
    """Handle ``/resume <id>``: continue a paused run from its checkpoint.

    A resumed run starts a fresh per-run subagent budget and, when advanced
    orchestration is active, resumes with concurrent multi-tool execution.
    """
    checkpoint_id = arg.strip()
    if not checkpoint_id:
        console.print("[yellow]Usage:[/] /resume <checkpoint-id>")
        return
    state = await store.sessions.load_checkpoint(checkpoint_id)
    if state is None:
        console.print(f"[red]No such checkpoint:[/] {checkpoint_id}")
        return

    if state.session_id:
        current_session[0] = state.session_id
    console.print(
        f"[cyan]Resuming[/] checkpoint [cyan]{checkpoint_id}[/] "
        f"(turn {state.turns}, session {state.session_id or '-'})…"
    )
    if subagent_budget is not None:
        subagent_budget.reset()
    async for event in runner.resume_streamed(
        agent, state, session_id=state.session_id, concurrent=concurrent
    ):
        render_stream_event(event, console)


async def _skill_command(
    arg: str,
    *,
    console: Console,
    store: Store,
    agent: Agent,
    skills: SkillRegistry | None,
    current_session: list[str | None],
) -> None:
    """Handle ``/skill load <name>``: inject a skill into the active session."""
    sub, _, name = arg.partition(" ")
    if sub.strip().lower() != "load":
        console.print("[yellow]Usage:[/] /skill load <name>")
        return
    name = name.strip()
    if not name:
        console.print("[yellow]Usage:[/] /skill load <name>")
        return
    if skills is None:
        console.print("[yellow]No skill registry attached.[/]")
        return

    skills.refresh()
    skill = skills.get(name)
    if skill is None:
        console.print(f"[red]No such skill:[/] {name}")
        return

    session_id = current_session[0]
    if not session_id:
        console.print("[yellow]No active session to inject into.[/]")
        return

    block = skills.to_prompt_block(names=[name])
    messages = await store.sessions.load_messages(session_id)
    if not messages or messages[0].role != "system":
        messages.insert(0, Message.system(agent.instructions))
    messages[0] = Message.system((messages[0].content or "") + "\n\n" + block)
    await store.sessions.save_messages(session_id, messages)
    console.print(f"[green]Loaded skill[/] [cyan]{name}[/] into the current session.")
