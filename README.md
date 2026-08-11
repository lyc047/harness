# harness

A production-grade Python **AI agent harness**: an LLM tool-loop runtime with
MCP client support, multi-agent orchestration, planning, human-in-the-loop
approval, self-evolving skills, user preferences, sandboxed tool execution and
JSONL tracing. Built from scratch on `asyncio`, OpenAI-compatible providers
(DeepSeek), and a CLI-first workflow.

```
user input → LLM → tool_calls → approval → sandbox → results → loop → final answer
```

## Features

| Feature | Where |
|---|---|
| Streaming turn loop (thinking-mode aware, parallel tool calls, max_turns) | `src/harness/core/runner.py` |
| OpenAI-compatible provider (DeepSeek) with retry/backoff | `src/harness/llm/` |
| Builtin tools (read/write/glob/grep/bash) + `@tool` decorator | `src/harness/tools/` |
| MCP client (stdio + Streamable HTTP) with `/mcp` commands | `src/harness/tools/mcp/` |
| Multi-agent orchestration (manager pattern) | `src/harness/agents/` |
| Planning with plan revision (`/plan`) | `src/harness/planning/` |
| Human-in-the-loop approval + pause/resume checkpoints | `src/harness/safety/` |
| Self-evolving skills + user preferences (SQLite) | `src/harness/skills/`, `src/harness/memory/` |
| Sandboxed `bash` (local dev / remote SSH) | `src/harness/sandbox/` |
| Codex-style web UI (streaming chat, tool cards, approvals, pause/resume) | `src/harness/web/` |
| JSONL tracing of turns & tool calls | `src/harness/observability/tracing.py` |

## Requirements

