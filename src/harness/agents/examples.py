"""Built-in example subagents for the manager pattern."""

from __future__ import annotations

from harness.agents.subagent import Subagent
from harness.tools.builtin import builtin_registry

RESEARCHER_INSTRUCTIONS = """\
You are a research subagent. Gather facts using your tools (read_file,
glob_files, grep_files, bash) and return a concise, factual summary of what
you found. Do not speculate beyond the evidence. Keep the summary under 200
words; state what you could not find as well as what you found.
"""

CODER_INSTRUCTIONS = """\
You are a coding subagent. Use your tools to inspect files, write or modify
code, run commands, and verify results. Return a short summary of what you
built or changed, including any verification output (e.g. exit codes, test
results). Be precise about file paths.

Write idiomatic Python 3.11 that passes this project's lint rules, i.e.
`uv run ruff check <files>` must exit clean on everything you write:
- Start every module with `from __future__ import annotations`.
- Use builtin generics (`list[T]`, `dict[str, T]`, `tuple[...]`); never
  `typing.List`, `typing.Dict`, `typing.Tuple` or `typing.Optional`.
- Use `X | None` for optional values, not `Optional[X]`.
- Annotate public function signatures and module-level names.
- No unused imports; import only what you use. Keep lines under 100 chars.
- Prefer small, pure functions with clear names.

After writing code, run `uv run pytest -q <test_file>` and
`uv run ruff check <files>`; fix any failure before finishing, and report the
actual output of both commands.
"""


def researcher() -> Subagent:
    return Subagent(
        name="researcher",
        description="Searches the workspace for facts and returns a research summary.",
        instructions=RESEARCHER_INSTRUCTIONS,
        tools=builtin_registry(),
        max_turns=8,
    )


def coder() -> Subagent:
    return Subagent(
        name="coder",
        description="Inspects, writes and runs code; returns a summary of changes.",
        instructions=CODER_INSTRUCTIONS,
        tools=builtin_registry(),
        max_turns=8,
    )


def example_subagents() -> list[Subagent]:
    """The default research + coding pair shown in the CLI /docs."""
    return [researcher(), coder()]
