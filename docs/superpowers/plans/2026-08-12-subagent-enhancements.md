# Subagent Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make multi-agent orchestration measurably more effective by (1) optimizing each subagent's prompt and delegation description, (2) giving subagents per-role tool allowlists instead of the current uniform five-tool set, and (3) letting MCP tools reach subagents under an explicit allowlist plus shipping a pluggable `web_search` builtin (free `cn.bing` default).

**Architecture:** A single plan with three ordered tasks, validated end-to-end with the existing multi-dimensional compare rubric (`scripts/e2e_subagents_compare.py`). The load-bearing mechanism change: a subagent's tool set is resolved **at delegation time** (not creation time), so MCP servers connected after startup flow into subagents that explicitly allow them. `SubagentSpec` gains `tools:` (builtin allowlist + `mcp_*` patterns); `Subagent` carries the `mcp_*` patterns; `SubagentTool.invoke` resolves the parent's current MCP tools against that allowlist per delegation.

**Tech Stack:** Python 3.11, existing harness core (`Runner`, `ToolRegistry`, `Subagent`, `SubagentTool`), YAML subagent configs, stdlib `urllib` + `html.parser` for the free web-search backend, existing MCP client infra.

## Global Constraints

- The OFF-path must stay byte-for-byte unchanged: when `HARNESS_SUBAGENTS=0`, none of these changes affect a run.
- Backwards compatible YAML: an existing subagent config without a `tools:` field keeps today's full builtin tool set.
- Subagent instructions stay lean — every added line costs tokens on every delegation. No prose bloat.
- Security boundary: subagents get **no MCP tools by default**; MCP tools reach a subagent only through an explicit `mcp_*` entry in its `tools:` allowlist.
- Quality gate on every task: `uv run ruff check . && uv run mypy src && uv run pytest -q` plus `node --check src/harness/web/static/js/app.js` (unchanged files exempt).
- No new runtime dependencies for the free web-search backend (stdlib only). `TavilyProvider` is optional and only activated by a `TAVILY_API_KEY` in `.env` — if Tavily is added it stays an optional extra, not a hard dependency.
- Never print, log, or echo `TAVILY_API_KEY` or any API key. It is a secret.

---

### Task 1: Targeted prompt and description optimization

**Files:**
- Modify: `src/harness/skills/bundled/subagents/researcher.yaml`
- Modify: `src/harness/skills/bundled/subagents/search.yaml`
- Modify: `src/harness/skills/bundled/subagents/coder.yaml`
- Modify: `src/harness/skills/bundled/subagents/doc_writer.yaml`
- Modify: `src/harness/skills/bundled/subagents/frontend_design.yaml`
- Modify: `src/harness/skills/bundled/subagents/file_handler.yaml`
- Modify: `src/harness/agents/orchestrator.py` (`DELEGATION_PROTOCOL_ADVANCED` at lines 68-76, and `DELEGATION_HINT` at lines 79-83)
- Test: `tests/test_subagent_prompts.py` (new)

**Interfaces:**
- Consumes: existing `SubagentSpec` YAML shape (`name`, `description`, `instructions`, `skill`, `model`, `max_turns`); `BUNDLED_SUBAGENTS_DIR` from `harness.agents.registry`.
- Produces: improved bundled YAML content; updated `DELEGATION_PROTOCOL_ADVANCED` / `DELEGATION_HINT`; a test asserting the bundled instructions contain the new guidance.
- Later tasks depend on: Task 2 edits the same six YAML files (appends `tools:`); Task 3 depends on `DELEGATION_PROTOCOL_ADVANCED` ending with `DELEGATION_PROTOCOL` unchanged (existing test `test_attach_delegation_protocol_swaps_variants` in `tests/test_agents.py` asserts `.endswith(...)` on both variants — the parent protocol text must stay a prefix of the advanced variant).

- [ ] **Step 1: Write the failing test** — create `tests/test_subagent_prompts.py`:

```python
"""Prompt/description sharpening for the bundled subagents and the advanced protocol."""

from __future__ import annotations

import yaml

from harness.agents.registry import BUNDLED_SUBAGENTS_DIR


def _bundled(name: str) -> dict:
    text = (BUNDLED_SUBAGENTS_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_researcher_points_at_web_search_and_doc_handoff() -> None:
    cfg = _bundled("researcher")
    assert "web_search" in cfg["description"]
    assert "web_search" in cfg["instructions"]
    assert "file:line" in cfg["instructions"]  # source attribution
    assert "doc_writer" in cfg["instructions"]  # handoff seed
    assert "under 200" in cfg["instructions"]  # length cap kept


def test_search_distinguishes_glob_vs_grep() -> None:
    cfg = _bundled("search")
    assert "glob_files" in cfg["instructions"]
    assert "grep_files" in cfg["instructions"]
    assert "path pattern" in cfg["instructions"]
    assert "content" in cfg["instructions"]
    # still workspace-only: search must not advertise web search
    assert "web_search" not in cfg["instructions"]
    assert "web_search" not in cfg["description"]


def test_coder_can_consult_web_search() -> None:
    cfg = _bundled("coder")
    assert "web_search" in cfg["instructions"]
    assert "cite the URL" in cfg["instructions"]


def test_writer_descriptions_sharpen_ownership() -> None:
    for name, marker in {
        "doc_writer": "not the parent's",
        "frontend_design": "not the parent's",
    }.items():
        assert marker in _bundled(name)["description"]


def test_file_handler_boundary_keeps_code_out() -> None:
    cfg = _bundled("file_handler")
    assert "NOT code" in cfg["description"]
    assert "coder" in cfg["description"]


def test_advanced_protocol_nudges_chaining() -> None:
    from harness.agents.orchestrator import DELEGATION_PROTOCOL_ADVANCED

    assert "RECOMMENDED NEXT STEP names a subagent" in DELEGATION_PROTOCOL_ADVANCED
    assert "router" in DELEGATION_PROTOCOL_ADVANCED
    assert "chaining best-fit subagents" in DELEGATION_PROTOCOL_ADVANCED


def test_advanced_hint_allows_nested_handoff() -> None:
    from harness.agents.orchestrator import DELEGATION_HINT

    assert "better-fit subagent" in DELEGATION_HINT
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_prompts.py -v`
Expected: FAIL — the new fragments do not exist yet.

