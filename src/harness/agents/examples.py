"""Built-in example subagents for the manager pattern (registry-driven).

The six defaults are declared as YAML configs shipped under
``src/harness/skills/bundled/subagents/`` and loaded through
:class:`SubagentRegistry` — the set is data-driven, so users override or add
subagents via ``skills/subagents/*.yaml`` without touching code.
"""

from __future__ import annotations

from harness.agents.registry import default_subagent_registry
from harness.agents.subagent import Subagent


def example_subagents() -> list[Subagent]:
    """The default subagent set shown in the CLI /docs (bundled + overrides)."""
    registry = default_subagent_registry()
    return [registry.to_subagent(spec) for spec in registry.discover()]