- Python ≥ 3.11, [uv](https://docs.astral.sh/uv/)
- A DeepSeek API key (or any OpenAI-compatible endpoint)

## Install

```bash
uv sync
```

Copy `.env.example` to `.env` and set your key:

```bash
DEEPSEEK_API_KEY=sk-...
```

## Usage

```bash
uv run harness chat                 # interactive REPL
uv run harness chat --session <id>  # resume a session
uv run harness chat --subagents     # enable subagents (researcher/coder/frontend_design/doc_writer/search/file_handler)
uv run harness serve                # Codex-style web UI → http://127.0.0.1:8000
uv run harness serve --port 9000 --reload   # dev (auto-reloads on edits)
uv run harness serve --subagents            # enable subagents in the web UI (researcher/coder/frontend_design/doc_writer/search/file_handler)
uv run harness --help
```

### Web UI

`uv run harness serve` starts FastAPI/uvicorn (REST + WebSocket + static files)
with **no build step**. It shares the exact same core stack as the CLI, so the
two surfaces never drift. Features: session sidebar (create/switch/resume, with
auto-named titles — double-click a title to rename), a status-bar **permission
mode switcher** (计划 / 手动确认 / 自动 / 完全放开, per connection, defaulting
to manual confirmation), streaming markdown messages with an auto-opened
thinking panel (the model's reasoning is visible while it works), tool-call
cards showing the command + stdout/stderr, an approval dialog
(`y`/`n`/`a`/`p`/edit args), pause/resume checkpoints, `/plan` streaming with
step tracking, and per-message **回退** (roll back the conversation and undo
every `write_file` made after that point) and **分叉** (fork the history into a
new child session). The frontend is vanilla HTML/CSS/JS and works offline — no
CDN. Runs are per-tab; all sessions share one SQLite store.

**MCP** in the browser: `/mcp add stdio <name> <command> args...` /
`/mcp add http <name> <url>` connect external MCP servers (per tab), their
tools register as `mcp_<server>_<tool>` and run through the normal approval
pipeline; `/mcp list` and `/mcp remove <name>` manage them.

Permission modes: **计划** runs only tools the policy allows unconditionally
(read-only planning; mutations are blocked), **手动确认** is the default
ASK policy, **自动** auto-approves everything except explicit `deny` rules, and
**完全放开** allows every call, overriding even `deny` rules (the sandbox
boundary itself is unchanged).

REST + WS API (all `json`):

| Method / WS type | Purpose |
|---|---|
| `GET /api/sessions` · `POST /api/sessions` | list / create sessions |
| `PATCH /api/sessions/{id}` `{"name"}` | rename a session |
| `GET /api/sessions/{id}/messages` · `DELETE` | history / delete |
| `{type:"message"}` · `{type:"plan"}` | start a run / plan |
| `{type:"approval","decision":"y\|n\|a\|p\|e:…"}` | approve a tool |
| `{type:"pause"\|"resume"\|"cancel"}` | run control |
| `{type:"rollback","step":N}` | roll back conversation + code to step N |
| `{type:"branch","step":N}` | fork a new session from step N |
| `{type:"set_mode","mode":"plan\|ask\|auto\|full"}` | switch the connection's permission mode |
| `{type:"command","name":…}` | slash commands |

### REPL commands

| Command | Purpose |
|---|---|
| `/help` | list commands |
| `/new` | start a fresh session |
| `/session` | list / switch sessions |
| `/tools` | show registered tools |
| `/plan` | plan the current multi-step task |
| `/skills` · `/skill load <name>` | list / load skills |
| `/permissions` | print the current permission policy |
| `/checkpoints` | list pause/resume checkpoints |
| `/resume <id>` | resume a paused run |
| `/mcp add|list|remove` | manage MCP servers |
| `/exit` | quit |

### Subagents

`--subagents` turns the agent into a **manager**: it delegates matched work to
specialized subagents (researcher / coder / frontend_design / doc_writer /
search / file_handler) via `delegate_to_<name>` tools instead of doing it
itself. Each subagent runs **in isolation** — its own instructions, tools,
history and (optionally) a cheaper model — and returns a structured delivery
(`WHAT YOU DID` / `KEY FINDINGS` / `RECOMMENDED NEXT STEP` / `GAPS`, with large
deliverables saved to a file), which the parent verifies against the brief.

- **Declarative registry**: subagents are YAML configs, not Python. The six
  defaults ship in `src/harness/skills/bundled/subagents/*.yaml`; drop a
  same-named file in `skills/subagents/*.yaml` to override, or a brand-new one
  to add a subagent — zero code changes.
- **Model tiering**: set `HARNESS_SUBAGENT_MODEL` to give delegates a cheaper
  tier (the provider now honors `agent.model` per request); a subagent's own
  `model:` field wins, unset inherits the parent.
- **Web run view**: with `serve --subagents`, each delegated run renders as a
  nested card inside the parent bubble — the subagent's own thinking, tool
  calls and results stream into it, and its tool approvals flow through the
  same approval dialog as the parent's.

### Approval & sandbox

Dangerous tool calls ask for human approval (`y` / `n` / `a` / `e` / `p`)
according to `permissions.toml` (see `permissions.example.toml`; copy it to
customise, or set `HARNESS_PERMISSIONS_FILE`). `bash` runs through a sandbox:
`HARNESS_SANDBOX_MODE=local` is the zero-isolation development default, while
`HARNESS_SANDBOX_MODE=ssh` executes on a rented server (set `SANDBOX_HOST`,
`SANDBOX_USER`, `SANDBOX_KEY_PATH`) so the local filesystem is untouched.

### Tracing

Every run appends JSONL to `harness.trace.jsonl` (`HARNESS_TRACE_FILE`):
`run_start`, `turn_start`, `model_call`, `tool_call`, `tool_result`, `run_end`
— each with a UTC timestamp.

## Development

```bash
uv run ruff check .      # lint
uv run mypy src          # type-check
uv run pytest -q         # tests (no API key needed)
```

Real-model end-to-end scripts live in `scripts/` (`uv run python scripts/e2e_*.py`).

## Architecture

See [docs/architecture.md](docs/architecture.md) for module layout, data flow
and the extension guide.