- [ ] **Step 3: Apply the edits**

`researcher.yaml` — replace the whole file:

```yaml
name: researcher
description: >-
  Use when the task needs facts gathered from the workspace — files, code, git
  history — or from the web via `web_search`. For research or investigation
  tasks, delegate to researcher by default instead of doing the search
  yourself; it returns a factual summary under 200 words.
instructions: |
  You are a research subagent. Gather facts using your tools (read_file,
  glob_files, grep_files, bash, and web_search when the question needs
  up-to-date external information). Return a concise, factual summary of what
  you found. Do not speculate beyond the evidence. Keep the summary under 200
  words; state what you could not find as well as what you found.

  Cite what you found: `file:line` for workspace facts, `url` for web facts.
  If the parent will need these findings composed into a document, put
  `doc_writer` in RECOMMENDED NEXT STEP.
max_turns: 8
```

`search.yaml` — add the glob-vs-grep decision line to `instructions` (insert after the first paragraph):

```yaml
name: search
description: >-
  Use when the task is to locate things in the workspace — find files, grep for
  symbols or text, match glob patterns. For locating work, delegate to search by
  default; it keeps the searching out of your context and returns exact paths.
instructions: |
  You are a search subagent. Your job is to LOCATE things in the workspace, not
  to analyze them. Use glob_files, grep_files and bash (find/grep) to answer
  "where is X", "which files contain Y", "what matches this pattern".
  Use glob_files to locate by path pattern and grep_files to locate by content.

  Report exact file paths and line numbers, with at most one line of surrounding
  context each. Do not synthesize, evaluate, or recommend — the parent does that.
  If something is not found, say so explicitly instead of guessing. Keep the
  list tight and under 200 words.
max_turns: 6
```

`coder.yaml` — add one web_search line to `instructions` (right after "Be precise about file paths."):

```yaml
name: coder
description: >-
  Use when code needs to be inspected, written, modified, or tested. For any
  coding task, delegate to coder by default instead of writing the code
  yourself; it runs the tests and returns verification output.
instructions: |
  You are a coding subagent. Use your tools to inspect files, write or modify
  code, run commands, and verify results. Return a short summary of what you
  built or changed, including any verification output (e.g. exit codes, test
  results). Be precise about file paths.

  When you need an external API's signature or usage, use web_search if
  available and cite the URL.

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
max_turns: 8
```

`doc_writer.yaml` — replace only the `description`:

```yaml
name: doc_writer
description: >-
  Use when the task involves writing or co-authoring documentation — docs,
  proposals, specs, decision docs, RFCs. For ANY documentation work, delegate
  to doc_writer by default instead of drafting it yourself — document prose is
  doc_writer's job, not the parent's; it returns the document path and a summary.
skill: doc-coauthoring
instructions: |
  You are a documentation writing subagent. Co-author documentation with the
  user (docs, proposals, technical specs, decision docs, RFCs) following the
  doc-coauthoring skill below: gather context, build the document section by
  section with brainstorming and surgical edits, then run a reader test to
  catch blind spots. Draft and edit with write_file in the workspace. Return
  a short summary of the document produced, its path, and any open questions
  for the user.
max_turns: 12
```

`frontend_design.yaml` — replace only the `description`:

```yaml
name: frontend_design
description: >-
  Use when the task involves designing or reshaping frontend UI — new
  interfaces or restyling existing HTML/CSS/JS. For ANY UI design work, delegate
  to frontend_design by default instead of designing yourself — UI craft is
  frontend_design's job, not the parent's; it returns a design plan or
  implemented files.
skill: frontend-design
instructions: |
  You are a frontend design subagent. Design distinctive, intentional UI — new
  interfaces or reshaping existing ones — following the frontend-design skill
  below. Work from the user's brief, propose a design plan (palette, type,
  layout, a signature element), critique it against templated defaults, then
  implement with write_file (HTML/CSS/JS) when asked. You may be asked to
  improve this project's own web UI (frontend under src/harness/web/). Return
  a short summary of the design decisions and any files you wrote. Prefer
  vanilla HTML/CSS/JS; no build step unless the task requires one.
max_turns: 10
```

`file_handler.yaml` — replace only the `description`:

```yaml
name: file_handler
description: >-
  Use when the task is handling files that are not code — data, config, or
  document files (NOT code; code goes to coder). Reading a batch of files and
  summarizing, converting formats (JSON/YAML/CSV/markdown), fixing encodings or
  line endings, extracting or merging sections, bulk renames or cleanup. For
  file chores, delegate to file_handler by default; it keeps bulk file work out
  of your context.
instructions: |
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
max_turns: 8
```

`orchestrator.py` — replace the `DELEGATION_PROTOCOL_ADVANCED` block (lines 66-76):

```python
# Advanced mode: appended after DELEGATION_PROTOCOL so a level-1 subagent knows
# it can hand off a sub-task once more (structurally capped at two levels).
DELEGATION_PROTOCOL_ADVANCED = DELEGATION_PROTOCOL + """

## Deeper delegation (advanced mode)

You can also delegate a sub-task to another subagent via `delegate_to_<name>`,
the same way your parent delegates to you. Give it a complete, self-contained
brief. Nested delegation is at most two levels deep — never hand off a task
you can do yourself just to chain subagents.

When a subagent's RECOMMENDED NEXT STEP names a subagent that is the right
next handler for the remaining work, follow it — you are the router, and
chaining best-fit subagents is the point of advanced mode. Never chain to
avoid work you should do yourself.
"""
```

and replace `DELEGATION_HINT` (lines 79-83). The level-1 subagent is the one that must *decide* to hand off a sub-task for depth-2 to fire, so the hint gets the same nudge:

