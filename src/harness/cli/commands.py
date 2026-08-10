"""Slash-command handling for the REPL."""

from __future__ import annotations

from rich.console import Console

from harness.core.agent import Agent
from harness.core.messages import Message
from harness.memory.store import Store

HELP_TEXT = """\
Commands:
  /help            show this help
  /exit, /quit     leave the REPL
  /new             start a fresh session (new conversation)
  /session         list saved sessions
  /session <id>    resume a saved session
  /tools           list tools available to the agent
  /clear           wipe the current session's history
"""


async def handle_command(
    line: str,
    *,
    console: Console,
    store: Store,
    agent: Agent,
    current_session: list[str | None],
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

    else:
        console.print(f"[yellow]Unknown command:[/] {line}  (try /help)")

    return False
