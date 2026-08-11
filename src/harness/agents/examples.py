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

SEARCH_INSTRUCTIONS = """\
You are a search subagent. Your job is to LOCATE things in the workspace, not
to analyze them. Use glob_files, grep_files and bash (find/grep) to answer
"where is X", "which files contain Y", "what matches this pattern".

Report exact file paths and line numbers, with at most one line of surrounding
context each. Do not synthesize, evaluate, or recommend — the parent does that.
If something is not found, say so explicitly instead of guessing. Keep the
list tight and under 200 words.
"""

FILE_HANDLER_INSTRUCTIONS = """\
You are a file-handling subagent. You work on files that are data, config, or
documents — NOT code (code belongs to the coder subagent). Typical tasks:

- read a batch of files and summarize what is in each;
- convert between formats: JSON / YAML / TOML / CSV / markdown tables;
- fix encodings and line endings;
- extract, merge, split, or reorder sections of a file;
- bulk rename, dedupe, or clean up files with bash.

Preserve content semantics unless the task says otherwise. Use read_file,
write_file and bash; verify your changes (re-read or run a check) before
finishing. Return a short summary: what you did, the file paths touched, and
what changed.
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


# Every built-in subagent returns its result in this shape, so the parent can
# verify the delivery against what it asked for (see the delegation protocol
# in orchestrator.py) even though it never sees the subagent's internals.
DELIVERY_CONTRACT = """\
## Delivery contract

Return your final message as plain text with three parts:
1. WHAT YOU DID — the actual steps (files read, commands run).
2. KEY FINDINGS / RESULT — the substantive answer, with file paths.
3. GAPS — anything you could not determine, or open questions.

Keep it under 200 words unless the task asks for more. If you wrote files,
lead with their paths and what each is for.
"""


def _with_delivery(instructions: str) -> str:
    return f"{instructions.rstrip()}\n\n{DELIVERY_CONTRACT}"


def researcher() -> Subagent:
    return Subagent(
        name="researcher",
        description=(
            "Use when the task needs facts gathered from the workspace — files, "
            "code, git history, or searches. For research or investigation "
            "tasks, delegate to researcher by default instead of doing the "
            "search yourself; it returns a factual summary under 200 words."
        ),
        instructions=_with_delivery(RESEARCHER_INSTRUCTIONS),
        tools=builtin_registry(),
        max_turns=8,
    )


def coder() -> Subagent:
    return Subagent(
        name="coder",
        description=(
            "Use when code needs to be inspected, written, modified, or tested. "
            "For any coding task, delegate to coder by default instead of "
            "writing the code yourself; it runs the tests and returns "
            "verification output."
        ),
        instructions=_with_delivery(CODER_INSTRUCTIONS),
        tools=builtin_registry(),
        max_turns=8,
    )


def frontend_design() -> Subagent:
    return Subagent(
        name="frontend_design",
        description=(
            "Use when the task involves designing or reshaping frontend UI — new "
            "interfaces or restyling existing HTML/CSS/JS. For UI design work, "
            "delegate to frontend_design by default instead of designing "
            "yourself; it returns a design plan or implemented files."
        ),
        instructions=_with_delivery(
            _with_skill(FRONTEND_DESIGN_INSTRUCTIONS, "frontend-design")
        ),
        tools=builtin_registry(),
        max_turns=10,
    )


def doc_writer() -> Subagent:
    return Subagent(
        name="doc_writer",
        description=(
            "Use when the task involves writing or co-authoring documentation — "
            "docs, proposals, specs, decision docs, RFCs. For documentation "
            "work, delegate to doc_writer by default instead of drafting "
            "yourself; it returns the document path and a summary."
        ),
        instructions=_with_delivery(
            _with_skill(DOC_WRITER_INSTRUCTIONS, "doc-coauthoring")
        ),
        tools=builtin_registry(),
        max_turns=12,
    )


def search() -> Subagent:
    return Subagent(
        name="search",
        description=(
            "Use when the task is to locate things in the workspace — find "
            "files, grep for symbols or text, match glob patterns. For locating "
            "work, delegate to search by default; it keeps the searching out of "
            "your context and returns exact paths."
        ),
        instructions=_with_delivery(SEARCH_INSTRUCTIONS),
        tools=builtin_registry(),
        max_turns=6,
    )


def file_handler() -> Subagent:
    return Subagent(
        name="file_handler",
        description=(
            "Use when the task is handling files that are not code — reading a "
            "batch of files and summarizing, converting formats (JSON/YAML/CSV/"
            "markdown), fixing encodings or line endings, extracting or merging "
            "sections, bulk renames or cleanup. For file chores, delegate to "
            "file_handler by default; it keeps bulk file work out of your context."
        ),
        instructions=_with_delivery(FILE_HANDLER_INSTRUCTIONS),
        tools=builtin_registry(),
        max_turns=8,
    )


def example_subagents() -> list[Subagent]:
    """The default subagent set shown in the CLI /docs."""
    return [
        researcher(),
        coder(),
        frontend_design(),
        doc_writer(),
        search(),
        file_handler(),
    ]
