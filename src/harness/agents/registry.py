"""Declarative subagent configs (YAML), mirroring the SkillRegistry layering.

A subagent is configured by one YAML file::

    name: researcher
    description: Use when ...
    instructions: |
      You are a research subagent...
    skill: researcher          # optional: load skills/subagents/<name>.md body
    model: ""                  # optional: per-subagent model override
    max_turns: 8

Configs ship with the package (``src/harness/skills/bundled/subagents/``); a
same-named file under the runtime ``skills/subagents/`` dir overrides it, so
users add new subagents or tweak built-ins without touching Python code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from harness.agents.subagent import Subagent
from harness.skills.registry import BUNDLED_SKILLS_DIR
from harness.tools.builtin import builtin_registry
from harness.tools.registry import ToolRegistry

# Subagent material lives in this subdirectory. The main SkillRegistry never
# scans it (it only globs top-level ``*.md``), so subagent skills/configs never
# leak into the main agent's prompt.
BUNDLED_SUBAGENTS_DIR = BUNDLED_SKILLS_DIR / "subagents"

# Every built-in subagent returns its result in this shape, so the parent can
# verify the delivery against what it asked for (see the delegation protocol
# in orchestrator.py) even though it never sees the subagent's internals.
DELIVERY_CONTRACT = """\
## Delivery contract

Return your final message as plain text with four parts:
1. WHAT YOU DID — the actual steps (files read, commands run).
2. KEY FINDINGS / RESULT — the substantive answer, with file paths.
3. RECOMMENDED NEXT STEP — what you would do next if you could: a file to
   read, a subagent to hand off to (name it), or "none — task complete".
4. GAPS — anything you could not determine, or open questions.

Keep it under 200 words unless the task asks for more. If the deliverable is
too big to fit in the reply (over ~200 words), SAVE IT TO A FILE and return
the path with a short summary — do not paste the whole thing into your
message; the parent reads the file. Lead with file paths and what each is for.
"""


def load_subagent_skill(name: str) -> str:
    """Read a subagent skill markdown file (frontmatter stripped).

    The runtime ``skills/subagents/<name>.md`` copy wins over the bundled one,
    so a user's edited skill body overrides the shipped default.
    """
    candidates = [
        Path("skills") / "subagents" / f"{name}.md",
        Path(__file__).resolve().parents[3] / "skills" / "subagents" / f"{name}.md",
        BUNDLED_SUBAGENTS_DIR / f"{name}.md",
    ]
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if text.startswith("---"):
                return text.split("---", 2)[-1].strip()
            return text.strip()
    return ""


def _with_skill(instructions: str, skill_name: str) -> str:
    """Append a loaded skill body to subagent instructions if it exists."""
    body = load_subagent_skill(skill_name)
    if not body:
        return instructions
    return f"{instructions}\n\n# Skill: {skill_name}\n\n{body}"


def _with_delivery(instructions: str) -> str:
    return f"{instructions.rstrip()}\n\n{DELIVERY_CONTRACT}"


def _build_tools(allowlist: tuple[str, ...]) -> tuple[ToolRegistry, tuple[str, ...]]:
    """Resolve a spec's tools into a builtin registry + carried mcp_* patterns.

    An empty allowlist means "all builtins" (backwards compatible). ``mcp_*``
    entries are not builtins — they are carried to the Subagent's
    ``mcp_allowlist`` and resolved against the parent's registry at delegation
    time (see orchestrator.resolve_mcp_tools). Unknown names are skipped.
    """
    builtins = builtin_registry()
    if not allowlist:
        return builtins, ()
    registry = ToolRegistry()
    mcp: list[str] = []
    for name in allowlist:
        if name.startswith("mcp_"):
            mcp.append(name)
        elif (t := builtins.get(name)) is not None:
            registry.register(t)
    return registry, tuple(mcp)


@dataclass
class SubagentSpec:
    """A parsed YAML config for one subagent (the declarative source of truth)."""

    name: str
    description: str = ""  # guides the parent on when to delegate (trigger text)
    instructions: str = ""  # the subagent's base system prompt
    skill: str = ""  # optional subagent skill name; body appended if present
    model: str = ""  # per-subagent model override; empty => inherit
    max_turns: int = 10
    tools: tuple[str, ...] = ()  # empty => all builtins (backwards compatible)
    contract: str = ""  # acceptance criteria appended to every delegation brief (#3)


class SubagentRegistry:
    """Scan a directory for subagent YAML configs (bundled then runtime).

    The runtime directory is ``<skills_dir>/subagents``: a same-named config
    there wins over the bundled default, and brand-new files add subagents.
    """

    def __init__(
        self,
        runtime_dir: str | Path,
        bundled_dir: str | Path | None = None,
    ) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._bundled_dir = Path(bundled_dir) if bundled_dir is not None else None
        self._specs: dict[str, SubagentSpec] = {}

    @staticmethod
    def _parse(path: Path) -> SubagentSpec | None:
        """Parse one YAML config; None when the file is unreadable/invalid."""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(data, dict):
            return None
        max_turns = data.get("max_turns")
        try:
            turns = int(max_turns) if max_turns not in (None, "") else 10
        except (TypeError, ValueError):
            turns = 10
        tools_raw = data.get("tools")
        if isinstance(tools_raw, list):
            tools = tuple(str(t) for t in tools_raw if isinstance(t, (str, int)))
        else:
            tools = ()
        return SubagentSpec(
            name=str(data.get("name") or path.stem),
            description=str(data.get("description") or ""),
            instructions=str(data.get("instructions") or ""),
            skill=str(data.get("skill") or ""),
            model=str(data.get("model") or ""),
            max_turns=turns,
            tools=tools,
            contract=str(data.get("contract") or ""),
        )

    def discover(self) -> list[SubagentSpec]:
        """(Re)scan bundled + runtime dirs and rebuild the index.

        Bundled configs are loaded first, then the runtime directory, so a
        same-named runtime file overrides the shipped default.
        """
        self._specs = {}
        sources: list[Path] = []
        if self._bundled_dir is not None:
            sources.append(self._bundled_dir)
        sources.append(self._runtime_dir)
        for source in sources:
            if not source.is_dir():
                continue
            for path in sorted(source.glob("*.yaml")):
                spec = self._parse(path)
                if spec is not None:
                    self._specs[spec.name] = spec
        return self.all()

    def refresh(self) -> list[SubagentSpec]:
        """Alias for discover(); re-scans so newly added configs take effect."""
        return self.discover()

    def get(self, name: str) -> SubagentSpec | None:
        if not self._specs:
            self.discover()
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def all(self) -> list[SubagentSpec]:
        return list(self._specs.values())

    def to_subagent(self, spec: SubagentSpec) -> Subagent:
        """Materialise a spec into a runnable :class:`Subagent`.

        The uniform delivery contract is always appended, so the parent can
        verify the result regardless of which subagent it delegated to.
        """
        instructions = spec.instructions
        if spec.skill:
            instructions = _with_skill(instructions, spec.skill)
        registry, mcp_allowlist = _build_tools(spec.tools)
        return Subagent(
            name=spec.name,
            description=spec.description,
            instructions=_with_delivery(instructions),
            tools=registry,
            mcp_allowlist=mcp_allowlist,
            model=spec.model,
            max_turns=spec.max_turns,
            contract=spec.contract,
        )


def default_subagent_registry() -> SubagentRegistry:
    """The registry used by the CLI/web: bundled defaults + runtime overrides."""
    return SubagentRegistry(
        Path("skills") / "subagents",
        bundled_dir=BUNDLED_SUBAGENTS_DIR,
    )