```python
# Short hint appended to level-1 subagents' agents in advanced mode.
DELEGATION_HINT = (
    "You can delegate a sub-task to another subagent via its `delegate_to_<name>` "
    "tool. Choose the best-fit subagent and give it a self-contained brief. "
    "Nested delegation is at most two levels deep. If the task points to a "
    "better-fit subagent for part of it, hand that part off too."
)
```

- [ ] **Step 4: Run the test + quality gate**

Run: `uv run pytest tests/test_subagent_prompts.py -v` → PASS.
Then: `uv run ruff check . && uv run mypy src && uv run pytest -q` → all green (existing `test_attach_delegation_protocol_swaps_variants` must still pass — the advanced variant is still `DELEGATION_PROTOCOL` plus a suffix, so `.endswith` holds).

- [ ] **Step 5: Commit**

```bash
git add src/harness/skills/bundled/subagents/ src/harness/agents/orchestrator.py tests/test_subagent_prompts.py
git commit -m "feat: sharpen subagent prompts + advanced chaining nudge"
```

---

### Task 2: Per-subagent tool allowlists

**Files:**
- Modify: `src/harness/agents/subagent.py` (`Subagent` dataclass — add `mcp_allowlist`)
- Modify: `src/harness/agents/registry.py` (`SubagentSpec`, `_parse`, `to_subagent`, new `_build_tools` helper)
- Modify: the six bundled YAMLs (append `tools:`)
- Test: `tests/test_subagent_registry.py` (new)

**Interfaces:**
- Consumes: `builtin_registry()` from `harness.tools.builtin` (names during this task: `bash`, `glob_files`, `grep_files`, `read_file`, `write_file`; `web_search` lands in Task 3); `ToolRegistry` from `harness.tools.registry`.
- Produces: `SubagentSpec.tools: tuple[str, ...]` (empty ⇒ all builtins); `Subagent.mcp_allowlist: tuple[str, ...]` (the `mcp_*` patterns carried for Task 3); `_build_tools(allowlist) -> tuple[ToolRegistry, tuple[str, ...]]`; `Subagent.tools` is the filtered `ToolRegistry`.
- Later tasks depend on: Task 3 reads `Subagent.mcp_allowlist` and resolves MCP tools against it at delegation time.

- [ ] **Step 1: Write the failing tests** — create `tests/test_subagent_registry.py`:

```python
"""Per-subagent tool allowlists (SubagentSpec.tools + Subagent.mcp_allowlist)."""

from __future__ import annotations

from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry
from harness.tools.builtin import builtin_registry


def test_bundled_subagents_resolve_exact_allowlists(tmp_path) -> None:
    """Each bundled subagent's tools == its allowlist ∩ currently-registered
    builtins. `web_search` lands in Task 3; until then it is simply skipped,
    so this assertion stays correct across both commits."""
    reg = SubagentRegistry(tmp_path / "empty", bundled_dir=BUNDLED_SUBAGENTS_DIR)
    specs = {s.name: s for s in reg.discover()}
    expect = {
        "search": ["glob_files", "grep_files", "bash"],
        "researcher": ["read_file", "glob_files", "grep_files", "bash", "web_search"],
        "coder": ["read_file", "write_file", "glob_files", "grep_files", "bash", "web_search"],
        "doc_writer": ["read_file", "write_file", "bash"],
        "file_handler": ["read_file", "write_file", "bash"],
        "frontend_design": ["read_file", "write_file", "bash"],
    }
    registered = set(builtin_registry().names())
    for name, want in expect.items():
        sa = reg.to_subagent(specs[name])
        assert sa.tools.names() == sorted(set(want) & registered), name
        # mcp_* patterns are carried separately, not registered as builtins
        assert sa.mcp_allowlist == (), name


def test_subagent_without_tools_keeps_full_builtin_set(tmp_path) -> None:
    """Absent `tools:` field (backwards compatible) => every builtin."""
    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "plain.yaml").write_text(
        "name: plain\n"
        "description: Use when delegating; delegate by default.\n"
        "instructions: Do things.\n",
        encoding="utf-8",
    )
    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("plain")
    assert spec is not None
    assert spec.tools == ()
    sa = reg.to_subagent(spec)
    assert sa.tools.names() == sorted(builtin_registry().names())


def test_mcp_patterns_carried_not_registered(tmp_path) -> None:
    """`mcp_*` entries are carried on the Subagent for Task-3 propagation and
    never resolve into builtin tool registrations."""
    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "mcpuser.yaml").write_text(
        "name: mcpuser\n"
        "description: Use when delegating; delegate by default.\n"
        "instructions: Use mcp.\n"
        "tools:\n"
        "  - read_file\n"
        "  - mcp_*\n",
        encoding="utf-8",
    )
    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("mcpuser")
    assert spec is not None
    sa = reg.to_subagent(spec)
    assert sa.tools.names() == ["read_file"]
    assert sa.mcp_allowlist == ("mcp_*",)


def test_unknown_tool_names_are_skipped(tmp_path) -> None:
    """A typo in `tools:` resolves to nothing and is skipped, not fatal."""
    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "sloppy.yaml").write_text(
        "name: sloppy\n"
        "description: Use when delegating; delegate by default.\n"
        "instructions: x.\n"
        "tools:\n"
        "  - read_file\n"
        "  - write_fil\n",
        encoding="utf-8",
    )
    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("sloppy")
    assert spec is not None
    sa = reg.to_subagent(spec)
    assert sa.tools.names() == ["read_file"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_subagent_registry.py -v`
Expected: FAIL — `SubagentSpec` has no `tools` field and `to_subagent` ignores it.

- [ ] **Step 3: Implement `tools:` on spec/parse/to_subagent + YAMLs**

`src/harness/agents/subagent.py` — add the field to the `Subagent` dataclass:

