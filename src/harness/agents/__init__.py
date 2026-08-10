"""Multi-agent orchestration: subagents and manager-style delegation."""

from harness.agents.orchestrator import add_subagents, subagent_as_tool
from harness.agents.subagent import Subagent

__all__ = ["Subagent", "subagent_as_tool", "add_subagents"]
