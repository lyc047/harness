"""Permission policy engine for human-in-the-loop tool execution.

A :class:`Permissions` policy is a list of :class:`Rule` objects plus a
default. Each rule matches a tool call by name (exact match or ``fnmatch``
glob) and optionally by a regex over the JSON-encoded arguments. The effective
permission for a call is the strictest matching rule — **deny > ask > allow** —
so a deny rule always wins regardless of order, then ask beats an allow, and
unmatched calls fall back to the configured default (ask by default).

Policies can be loaded from a TOML file::

    default = "ask"

    [[rules]]
    tool = "read_file"
    permission = "allow"

    [[rules]]
    tool = "bash"
    permission = "ask"
    pattern = "rm -rf"
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from harness.core.messages import ToolCall


class Permission(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class Rule:
    """A single permission rule: tool pattern + optional arg pattern + action."""

    tool: str
    permission: Permission
    pattern: str | None = None

    def matches(self, tool_call: ToolCall) -> bool:
        if not fnmatch.fnmatch(tool_call.name, self.tool):
            return False
        if self.pattern is None:
            return True
        return re.search(self.pattern, tool_call.arguments) is not None


class Permissions:
    """Evaluate tool calls against a set of rules."""

    def __init__(
        self,
        rules: list[Rule] | None = None,
        *,
        default: Permission = Permission.ASK,
    ) -> None:
        self._rules = rules or []
        self._default = default

    @classmethod
    def default_harness(cls) -> Permissions:
        """Safe-by-default policy: read-only tools allowed, everything else asks."""
        return cls(
            default=Permission.ASK,
            rules=[
                Rule("read_file", Permission.ALLOW),
                Rule("glob", Permission.ALLOW),
                Rule("grep", Permission.ALLOW),
            ],
        )

    @classmethod
    def from_config(cls, path: str | Path) -> Permissions:
        """Load a TOML policy file into a :class:`Permissions`."""
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        default = Permission(data.get("default", "ask"))
        rules: list[Rule] = []
        for r in data.get("rules", []):
            rules.append(
                Rule(
                    tool=r["tool"],
                    permission=Permission(r["permission"]),
                    pattern=r.get("pattern"),
                )
            )
        return cls(rules, default=default)

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    @property
    def default(self) -> Permission:
        return self._default

    def decide(self, tool_call: ToolCall) -> Permission:
        """Deny > ask > allow among matching rules; default only if none match."""
        has_deny = has_ask = has_allow = False
        for rule in self._rules:
            if not rule.matches(tool_call):
                continue
            if rule.permission is Permission.DENY:
                has_deny = True
            elif rule.permission is Permission.ASK:
                has_ask = True
            else:
                has_allow = True
        if has_deny:
            return Permission.DENY
        if has_ask:
            return Permission.ASK
        if has_allow:
            return Permission.ALLOW
        return self._default

    def to_toml(self) -> str:
        """Render the policy back to TOML (for scaffolding a config file)."""
        lines = [f"default = {self._default.value!r}", ""]
        for r in self._rules:
            lines.append("[[rules]]")
            lines.append(f"tool = {r.tool!r}")
            lines.append(f"permission = {r.permission.value!r}")
            if r.pattern is not None:
                lines.append(f"pattern = {r.pattern!r}")
            lines.append("")
        return "\n".join(lines)