```python
@dataclass
class Subagent:
    """A self-contained agent config, runnable as a tool by a parent agent.

    Each delegation runs in a fresh, isolated context: the subagent never sees
    the parent's history, and its own history is discarded after the call —
    this is what keeps subagent work from polluting the main conversation.
    """

    name: str
    instructions: str
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    model: str = ""  # empty => inherit the parent's model
    max_turns: int = 10
    description: str = ""  # guides the parent on when to delegate
    mcp_allowlist: tuple[str, ...] = ()  # mcp_* patterns; resolved at delegation time
```

`src/harness/agents/registry.py` — add the field to `SubagentSpec`:

```python
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
```

`_parse` — read the `tools:` list (tolerate missing/bad values):

```python
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
        )
```

Add a module-level helper and rewrite `to_subagent`:

```python
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
```

```python
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
        )
```

Add the imports at the top of `registry.py` (the `ToolRegistry` import is new; `builtin_registry` is already imported):

```python
from harness.tools.builtin import builtin_registry
from harness.tools.registry import ToolRegistry
```

Append `tools:` to each bundled YAML (top-level key; the `name:` key stays first):

`search.yaml`:
```yaml
tools:
  - glob_files
  - grep_files
  - bash
```
`researcher.yaml`:
```yaml
tools:
  - read_file
  - glob_files
  - grep_files
  - bash
  - web_search
```
`coder.yaml`:
```yaml
tools:
  - read_file
  - write_file
  - glob_files
  - grep_files
  - bash
  - web_search
```
`doc_writer.yaml`:
```yaml
tools:
  - read_file
  - write_file
  - bash
```
`file_handler.yaml`:
```yaml
tools:
  - read_file
  - write_file
  - bash
```
`frontend_design.yaml`:
```yaml
tools:
  - read_file
  - write_file
  - bash
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/test_subagent_registry.py -v` → PASS. Then the full gate:

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
```

Existing `tests/test_agents.py` registry tests must still pass (they never assert tool sets).

- [ ] **Step 5: Commit**

```bash
git add src/harness/agents/subagent.py src/harness/agents/registry.py src/harness/skills/bundled/subagents/ tests/test_subagent_registry.py
git commit -m "feat: per-subagent tool allowlists (tools: in YAML)"
```

---

### Task 3: MCP tools reach subagents (allowlist) + pluggable `web_search`

**Files:**
- Modify: `src/harness/agents/orchestrator.py` (`SubagentTool.__init__`, `SubagentTool.invoke`, `subagent_as_tool`, `add_subagents`; add `resolve_mcp_tools` + `_matches_mcp_allowlist`)
- Create: `src/harness/tools/websearch/__init__.py` (protocol + factory)
- Create: `src/harness/tools/websearch/providers.py` (`BingProvider` default, `DuckDuckGoProvider`, `TavilyProvider`, parse helpers)
- Modify: `src/harness/tools/builtin/__init__.py` (register `web_search`)
- Create: `src/harness/tools/builtin/websearch.py` (tool wrapper)
- Modify: `src/harness/config.py` (fields + env reads)
- Test: `tests/test_websearch.py` (new), `tests/test_mcp_subagent_propagation.py` (new)

**Interfaces:**
- Consumes: `Subagent.mcp_allowlist` (Task 2); parent `ToolRegistry` (`agent.tools` in `add_subagents`); `@tool` decorator from `harness.tools.base`; `Settings` from `harness.config`.
- Produces: `resolve_mcp_tools(subagent, parent_tools) -> list[Tool]`; `WebSearchProvider` protocol; `select_websearch_provider(settings) -> WebSearchProvider`; `web_search(query, max_results=5) -> str` registered in `builtin_registry()`; `Settings.web_search_backend: str` / `Settings.tavily_api_key: str`.
- Later tasks depend on: the e2e compare (`scripts/e2e_subagents_compare.py`) runs against the whole stack — no code changes needed there.

- [ ] **Step 1: Write failing tests** — create `tests/test_websearch.py`:

```python
"""Pluggable web_search backend: provider selection, Bing/DDG parsing, tool shape."""

from __future__ import annotations

from harness.config import Settings
from harness.tools.base import ToolResult
from harness.tools.builtin import builtin_registry
from harness.tools.websearch import (
    BingProvider,
    DuckDuckGoProvider,
    TavilyProvider,
    WebSearchResult,
    select_websearch_provider,
)

_BING_FIXTURE = """\
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.python.org/">Welcome to Python.org</a></h2>
    <div class="b_caption"><p>The official home of the Python Programming Language.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://docs.python.org/3/">Python 3 documentation</a></h2>
    <div class="b_caption"><p>Python 3.11 documentation and reference.</p></div>
  </li>
</ol>
"""

_DDG_FIXTURE = """\
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com">DuckDuckGo</a></h2>
  <a class="result__snippet" href="https://duckduckgo.com">The search engine that doesn't track you.</a>
</div>
"""


class _FakeFetch:
    """Injects canned HTML so provider tests never touch the network."""

    def __init__(self, html: str) -> None:
        self._html = html

    async def fetch(self, url: str) -> str:
        return self._html


# ---- provider selection ---- #


def test_select_provider_default_bing() -> None:
    s = Settings.from_env({})
    assert isinstance(select_websearch_provider(s), BingProvider)


def test_select_provider_backend_switch() -> None:
    s = Settings.from_env({"HARNESS_WEB_SEARCH_BACKEND": "duckduckgo"})
    assert isinstance(select_websearch_provider(s), DuckDuckGoProvider)


def test_select_provider_tavily_when_key_set() -> None:
    s = Settings.from_env({"TAVILY_API_KEY": "tk", "HARNESS_WEB_SEARCH_BACKEND": "bing"})
    assert isinstance(select_websearch_provider(s), TavilyProvider)


def test_settings_web_search_fields() -> None:
    assert Settings.from_env({}).web_search_backend == "bing"
    assert Settings.from_env({}).tavily_api_key == ""
    s = Settings.from_env({"HARNESS_WEB_SEARCH_BACKEND": "duckduckgo", "TAVILY_API_KEY": "k"})
    assert s.web_search_backend == "duckduckgo"
    assert s.tavily_api_key == "k"


