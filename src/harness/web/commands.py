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
from harness.memory.store import Store
from harness.safety.permissions import Permissions
from harness.skills.registry import SkillRegistry

HELP_TEXT = """\
Commands:
  /help            show this help
  /new             start a fresh session (new conversation)
  /clear           wipe the current session's history
  /tools           list tools available to the agent
  /skills          list discovered skills
  /permissions     show the active permission policy
  /checkpoints     list saved run checkpoints
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


async def new_session_payload(store: Store) -> dict[str, Any]:
    """The ``/new`` command result: create a session and return it."""
    session = await store.sessions.create_session()
    return {
        "session": {
            "id": session.id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
    }


async def clear_payload(
    store: Store, session_id: str | None, agent: Agent
) -> dict[str, Any]:
    """The ``/clear`` command result: wipe history, keep the system prompt."""
    if not session_id:
        return {"ok": False, "message": "no active session to clear"}
    await store.sessions.save_messages(session_id, [Message.system(agent.instructions)])
    return {"ok": True, "session_id": session_id}
