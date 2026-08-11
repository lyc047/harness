"""Structured slash-command result builders, shared by REST and the WS runtime.

The CLI renders slash commands through rich with no structured return value, so
the web implements its own payload builders here. REST endpoints and the WS
``command`` handler both call into these functions so there is exactly one
implementation of each command result.
"""

from __future__ import annotations

from typing import Any

from harness.core.agent import Agent
from harness.core.messages import Message
from harness.memory.session import Session
from harness.memory.store import Store
from harness.safety.permissions import Permissions
from harness.skills.registry import SkillRegistry
from harness.tools.mcp.client import MCPClientManager
from harness.tools.mcp.manager import (
    build_mcp_config,
    register_mcp_server,
    unregister_mcp_server,
)

HELP_TEXT = """\
Commands:
  /help            show this help
  /new             start a fresh session (new conversation)
  /clear           wipe the current session's history
  /tools           list tools available to the agent
  /skills          list discovered skills
  /permissions     show the active permission policy
  /checkpoints     list saved run checkpoints
  /mcp <add|list|remove>   connect/manage MCP servers (stdio or HTTP)
  /plan <goal>     plan and execute a multi-step task step by step
"""


def help_payload() -> dict[str, Any]:
    """The ``/help`` command result."""
    return {"help": HELP_TEXT}


def tools_payload(agent: Agent) -> dict[str, Any]:
    """The ``/tools`` command result (name/description/parameter schema each)."""
    tools: list[dict[str, Any]] = []
    for name in agent.tools.names():
        tool = agent.tools.get(name)
        tools.append(
            {
                "name": name,
                "description": tool.description if tool else "",
                "parameters": tool.parameters_schema if tool else {},
            }
        )
    return {"tools": tools}


def skills_payload(registry: SkillRegistry) -> dict[str, Any]:
    """The ``/skills`` command result; re-scans so new skills are included."""
    registry.refresh()
    return {
        "skills": [{"name": s.name, "description": s.description} for s in registry.all()]
    }


def permissions_payload(permissions: Permissions) -> dict[str, Any]:
    """The ``/permissions`` command result."""
    return {"default": permissions.default.value, "toml": permissions.to_toml()}


async def checkpoints_payload(store: Store) -> dict[str, Any]:
    """The ``/checkpoints`` command result."""
    return {
        "checkpoints": [
            {"id": cid, "created_at": created}
            for cid, created in await store.sessions.list_checkpoints()
        ]
    }


async def mcp_payload(mcp: MCPClientManager, arg: str, agent: Agent) -> dict[str, Any]:
    """The ``/mcp`` command result: ``add stdio|http``, ``list``, or ``remove``.

    Executes against the connection's own manager (per-tab scope) and registers
    discovered tools onto ``agent.tools`` so the model can call them next turn.
    """
    sub, _, subarg = arg.partition(" ")
    sub = sub.strip().lower()
    subarg = subarg.strip()

    if sub == "add":
        transport, _, rest = subarg.partition(" ")
        name, _, tail = rest.partition(" ")
        config = build_mcp_config(transport.strip().lower(), name.strip(), tail.strip())
        if config is None:
            return {
                "ok": False,
                "message": "Usage: /mcp add stdio <name> <command> args...  |  "
                "/mcp add http <name> <url>",
            }
        try:
            names = await register_mcp_server(mcp, config, agent.tools)
        except Exception as exc:  # noqa: BLE001 — surface connect failures to the client
            return {
                "ok": False,
                "message": f"Failed to connect MCP server: {type(exc).__name__}: {exc}",
            }
        return {"ok": True, "action": "added", "name": config.name, "tools": names}

    if sub == "list":
        servers: list[dict[str, Any]] = []
        for server_name in mcp.servers:
            tools = [
                t.name for t in agent.tools.all() if getattr(t, "server", None) == server_name
            ]
            servers.append({"name": server_name, "tools": tools})
        return {"ok": True, "action": "list", "servers": servers}

    if sub == "remove":
        if not subarg:
            return {"ok": False, "message": "Usage: /mcp remove <name>"}
        if not mcp.is_connected(subarg):
            return {"ok": False, "message": f"Not connected: {subarg}"}
        unregister_mcp_server(subarg, agent.tools)
        try:
            await mcp.remove_server(subarg)
        except Exception as exc:  # noqa: BLE001 — removal must not crash the client
            return {
                "ok": False,
                "message": f"Failed to remove: {type(exc).__name__}: {exc}",
            }
        return {"ok": True, "action": "removed", "name": subarg}

    return {"ok": False, "message": "Usage: /mcp add|list|remove  (see /help)"}


def session_dict(session: Session) -> dict[str, Any]:
    """Serialize a :class:`Session` for the client (REST + WS frames)."""
    return {
        "id": session.id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "name": session.name,
        "parent_session_id": session.parent_session_id,
    }


async def new_session_payload(store: Store) -> dict[str, Any]:
    """The ``/new`` command result: create a session and return it."""
    session = await store.sessions.create_session()
    return {"session": session_dict(session)}


async def clear_payload(
    store: Store, session_id: str | None, agent: Agent
) -> dict[str, Any]:
    """The ``/clear`` command result: wipe history, keep the system prompt."""
    if not session_id:
        return {"ok": False, "message": "no active session to clear"}
    await store.sessions.save_messages(session_id, [Message.system(agent.instructions)])
    return {"ok": True, "session_id": session_id}