# ---- parsing (canned fixtures) ---- #


async def test_bing_provider_parses_canned_results() -> None:
    provider = BingProvider(fetch=_FakeFetch(_BING_FIXTURE).fetch)
    results = await provider.search("python")
    assert len(results) == 2
    assert results[0].title == "Welcome to Python.org"
    assert results[0].url == "https://www.python.org/"
    assert "programming language" in results[0].snippet
    assert results[1].url == "https://docs.python.org/3/"


async def test_duckduckgo_provider_parses_canned_results() -> None:
    provider = DuckDuckGoProvider(fetch=_FakeFetch(_DDG_FIXTURE).fetch)
    results = await provider.search("privacy")
    assert len(results) == 1
    assert results[0].title == "DuckDuckGo"
    assert results[0].url == "https://duckduckgo.com"
    assert "doesn't track you" in results[0].snippet


async def test_provider_degrades_to_empty_on_transport_error() -> None:
    class _Boom:
        async def fetch(self, url: str) -> str:
            raise OSError("network down")

    provider = BingProvider(fetch=_Boom().fetch)
    assert await provider.search("x") == []


# ---- tool shape ---- #


async def test_web_search_tool_returns_ok(monkeypatch) -> None:
    import harness.tools.builtin.websearch as ws_mod

    class _Stub:
        async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
            return [WebSearchResult("Python", "https://python.org/", "the language")]

    monkeypatch.setattr(ws_mod, "select_websearch_provider", lambda settings: _Stub())
    tool = builtin_registry().require("web_search")
    res = await tool.invoke(query="python", max_results=5)
    assert isinstance(res, ToolResult)
    assert not res.is_error
    assert "Python" in res.content
    assert "https://python.org/" in res.content


async def test_web_search_tool_no_results(monkeypatch) -> None:
    import harness.tools.builtin.websearch as ws_mod

    class _Empty:
        async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
            return []

    monkeypatch.setattr(ws_mod, "select_websearch_provider", lambda settings: _Empty())
    res = await builtin_registry().require("web_search").invoke(query="no such thing")
    assert "No results" in res.content
```

Create `tests/test_mcp_subagent_propagation.py`:

```python
"""MCP tools reach subagents only through explicit mcp_* allowlist entries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from harness.agents.orchestrator import add_subagents, resolve_mcp_tools
from harness.agents.subagent import Subagent
from harness.core.agent import Agent
from harness.core.messages import Message, ToolCall
from harness.core.runner import Runner
from harness.llm.base import (
    LLMResponse,
    StreamEnd,
    StreamEvent,
    StreamText,
    StreamToolCall,
    ToolSchema,
)
from harness.tools.base import Tool, ToolResult
from harness.tools.registry import ToolRegistry


class _FakeTool(Tool):
    def __init__(self, name: str) -> None:
        super().__init__(
            name=name,
            description=f"{name}: fake",
            parameters_schema={"type": "object", "properties": {}},
        )

    async def invoke(self, **kwargs: Any) -> ToolResult:
        return ToolResult.ok(f"{self.name} ran")


def _mcp(server: str, name: str) -> Tool:
    return _FakeTool(f"mcp_{server}_{name}")


def _subagent(name: str, mcp_allowlist: tuple[str, ...] = ()) -> Subagent:
    return Subagent(name=name, instructions=f"{name} instructions", mcp_allowlist=mcp_allowlist)


# ---- allowlist semantics (pure) ---- #


def test_resolve_mcp_tools_allowlist_semantics() -> None:
    parent = ToolRegistry()
    parent.register_all(
        [
            _mcp("demo", "add"),
            _mcp("demo", "list"),
            _mcp("other", "run"),
            _FakeTool("read_file"),  # a non-mcp builtin must never leak
        ]
    )
    # mcp_* matches every mcp tool
    all_sa = _subagent("a", mcp_allowlist=("mcp_*",))
    assert {t.name for t in resolve_mcp_tools(all_sa, parent)} == {
        "mcp_demo_add",
        "mcp_demo_list",
        "mcp_other_run",
    }
    # exact name matches only itself
    one_sa = _subagent("b", mcp_allowlist=("mcp_demo_add",))
    assert [t.name for t in resolve_mcp_tools(one_sa, parent)] == ["mcp_demo_add"]
    # server wildcard matches the whole server
    server_sa = _subagent("c", mcp_allowlist=("mcp_demo_*",))
    assert {t.name for t in resolve_mcp_tools(server_sa, parent)} == {"mcp_demo_add", "mcp_demo_list"}
    # default-deny: no allowlist => no MCP tools; a missing parent registry => none
    assert resolve_mcp_tools(_subagent("d"), parent) == []
    assert resolve_mcp_tools(all_sa, None) == []


# ---- integration: a delegated run carries allowlisted MCP tools ---- #


class _ToolsRecordingProvider:
    """Scripted provider that records the tool names of every stream call."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = script
        self.seen: list[list[str]] = []

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.seen.append(sorted(t["function"]["name"] for t in (tools or [])))
        response = self.script.pop(0)
        if response.tool_calls:
            for tc in response.tool_calls:
                yield StreamToolCall(tool_call=tc)
        if response.final_text:
            yield StreamText(text=response.final_text)
        yield StreamEnd(response=response)


def _allow_script() -> list[LLMResponse]:
    return [
        LLMResponse(tool_calls=[_call("p1", "delegate_to_allow", '{"task": "x"}')]),
        LLMResponse(tool_calls=[_call("a1", "mcp_demo_add", '{"a": 1}')]),
        LLMResponse(final_text="allow delivered"),
        LLMResponse(final_text="parent done"),
    ]


def _deny_script() -> list[LLMResponse]:
    return [
        LLMResponse(tool_calls=[_call("p1", "delegate_to_deny", '{"task": "y"}')]),
        LLMResponse(final_text="deny delivered"),
        LLMResponse(final_text="parent done"),
    ]


