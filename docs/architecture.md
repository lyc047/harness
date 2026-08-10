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
│   ├── main.py          # REPL entry: builds agent, tools, approval, sandbox
│   └── commands.py      # /commands (session, tools, plan, permissions, …)
├── core/
│   ├── agent.py         # immutable Agent config (name/instructions/tools/…)
│   ├── messages.py      # Message/ToolCall + OpenAI wire format (reasoning passthrough)
│   ├── runner.py        # the turn loop (stateless executor)
│   ├── run_result.py    # RunResult / RunState / RunPaused
│   └── hooks.py         # lifecycle callbacks (observability, rendering)
├── llm/
│   ├── base.py          # LLMProvider protocol + StreamEvent types
│   ├── openai_compat.py # DeepSeek/OpenAI-compatible (AsyncOpenAI, retries)
│   └── registry.py      # provider selection
├── tools/
│   ├── base.py          # Tool + @tool decorator (pydantic JSON Schema)
│   ├── registry.py      # ToolRegistry -> OpenAI function schemas
│   ├── builtin/         # read_file, write_file, glob, grep, bash
│   └── mcp/             # MCP client manager + tool adapter
├── memory/
│   ├── session.py       # SQLite session persistence + checkpoints
│   ├── preferences.py   # user preferences (key/value, SQLite)
│   └── store.py         # Store facade (sessions + preferences)
├── agents/
│   ├── subagent.py      # sub-agent config + context isolation
│   └── orchestrator.py  # manager pattern: sub-agents exposed as tools
├── planning/
│   ├── planner.py       # plan generation from a task
│   └── executor.py      # step execution + revision (planning_interval)
├── safety/
│   ├── permissions.py   # allow/deny/ask rule engine
│   └── approver.py      # ApprovalExecutor (y/n/a/e/p, fail-closed)
├── skills/
│   ├── registry.py      # markdown + YAML frontmatter skill index
│   └── loader.py        # runtime skill creation + prompt injection
├── sandbox/
│   ├── base.py          # SandboxResult / SandboxProvider / SandboxedExecutor
│   ├── local.py         # local subprocess (dev, NO isolation)
│   └── remote_ssh.py    # paramiko SSH to a rented server
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
   ApprovalExecutor(SandboxedExecutor(default_executor, sandbox), permissions)
   ```

   approval first, then sandbox routing for `bash`. Results are appended as
   `tool` messages.
4. **Loop** — until the model produces a final answer or `max_turns` is
   exceeded. The message list is persisted after every turn, so a killed
   process can resume from the checkpoint.

Every step emits lifecycle events (the `Hooks` callbacks), which the CLI
renders and the `Tracer` records as JSONL.

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
- **Observability** — add a field to `core/hooks.py` `Hooks` and wire it in the
  runner; the `Tracer` and the CLI both consume hooks, so a new hook appears in
  both places automatically.
