# Subagent Enhancements Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make multi-agent orchestration measurably more effective by (1) optimizing each subagent's prompt and delegation description, (2) giving subagents per-role tool allowlists instead of the current uniform five-tool set, and (3) letting MCP tools reach subagents under an explicit allowlist plus shipping a pluggable `web_search` builtin (free `cn.bing` default).

**Architecture:** A single plan with three ordered tasks, validated end-to-end with the existing multi-dimensional compare rubric (`scripts/e2e_subagents_compare.py`). The load-bearing mechanism change: a subagent's tool set is resolved **at delegation time** (not creation time), so MCP servers connected after startup flow into subagents that explicitly allow them.

**Tech Stack:** Python 3.11, existing harness core (`Runner`, `ToolRegistry`, `Subagent`, `SubagentTool`), YAML subagent configs, stdlib `urllib` for the free web-search backend, existing MCP client infra.

## Global Constraints

- The OFF-path must stay byte-for-byte unchanged: when `HARNESS_SUBAGENTS=0`, none of these changes affect a run.
- Backwards compatible YAML: an existing subagent config without a `tools:` field keeps today's full builtin tool set.
- Subagent instructions stay lean — every added line costs tokens on every delegation. No prose bloat.
- Security boundary: subagents get **no MCP tools by default**; MCP tools reach a subagent only through an explicit `mcp_*` entry in its `tools:` allowlist.
- Quality gate on every task: `uv run ruff check . && uv run mypy src && uv run pytest -q` plus `node --check src/harness/web/static/js/app.js` (unchanged files exempt).
- No new runtime dependencies for the free web-search backend (stdlib only). `TavilyProvider` is optional and only activated by a `TAVILY_API_KEY` in `.env` — if Tavily is added it stays an optional extra, not a hard dependency.

---

### Task 1: Targeted prompt and description optimization

**Files:**
- Modify: `src/harness/skills/bundled/subagents/researcher.yaml`
- Modify: `src/harness/skills/bundled/subagents/search.yaml`
- Modify: `src/harness/skills/bundled/subagents/coder.yaml`
- Modify: `src/harness/skills/bundled/subagents/doc_writer.yaml`
- Modify: `src/harness/skills/bundled/subagents/frontend_design.yaml`
- Modify: `src/harness/skills/bundled/subagents/file_handler.yaml`
- Modify: `src/harness/agents/orchestrator.py` (`DELEGATION_PROTOCOL_ADVANCED`)
- Test: `tests/test_subagent_prompts.py` (new)

**Goal:** Sharpen each subagent's `description` (the parent's delegation trigger) and `instructions` (its own system prompt), and nudge the advanced delegation protocol to actually use RECOMMENDED NEXT STEP handoffs — the benchmark showed depth-2 never fires because nothing induces the chain.

**Interfaces:**
- Consumes: existing `SubagentSpec` YAML shape (`name`, `description`, `instructions`, `skill`, `model`, `max_turns`).
- Produces: improved bundled YAML content; `DELEGATION_PROTOCOL_ADVANCED` text; a test asserting the bundled instructions contain the new guidance.

**Concrete edits:**

- `researcher.yaml`:
  - `description`: make the delegation trigger crisper ("facts gathered from the workspace **or the web** via `web_search` when available").
  - `instructions`: add source attribution ("cite what you found: `file:line` for workspace facts, `url` for web facts"), and add a handoff seed: "if the parent will need these findings composed into a document, put `doc_writer` in RECOMMENDED NEXT STEP." Keep the under-200-words cap.