def _call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


async def test_allowlisted_subagent_sees_mcp_tools() -> None:
    provider = _ToolsRecordingProvider(_allow_script())
    parent = Agent(name="parent", instructions="parent", model="m")
    parent.tools.register(_mcp("demo", "add"))
    runner = Runner(provider)
    add_subagents(parent, runner, [_subagent("allow", mcp_allowlist=("mcp_*",))])

    result = await runner.run(parent, "go", session_id=None)
    assert result.final_output == "parent done"
    # the subagent's own stream call saw the allowlisted MCP tool and called it
    assert "mcp_demo_add" in provider.seen[1]


async def test_unlisted_subagent_never_sees_mcp_tools() -> None:
    provider = _ToolsRecordingProvider(_deny_script())
    parent = Agent(name="parent", instructions="parent", model="m")
    parent.tools.register(_mcp("demo", "add"))
    runner = Runner(provider)
    add_subagents(parent, runner, [_subagent("deny")])

    result = await runner.run(parent, "go", session_id=None)
    assert result.final_output == "parent done"
    # the deny subagent's stream call saw no MCP tools (default deny)
    assert "mcp_demo_add" not in provider.seen[1]
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_websearch.py tests/test_mcp_subagent_propagation.py -v`
Expected: FAIL — `harness.tools.websearch` does not exist, `builtin_registry()` lacks `web_search`, `resolve_mcp_tools` is not defined, and `Settings` has no `web_search_backend`/`tavily_api_key`.

- [ ] **Step 3: Implement**

**3a. `src/harness/config.py`** — add two fields to `Settings` (after the subagent block, before `# Logging / tracing`):

```python
    # Web search backend for the web_search tool
    web_search_backend: str = "bing"  # bing (free, cn default) | duckduckgo | tavily-on-key
    tavily_api_key: str = ""  # optional; its presence switches web_search to Tavily
```

In `from_env` (after the `subagent_budget` line):

```python
            web_search_backend=get("HARNESS_WEB_SEARCH_BACKEND", default="bing"),
            tavily_api_key=get("TAVILY_API_KEY"),
```

In `load` (the prefixes snapshot — `TAVILY_` must be included or the key never reaches `from_env`):

```python
        prefixes = ("DEEPSEEK_", "HARNESS_", "SANDBOX_", "TAVILY_")
```

**3b. Create `src/harness/tools/websearch/providers.py`**:

```python
"""Pluggable web-search backends: free Bing/DDG scrapers + optional Tavily.

The free backends use only the stdlib (``urllib`` + ``html.parser``) and
degrade to empty result lists on any error — a search backend must never
crash the agent loop.
"""

from __future__ import annotations

import asyncio
import html.parser
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

_UA = {"User-Agent": "Mozilla/5.0 (compatible; HarnessBot/1.0)"}


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str


class WebSearchProvider(Protocol):
    """A backend that turns a query into ranked web results."""

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]: ...


async def _http_get(url: str, *, headers: dict[str, str] | None = None) -> str:
    def _blocking() -> str:
        req = urllib.request.Request(url, headers=headers or _UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")

    return await asyncio.to_thread(_blocking)


async def _http_post(url: str, body: str, *, headers: dict[str, str] | None = None) -> str:
    def _blocking() -> str:
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers or {}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")

    return await asyncio.to_thread(_blocking)


class _BingParser(html.parser.HTMLParser):
    """Best-effort extraction of cn.bing.com/search organic results.

    Each result is an ``<li class="b_algo">``: the title is an ``<a>`` inside
    ``<h2>``, the snippet is the first ``<p>`` inside ``.b_caption``. Markup
    drift degrades to fewer/empty results — parsing never raises.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebSearchResult] = []
        self._algo = 0
        self._cur: WebSearchResult | None = None
        self._title_done = False
        self._in_p = False
        self._purpose = "body"  # "title" | "snippet" | "body"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "li" and "b_algo" in cls.split():
            self._algo += 1
            self._cur = WebSearchResult(title="", url="", snippet="")
            self._title_done = False
            self._purpose = "body"
        if self._cur is None:
            return
        if tag == "h2":
            self._purpose = "title"
        elif tag == "a" and self._purpose == "title" and not self._cur.url:
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self._cur.url = href
        elif tag == "p" and self._title_done and not self._in_p:
            self._purpose = "snippet"
            self._in_p = True

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None:
            return
        if tag == "h2":
            self._title_done = True
            self._purpose = "body"
        elif tag == "p" and self._in_p:
            self._in_p = False
            self._purpose = "body"
        elif tag == "li" and self._algo:
            self._algo -= 1
            if self._algo == 0 and self._cur:
                self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is None:
            return
        if self._purpose == "title":
            self._cur.title = (self._cur.title + " " + data.strip()).strip()
        elif self._purpose == "snippet":
            self._cur.snippet = (self._cur.snippet + " " + data.strip()).strip()


def _extract_bing_results(html_text: str) -> list[WebSearchResult]:
    parser = _BingParser()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — malformed HTML degrades to empty
        return []
    return parser.results


class _DuckDuckGoParser(html.parser.HTMLParser):
    """Best-effort extraction of html.duckduckgo.com/html results.

    A result is ``<a class="result__a" href="URL">Title</a>`` followed by
    ``<a class="result__snippet" ...>Snippet</a>``. Parsing never raises.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebSearchResult] = []
        self._title: str | None = None
        self._url = ""
        self._in_title_a = False
        self._in_snippet_a = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        cls = dict(attrs).get("class") or ""
        if "result__a" in cls.split():
            self._in_title_a = True
            self._url = dict(attrs).get("href") or ""
            self._buf = []
        elif "result__snippet" in cls.split():
            self._in_snippet_a = True
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title_a or self._in_snippet_a:
            self._buf.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._in_title_a:
            self._in_title_a = False
            self._title = " ".join(self._buf).strip()
        elif self._in_snippet_a:
            self._in_snippet_a = False
            if self._title and self._url:
                self.results.append(
                    WebSearchResult(
                        title=self._title,
                        url=self._url,
                        snippet=" ".join(self._buf).strip(),
                    )
                )
            self._title = None


def _extract_duckduckgo_results(html_text: str) -> list[WebSearchResult]:
    parser = _DuckDuckGoParser()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — malformed HTML degrades to empty
        return []
    return parser.results


class BingProvider:
    """Free default backend: scrape cn.bing.com/search (China-reachable)."""

    def __init__(
        self,
        *,
        fetch: Callable[[str], Awaitable[str]] | None = None,
        base_url: str = "https://cn.bing.com/search",
    ) -> None:
        self._fetch = fetch or _http_get
        self._base_url = base_url

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        url = f"{self._base_url}?q={urllib.parse.quote(query)}&setlang=zh-hans"
        try:
            html_text = await self._fetch(url)
        except Exception:  # noqa: BLE001 — network failure degrades to no results
            return []
        return _extract_bing_results(html_text)[:max_results]


class DuckDuckGoProvider:
    """Alternative free backend: scrape html.duckduckgo.com/html."""

    def __init__(
        self,
        *,
        fetch: Callable[[str], Awaitable[str]] | None = None,
        base_url: str = "https://html.duckduckgo.com/html/",
    ) -> None:
        self._fetch = fetch or _http_get
        self._base_url = base_url

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        url = f"{self._base_url}?q={urllib.parse.quote(query)}"
        try:
            html_text = await self._fetch(url)
        except Exception:  # noqa: BLE001 — network failure degrades to no results
            return []
        return _extract_duckduckgo_results(html_text)[:max_results]


class TavilyProvider:
    """Keyed backend; only constructed when a TAVILY_API_KEY is configured."""

    def __init__(self, api_key: str, endpoint: str = "https://api.tavily.com/search") -> None:
        self._api_key = api_key
        self._endpoint = endpoint

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        body = json.dumps({"api_key": self._api_key, "query": query, "max_results": max_results})
        try:
            raw = await _http_post(
                self._endpoint,
                body,
                headers={"Content-Type": "application/json"},
            )
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — any failure degrades to no results
            return []
        return [
            WebSearchResult(
                title=str(r.get("title", "")),
                url=str(r.get("url", "")),
                snippet=str(r.get("content", "")),
            )
            for r in data.get("results", [])
        ][:max_results]
```

