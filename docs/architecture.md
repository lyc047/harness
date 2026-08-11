# Architecture

`harness` is a Python agent framework built from scratch around a streaming
tool-call loop. The design mirrors openai-agents-python (Agent/Runner split,
`RunState` snapshots, session abstraction), smolagents (typed memory steps,
`planning_interval`, hooks), and the MCP python-sdk (client).

## Module layout

```
src/harness/
├── config.py            # Settings dataclass (.env / env vars)
├── cli/
│   ├── main.py          # REPL entry + `serve` subcommand (uvicorn factory)
│   └── commands.py      # /commands (session, tools, plan, permissions, …)
├── core/
│   ├── agent.py         # immutable Agent config (name/instructions/tools/…)
│   ├── messages.py      # Message/ToolCall + OpenAI wire format (reasoning passthrough)
│   ├── runner.py        # the turn loop (stateless executor)
│   ├── run_result.py    # RunResult / RunState / RunPaused
│   ├── hooks.py         # lifecycle callbacks (observability, rendering)
│   ├── snapshot.py      # SnapshotExecutor: pre-write file snapshots (rollback)
│   └── compose.py       # build_core_stack(): the one CLI+web composition point
├── llm/
│   ├── base.py          # LLMProvider protocol + StreamEvent types
│   ├── openai_compat.py # DeepSeek/OpenAI-compatible (AsyncOpenAI, retries)
│   └── registry.py      # provider selection
├── tools/
│   ├── base.py          # Tool + @tool decorator (pydantic JSON Schema)
│   ├── registry.py      # ToolRegistry -> OpenAI function schemas
│   ├── builtin/         # read_file, write_file, glob_files, grep_files, bash
│   └── mcp/             # MCP client manager + tool adapter
├── memory/
│   ├── session.py       # SQLite sessions/messages/checkpoints + file snapshots
│   ├── preferences.py   # user preferences (key/value, SQLite)
│   └── store.py         # Store facade (sessions + preferences)
├── agents/
│   ├── subagent.py      # sub-agent config + context isolation
│   ├── registry.py      # YAML subagent registry (bundled + runtime override)
│   └── orchestrator.py  # manager pattern: sub-agents exposed as tools;
│                        #   per-subagent model tiering + run-event sink
│                        #   (the web renders nested subagent runs)
├── planning/
│   ├── planner.py       # plan generation from a task
│   └── executor.py      # step execution + revision (planning_interval)
├── safety/
│   ├── permissions.py   # allow/deny/ask rule engine
│   └── approver.py      # ApprovalExecutor (y/n/a/e/p, fail-closed)
├── skills/
│   ├── registry.py      # markdown + YAML frontmatter skill index
│   ├── loader.py        # runtime skill creation + prompt injection
│   └── bundled/         # skills shipped with the repo (skill-creator, subagent skills)
├── sandbox/
│   ├── base.py          # SandboxResult / SandboxProvider / SandboxedExecutor
│   ├── local.py         # local subprocess (dev, NO isolation)
│   └── remote_ssh.py    # paramiko SSH to a rented server
├── web/
│   ├── events.py        # runner/planner events + messages → WS JSON frames
│   ├── commands.py      # one implementation of every slash command result
│   ├── runtime.py       # WebApprover + per-connection Runtime (mirrors CLI stack)
│   ├── server.py        # FastAPI app: REST + /ws + static files
│   └── static/          # zero-dependency Codex-style frontend (HTML/CSS/JS)
└── observability/
    ├── logging.py       # structured logging (stdout + rotating file)
    └── tracing.py       # JSONL turn/tool traces
```

## Core data flow (the turn loop)

1. **Input** — `Runner.run_streamed(agent, user_input, session_id)` loads prior
   messages from SQLite, prepends the system prompt, appends the user message.
2. **Model call** — the provider streams `StreamText` / `StreamReasoning` /
   `StreamToolCall` / `StreamEnd`. DeepSeek thinking content is preserved in
   `Message.reasoning_content` and passed back verbatim on tool-call turns.
3. **Tool calls** — each `ToolCall` is executed through the **executor chain**:

   ```
   ApprovalExecutor(SandboxedExecutor(SnapshotExecutor(default_executor, sessions), sandbox), permissions)
   ```

   approval first, then sandbox routing for `bash`, then a pre-write
   **snapshot** of every `write_file` target (so the conversation can roll the
   workspace back). Results are appended as `tool` messages.
4. **Loop** — until the model produces a final answer or `max_turns` is
   exceeded. The message list is persisted after every turn, so a killed
   process can resume from the checkpoint.

Every step emits lifecycle events (the `Hooks` callbacks), which the CLI
renders and the `Tracer` records as JSONL.

## Web UI (`harness serve`)

A Codex-style browser interface shares the exact same core stack as the CLI
(`core/compose.py::build_core_stack`), so behaviour cannot drift between
surfaces.

```
browser (vanilla JS SPA) ⇄ FastAPI/uvicorn ⇄ per-connection Runtime ⇄ Runner/Approval/Planner
                                   ⇄ single shared Store (SQLite, WAL)
```