- `search.yaml`:
  - `instructions`: add one line on glob-vs-grep decision ("`glob_files` to locate by path pattern, `grep_files` to locate by content"). Stay workspace-only; no web search (that is researcher's job).
- `coder.yaml`:
  - `instructions`: append one line: "When you need an external API's signature or usage, use `web_search` if available and cite the URL." Everything else stays (it is the quality benchmark).
- `doc_writer.yaml`, `frontend_design.yaml`:
  - `description` only: tighten trigger wording so the parent routes document work and UI work to them instead of doing it itself. `instructions` stay thin (the real body lives in the `skill:` files).
- `file_handler.yaml`:
  - `description`: sharpen the boundary ("data/config/document files — NOT code; code goes to coder").
- `DELEGATION_PROTOCOL_ADVANCED` (orchestrator.py):
  - Add: "When a subagent's RECOMMENDED NEXT STEP names a subagent that is the right next handler for the remaining work, follow it — you are the router, and chaining best-fit subagents is the point of advanced mode. Never chain to avoid work you should do yourself."

- [ ] **Step 1: Write the failing test** — `tests/test_subagent_prompts.py` asserts, for each bundled YAML, the new required fragments exist (e.g. researcher mentions `web_search` and `doc_writer` handoff; search mentions glob-vs-grep; advanced protocol mentions chaining).
- [ ] **Step 2: Run it to verify it fails** — `pytest tests/test_subagent_prompts.py -v`.
- [ ] **Step 3: Apply the edits above.**
- [ ] **Step 4: Run the test + quality gate.**
- [ ] **Step 5: Commit.**

---

### Task 2: Per-subagent tool allowlists

**Files:**
- Modify: `src/harness/agents/subagent.py` (`Subagent.tools`)
- Modify: `src/harness/agents/registry.py` (`SubagentSpec`, `_parse`, `to_subagent`)
- Modify: the six bundled YAMLs (add `tools:`)
- Test: `tests/test_subagent_registry.py` (new)

**Goal:** `SubagentSpec` gains a `tools: [name, ...]` list; `to_subagent` builds a filtered registry (builtin names only at this stage; `mcp_*` entries are carried as patterns but resolved lazily in Task 3). Absent field ⇒ full builtin set (backwards compatible).

**Interfaces:**
- Consumes: `builtin_registry()` tool names (`read_file`, `write_file`, `glob_files`, `grep_files`, `bash`); `ToolRegistry` filter/remove API.
- Produces: `SubagentSpec.tools: tuple[str, ...]` (default = all builtins); `Subagent.tools` is the filtered `ToolRegistry`.

**Concrete edits:**

- `SubagentSpec`: add `tools: tuple[str, ...] = ()`; empty ⇒ all builtins (documented in `to_subagent`).
- `_parse`: read `tools:` as a YAML list of names; tolerate missing/bad values (fall back to default).
- `to_subagent`: build `ToolRegistry`, register the builtin tools whose names are in the allowlist (or all, if the allowlist is empty). Keep `mcp_*` pattern entries (they are not builtins — they are consumed in Task 3's resolver).
- Bundled YAML `tools:` defaults:
  - `search.yaml`: `[glob_files, grep_files, bash]` (no read/write — pure locate)
  - `researcher.yaml`: `[read_file, glob_files, grep_files, bash, web_search]`
  - `coder.yaml`: all five builtins + `web_search`
  - `doc_writer.yaml`: `[read_file, write_file, bash]`
  - `file_handler.yaml`: `[read_file, write_file, bash]`
  - `frontend_design.yaml`: `[read_file, write_file, bash]`
  - (`web_search` is registered in Task 3; until then the name simply resolves to nothing on registration and is skipped.)
- A unit test asserts each bundled subagent's tools resolve to exactly its allowlist, and that a config without `tools:` still gets all five.

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2: Verify they fail.**
- [ ] **Step 3: Implement `tools:` on spec/parse/to_subagent + YAMLs.**
- [ ] **Step 4: Tests + quality gate.**
- [ ] **Step 5: Commit.**

---

### Task 3: MCP tools reach subagents (allowlist) + pluggable `web_search`

**Files:**
- Modify: `src/harness/agents/orchestrator.py` (`subagent_as_tool`, `SubagentTool` construction, lazy tool resolution)
- Modify: `src/harness/agents/subagent.py` (how the subagent's tool list is supplied per run, if needed)
- Create: `src/harness/tools/websearch/__init__.py` (provider protocol + factory)
- Create: `src/harness/tools/websearch/providers.py` (`BingProvider` default, `DuckDuckGoProvider`, `TavilyProvider`)
- Modify: `src/harness/tools/builtin/__init__.py` (register `web_search`)
- Create: `src/harness/tools/builtin/websearch.py` (tool wrapper)
- Modify: `src/harness/config.py` (optional `web_search_provider` / env reads)
- Test: `tests/test_websearch.py` (new), `tests/test_mcp_subagent_propagation.py` (new)

**Goal:** (a) A subagent's tools resolve at delegation time to its allowlisted builtins **plus** the MCP tools currently registered on the parent that match its `mcp_*` allowlist patterns. (b) A `web_search` builtin with a pluggable backend, defaulting to a free `cn.bing` scraper, automatically switching to Tavily when `TAVILY_API_KEY` is set.

**Interfaces:**
- Consumes: `SubagentTool` constructor; parent registry (to snapshot `mcp_*` tools at call time); `MCPServerConfig`; `builtin_registry`.
- Produces: `WebSearchProvider` protocol (`async search(query) -> list[WebSearchResult{title, url, snippet}]`); `web_search(query, max_results=5) -> ToolResult`; `select_websearch_provider(settings) -> WebSearchProvider`.

**Concrete edits:**

- **Lazy resolution:** `SubagentTool.__call__` builds the subagent's tool list per delegation:
  1. allowlisted builtins (from Task 2),
  2. current tools on the parent registry whose name starts with `mcp_` **and** matches an `mcp_*` entry in the subagent's allowlist (pattern: `mcp_*` matches any `mcp_<server>_<tool>`; a specific `mcp_<server>_<tool>` matches exactly).
  Pass the resulting list to the subagent's `run`/`resume` call for that delegation. No MCP tools ⇒ behavior identical to today.
- **`web_search` builtin:** tool registered in `builtin_registry`. Signature `web_search(query: str, max_results: int = 5)`. Provider chosen by `select_websearch_provider`: `TAVILY_API_KEY` set ⇒ Tavily; else the configured free default (Bing). Results rendered as numbered `title / url / snippet` lines.
- **`BingProvider` (default):** query `https://cn.bing.com/search?q=<urlencoded>&setlang=zh-hans`, parse `li.b_algo` blocks for title/link/snippet with stdlib HTML parsing. Best-effort: empty list on parse failure, never raises.
- **`DuckDuckGoProvider`:** same shape via `html.duckduckgo.com` — included as an alternative free backend, selected by env `HARNESS_WEB_SEARCH_BACKEND=duckduckgo` (default `bing`).
- **`TavilyProvider`:** optional; POST to `https://api.tavily.com/search` with `TAVILY_API_KEY`; returns parsed results. Only constructed when the key is present.
- **Config:** `config.py` reads `HARNESS_WEB_SEARCH_BACKEND` (default `bing`) and passes `TAVILY_API_KEY` through from env.

- [ ] **Step 1: Write failing tests** — provider selection (key vs no key, backend switch), Bing result parsing on a canned HTML fixture, `web_search` tool returns `ToolResult`, MCP propagation (register a fake `mcp_*` tool on parent, allowlist it on a subagent, assert it appears in that subagent's resolved tools and not in another's).
- [ ] **Step 2: Verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests + quality gate.**
- [ ] **Step 5: Commit.**

---

## Validation

1. Quality gate green after every task.
2. New unit tests cover: prompt fragments, `tools:` parsing/filtering + default, provider selection, Bing parse, `web_search` result shape, MCP propagation + allowlist exclusion.
3. Re-run `scripts/e2e_subagents_compare.py` (n=3, multi-dim rubric): expect no regression on A/B/C, and — ideally — Task 1's handoff guidance makes the depth/types dimensions finally discriminate (depth-2 fires). Report the delta either way.
4. Optional live smoke: a direct `web_search` call from a Python one-liner against cn.bing to confirm the default backend works from this network.

## Risks and Notes

- **R1 (China network):** the free default is `cn.bing` because DuckDuckGo is unreliable/blocked in mainland China; DDG remains a config-switchable alternative. If neither is reachable from a deployment, the provider seam lets a keyed backend take over.
- **R2 (scraper fragility):** Bing markup can change and break parsing. The provider degrades to an empty result list (never raises), and the tool reports "no results" so the agent can adapt.
- **R3 (token cost):** subagent instructions are per-delegation context. Task 1 additions are one or two lines each, deliberately minimal.
- **R4 (security):** MCP propagation is allowlist-only and default-deny; a subagent never sees `mcp_*` tools it did not explicitly opt into. The `search` subagent loses `write_file` — existing behaviors that rely on a subagent writing files must use a subagent whose allowlist includes it (coder/doc_writer/file_handler still do).
- **R5 (off-path):** none of the three tasks touch the runner's core loop; with `HARNESS_SUBAGENTS=0` no delegation tools are registered and nothing changes.