**3c. Create `src/harness/tools/websearch/__init__.py`**:

```python
"""Web search provider seam: select a backend from settings."""

from __future__ import annotations

from harness.config import Settings
from harness.tools.websearch.providers import (
    BingProvider,
    DuckDuckGoProvider,
    TavilyProvider,
    WebSearchProvider,
    WebSearchResult,
)

__all__ = [
    "WebSearchProvider",
    "WebSearchResult",
    "BingProvider",
    "DuckDuckGoProvider",
    "TavilyProvider",
    "select_websearch_provider",
]


def select_websearch_provider(settings: Settings) -> WebSearchProvider:
    """Pick the backend: Tavily when a key is configured, else the free scraper.

    ``HARNESS_WEB_SEARCH_BACKEND`` chooses between the two free scrapers
    (``bing`` default, ``duckduckgo`` alternative).
    """
    if settings.tavily_api_key:
        return TavilyProvider(settings.tavily_api_key)
    if settings.web_search_backend == "duckduckgo":
        return DuckDuckGoProvider()
    return BingProvider()
```

**3d. Create `src/harness/tools/builtin/websearch.py`**:

```python
"""web_search builtin tool: pluggable backend, defaults to free cn.bing."""

from __future__ import annotations

from harness.config import Settings
from harness.tools.base import tool
from harness.tools.websearch import select_websearch_provider


@tool(
    name="web_search",
    description=(
        "Search the web for up-to-date external information and return ranked "
        "results (title, url, snippet)."
    ),
)
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for up-to-date external information.

    The backend is pluggable: TAVILY_API_KEY uses Tavily, otherwise the free
    Bing or DuckDuckGo scraper (HARNESS_WEB_SEARCH_BACKEND). A failure degrades
    to "No results found" so the agent can adapt.
    """
    provider = select_websearch_provider(Settings.load())
    try:
        results = await provider.search(query, max_results=max_results)
    except Exception:  # noqa: BLE001 — a search backend must never crash the run
        return "No results found (search backend unavailable)."
    if not results:
        return "No results found."
    return "\n".join(
        f"{i}. {r.title}\n   {r.url}\n   {r.snippet}"
        for i, r in enumerate(results[:max_results], start=1)
    )
```

**3e. `src/harness/tools/builtin/__init__.py`** — register the tool:

```python
from harness.tools.builtin.files import glob_files, grep_files, read_file, write_file
from harness.tools.builtin.shell import bash
from harness.tools.builtin.websearch import web_search
from harness.tools.registry import ToolRegistry

__all__ = [
    "read_file",
    "write_file",
    "glob_files",
    "grep_files",
    "bash",
    "web_search",
    "builtin_registry",
]


def builtin_registry() -> ToolRegistry:
    """A registry pre-populated with all built-in tools."""
    registry = ToolRegistry()
    registry.register_all(
        [read_file, write_file, glob_files, grep_files, bash, web_search]
    )
    return registry
```

**3f. `src/harness/agents/orchestrator.py`** — lazy MCP resolution.

Add the import (near the other imports at the top):

```python
from harness.tools.registry import ToolRegistry
```

Add two module-level helpers (after `DELEGATION_HINT`):

