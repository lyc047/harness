"""Built-in example subagents for the manager pattern."""

from __future__ import annotations

from pathlib import Path

from harness.agents.subagent import Subagent
from harness.skills.registry import BUNDLED_SKILLS_DIR
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

FRONTEND_DESIGN_INSTRUCTIONS = """\
You are a frontend design subagent. Design distinctive, intentional UI — new
interfaces or reshaping existing ones — following the frontend-design skill
below. Work from the user's brief, propose a design plan (palette, type,
layout, a signature element), critique it against templated defaults, then
implement with write_file (HTML/CSS/JS) when asked. You may be asked to
improve this project's own web UI (frontend under src/harness/web/). Return
a short summary of the design decisions and any files you wrote. Prefer
vanilla HTML/CSS/JS; no build step unless the task requires one.
"""

DOC_WRITER_INSTRUCTIONS = """\
You are a documentation writing subagent. Co-author documentation with the
user (docs, proposals, technical specs, decision docs, RFCs) following the
doc-coauthoring skill below: gather context, build the document section by
section with brainstorming and surgical edits, then run a reader test to
catch blind spots. Draft and edit with write_file in the workspace. Return
a short summary of the document produced, its path, and any open questions
for the user.
"""


def _load_subagent_skill(name: str) -> str:
    """Read a subagent skill markdown file (frontmatter stripped).

    Subagent-only skills live in ``skills/subagents/`` — a subdirectory the
    main ``SkillRegistry`` does not scan (it only globs top-level ``*.md``),
    so they never leak into the main agent's prompt. The bundled copies ship
    with the package (``src/harness/skills/bundled/subagents/``), so a fresh
    clone gets them; a user's ``skills/subagents/<name>.md`` overrides.
    """
    candidates = [
        Path("skills") / "subagents" / f"{name}.md",
        Path(__file__).resolve().parents[3] / "skills" / "subagents" / f"{name}.md",
        BUNDLED_SKILLS_DIR / "subagents" / f"{name}.md",
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
    body = _load_subagent_skill(skill_name)
    if not body:
        return instructions
    return f"{instructions}\n\n# Skill: {skill_name}\n\n{body}"


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


def frontend_design() -> Subagent:
    return Subagent(
        name="frontend_design",
        description=(
            "Designs distinctive, intentional frontend UI (new interfaces or "
            "reshaping existing ones) and returns a design plan or implemented "
            "HTML/CSS/JS."
        ),
        instructions=_with_skill(FRONTEND_DESIGN_INSTRUCTIONS, "frontend-design"),
        tools=builtin_registry(),
        max_turns=10,
    )


def doc_writer() -> Subagent:
    return Subagent(
        name="doc_writer",
        description=(
            "Co-authors documentation with the user (docs, proposals, specs, "
            "decision docs) and returns the written document path and summary."
        ),
        instructions=_with_skill(DOC_WRITER_INSTRUCTIONS, "doc-coauthoring"),
        tools=builtin_registry(),
        max_turns=12,
    )


def example_subagents() -> list[Subagent]:
    """The default subagent set shown in the CLI /docs."""
    return [researcher(), coder(), frontend_design(), doc_writer()]
