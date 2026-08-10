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
uv run harness chat --subagents     # enable researcher/coder subagents
uv run harness --help
```

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