```python
def _matches_mcp_allowlist(name: str, allowlist: tuple[str, ...]) -> bool:
    """Whether a tool name matches any mcp_* entry in the allowlist.

    ``mcp_*`` matches every MCP tool; a trailing ``*`` is a prefix wildcard
    (``mcp_demo_*`` matches the whole server); anything else matches exactly.
    """
    for entry in allowlist:
        if entry == "mcp_*":
            return True
        if entry.endswith("*") and name.startswith(entry[:-1]):
            return True
        if name == entry:
            return True
    return False


def resolve_mcp_tools(
    subagent: Subagent, parent_tools: ToolRegistry | None
) -> list[Tool]:
    """MCP tools from the parent's current registry the subagent allowlisted.

    Resolution is lazy (per delegation) so MCP servers added after startup flow
    into subagents that explicitly allow them. An empty allowlist means
    default-deny: no MCP tools ever reach the subagent.
    """
    if parent_tools is None or not subagent.mcp_allowlist:
        return []
    return [
        t
        for t in parent_tools.all()
        if t.name.startswith("mcp_") and _matches_mcp_allowlist(t.name, subagent.mcp_allowlist)
    ]
```

`SubagentTool.__init__` — add the parameter (before `nested_hint`):

```python
        budget: SubagentBudget | None = None,
        nested_delegates: tuple[Tool, ...] = (),
        parent_tools: ToolRegistry | None = None,
        nested_hint: str = "",
        advanced: bool = False,
```

and store it (next to the other private fields):

```python
        self._budget = budget
        self._nested_delegates = nested_delegates
        self._parent_tools = parent_tools
        self._nested_hint = nested_hint
```

`SubagentTool.invoke` — resolve MCP tools at call time (replace the `as_agent` block):

```python
        mcp_tools = resolve_mcp_tools(self.subagent, self._parent_tools)
        agent = self.subagent.as_agent(
            model=self._model,
            extra_tools=tuple(self._nested_delegates) + tuple(mcp_tools),
            extra_instructions=self._nested_hint,
        )
```

`subagent_as_tool` — add the parameter and pass it through:

```python
    budget: SubagentBudget | None = None,
    nested_delegates: tuple[Tool, ...] = (),
    parent_tools: ToolRegistry | None = None,
    nested_hint: str = "",
    advanced: bool = False,
) -> Tool:
```

and in the `SubagentTool(...)` construction:

```python
        budget=budget,
        nested_delegates=nested_delegates,
        parent_tools=parent_tools,
        nested_hint=nested_hint,
        advanced=advanced,
    )
```

`add_subagents` — pass the parent's own registry so resolution reads it at call time. In the non-advanced branch:

```python
    if not advanced:
        for sa in subagents:
            agent.tools.register(
                subagent_as_tool(
                    sa, runner, base, on_event=on_event, parent_tools=agent.tools
                )
            )
        return
    level2 = {
        sa.name: subagent_as_tool(
            sa, runner, base,
            on_event=on_event, concurrent=True, budget=budget, advanced=True,
            parent_tools=agent.tools,
        )
        for sa in subagents
    }
    for sa in subagents:
        nested = tuple(t for name, t in level2.items() if name != sa.name)
        agent.tools.register(
            subagent_as_tool(
                sa, runner, base,
                on_event=on_event,
                concurrent=True,
                budget=budget,
                nested_delegates=nested,
                parent_tools=agent.tools,
                nested_hint=DELEGATION_HINT,
                advanced=True,
            )
        )
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/test_websearch.py tests/test_mcp_subagent_propagation.py -v` → PASS.
Then the full gate:

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
```

Existing tests must stay green — notably `tests/test_agents.py` (which constructs `SubagentTool`/`subagent_as_tool` without `parent_tools`, exercising the default-None path) and `tests/test_web_runtime.py` / `tests/test_web_server.py` (no exact builtin-count assertions).

- [ ] **Step 5: Commit**

```bash
git add src/harness/config.py src/harness/tools/websearch/ src/harness/tools/builtin/ src/harness/agents/orchestrator.py tests/test_websearch.py tests/test_mcp_subagent_propagation.py
git commit -m "feat: MCP tools reach allowlisted subagents + pluggable web_search"
```

---

## Validation

1. Quality gate green after every task.
2. New unit tests cover: prompt fragments, `tools:` parsing/filtering + default, provider selection, Bing/DDG parse, `web_search` result shape, MCP propagation + allowlist exclusion.
3. Re-run `uv run scripts/e2e_subagents_compare.py` (n=3, multi-dim rubric): expect no regression on A/B/C, and — ideally — Task 1's handoff guidance makes the depth/types dimensions finally discriminate (depth-2 fires). Report the delta either way.
4. Optional live smoke: a direct `web_search` call from a Python one-liner against cn.bing to confirm the default backend works from this network:

```python
import asyncio
from harness.tools.builtin.websearch import web_search
print(asyncio.run(web_search("python", max_results=3)))
```

## Risks and Notes

- **R1 (China network):** the free default is `cn.bing` because DuckDuckGo is unreliable/blocked in mainland China; DDG remains a config-switchable alternative. If neither is reachable from a deployment, the provider seam lets a keyed backend take over.
- **R2 (scraper fragility):** Bing/DDG markup can change and break parsing. Both providers degrade to an empty result list (never raise), and the tool reports "no results" so the agent can adapt.
- **R3 (token cost):** subagent instructions are per-delegation context. Task 1 additions are one or two lines each, deliberately minimal.
- **R4 (security):** MCP propagation is allowlist-only and default-deny; a subagent never sees `mcp_*` tools it did not explicitly opt into. The `search` subagent loses `write_file` — existing behaviors that rely on a subagent writing files must use a subagent whose allowlist includes it (coder/doc_writer/file_handler still do).
- **R5 (off-path):** none of the three tasks touch the runner's core loop; with `HARNESS_SUBAGENTS=0` no delegation tools are registered and nothing changes.
- **R6 (transient Task-1 state):** `researcher`/`coder` instructions mention `web_search` before Task 3 registers it; if a real run happens between the Task 1 and Task 3 commits, the model may attempt a `web_search` call that fails as an unknown tool and recovers. The quality gate and e2e compare run after Task 3, so this transient state is never evaluated.