- **One process owns one `Store`** (created in the app lifespan, shared by REST
  and every WebSocket). Each connection gets its own stateful `Runtime` — agent,
  runner, approval, planner — built the same way the CLI builds its stack, so a
  tab behaves like an independent REPL session. `PRAGMA busy_timeout=5000` on
  the session/preference stores absorbs concurrent multi-tab writes.
- **REST = instant operations** (`/api/sessions`, messages, tools, skills,
  permissions, checkpoints, help). **WS = streaming runs** (`message`, `plan`,
  approval decisions, pause/resume, cancel, `command`). Frames are serialized in
  one place (`web/events.py::serialize_event`) so the CLI, WS and REST history
  all agree on the wire shape.
- **`WebApprover`** implements the approval prompt: it pushes an
  `approval_required` frame to the outbox, then awaits a decision from a queue
  the WS receive loop fills. Timeout fails closed (`"n"`); stale decisions from
  a cancelled run are drained before the next one starts.
- **Cancellation** — a new `message`/`plan`/`cancel`/disconnect cancels the
  previous run task. The run tasks convert `RunPaused` into a checkpoint +
  `paused` frame, `MaxTurnsExceeded`/exceptions into `run_error`.
- **Session naming** — sessions carry an optional `name`; an unnamed session is
  titled by an LLM one-liner summary of its first user message (`provider.complete`,
  falling back to plain truncation when the call fails or times out so a slow
  model can never block the turn). Double-click a sidebar title to rename it
  (`PATCH /api/sessions/{id}`); renames and auto-titles persist to SQLite.
- **Rollback** — hovering a user/assistant bubble shows `回退`. The server maps
  the UI step (user/assistant bubbles only) to the DB message `idx`, restores
  every pre-write file snapshot captured after that point (newest first, so two
  writes to one file undo in reverse), then truncates the history and emits a
  `rolled_back` frame. Only `write_file` is tracked — `bash` gives no reliable
  per-file signal, so its mutations are intentionally not rolled back.
- **Branching** — `分叉` copies the conversation `[0..step]` into a new child
  session (`parent_session_id` recorded, `· 分支`-suffixed default name) and
  switches to it. Files stay shared across the branch — both sessions see the
  same workspace.
- **Permission modes** — a status-bar switcher (计划/手动确认/自动/完全放开)
  rewrites the approval decision on the connection's `ApprovalExecutor`. The
  `Mode` enum (`safety/approver.py`) maps: **plan** blocks everything the policy
  does not explicitly allow (read-only planning — reads pass, mutations return a
  blocked error to the model), **ask** is the default ASK dialog behavior,
  **auto** auto-approves ASK decisions while explicit `deny` rules still block,
  and **full** forces every call through regardless of policy (the sandbox
  boundary is unchanged — isolation still applies). The mode is per-connection,
  in-memory only (resets to `ask` on reconnect), and takes effect from the next
  tool call, so a running turn keeps its current approvals. `ready` reports the
  active mode; `{type:"set_mode"}` + the `mode_changed` frame keep the dropdown
  in sync.
- **MCP (per-connection)** — each `Runtime` owns an `MCPClientManager`
  (`tools/mcp/`), managed from the composer via the `/mcp` slash command
  (`add stdio|http` / `list` / `remove`; parsed by the shared
  `build_mcp_config`). Discovered tools register onto the connection's own
  `agent.tools` as `mcp_<server>_<tool>` and hit the same approval→execute
  pipeline (default ASK). The manager closes in `Runtime.shutdown()`, so a tab
  disconnecting tears down its servers; MCP state never crosses tabs.
- **Frontend** is four hand-written files (no build step, no CDN): a markdown
  renderer that is escape-first (`escapeHtml` before any inline/block transform,
  `safeUrl` whitelisting http/https) so model/tool text is never injected as
  HTML; tool-call cards keyed by `tool_call.id`; an approval dialog
  (`y`/`n`/`a`/`p`/edit); pause/resume overlay; session sidebar; `/plan` panel.

## Design principles

- **Agent is config, Runner is stateless.** State flows through explicit
  arguments (`RunState`), never instance attributes.
- **Composition over inheritance.** Approval wraps the sandbox wraps the
  default executor; each layer is independently testable with fakes.
- **Interfaces for extension.** Provider, tool, sandbox, skill, and storage
  all hang off protocols/registries so new capabilities don't touch core.

## Extending harness

- **New tool** — `@tool`-decorate a function (pydantic derives the JSON Schema)
  and register it on the agent.
- **New LLM provider** — implement `LLMProvider` (stream/complete) and register
  it in `llm/registry.py`.
- **New sandbox** — implement `SandboxProvider.run_command/check_available`,
  then add a branch in `sandbox/__init__.py:build_sandbox`.
- **New slash command** — add a branch in `cli/commands.py:handle_command`.
- **New WS frame type** — extend `web/events.py::serialize_event` (server → client)
  and `web/server.py::_dispatch` (client → server); the frontend switch in
  `static/js/app.js` handles it in one place.
- **Observability** — add a field to `core/hooks.py` `Hooks` and wire it in the
  runner; the `Tracer` and the CLI both consume hooks, so a new hook appears in
  both places automatically.
