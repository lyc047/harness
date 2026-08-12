# 子智能体高级编排模式(嵌套 + 并发 + 前端开关)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有单层顺序子智能体之上,实现"高级编排模式":结构深度 2 的嵌套委派、单回合多工具并发执行、前端总开关、总预算护栏、按文件路径互斥 —— 开关关闭时行为与现状完全一致。

**Architecture:** 三个独立正交的机制叠加在现有链路上:(1) Runner 层加 `concurrent` 开关(gather 保序);(2) 执行器链插入 `FileLockExecutor`(按路径互斥);(3) 委派协议升级 —— 子 agent 可带 `nested_delegates`(结构封顶 2 层)、`on_event` 加 `run_id`、`SubagentTool` 带预算检查。Web 端通过 `set_advanced` 开关重建 delegate 工具集,审批协议改为 `tool_call_id` 关联以支持并发审批。

**Tech Stack:** Python ≥ 3.11 / asyncio / FastAPI + WebSocket / 原生 JS 前端(无构建步骤)/ pytest + scripted fake provider。零新依赖。

## Global Constraints

- 质量门:`uv run ruff check . && uv run mypy src && uv run pytest -q` 必须全绿(mypy 严格模式)。
- 平台:Windows 10 / Git Bash / uv;代码必须 Windows 可跑(避免 subprocess/路径陷阱)。
- 兼容性:高级模式默认关闭;关闭时所有现有行为、帧协议、测试原样通过。
- 无新第三方依赖;所有改动仅限仓库现有模块。
- 审批协议向后兼容桥:WebApprover 对"空/未知 tool_call_id 且仅一个待决审批"回退到唯一待决项,旧客户端不带 id 也可用。
- 提交信息用 `feat:`/`fix:`/`test:` 前缀,中文说明,单任务一个提交。

---

### Task 1: 配置字段 `subagent_advanced` / `subagent_budget`

**Files:**
- Modify: `src/harness/config.py`(字段区 ~line 58、`from_env` ~line 128)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 现有 `Settings` dataclass 与 `from_env` 的 `get_bool`/`get_int` helper。
- Produces: `Settings.subagent_advanced: bool`(默认 False)、`Settings.subagent_budget: int`(默认 40);env `HARNESS_SUBAGENT_ADVANCED`(bool)、`HARNESS_SUBAGENT_BUDGET`(int)。后续所有任务读取这两个字段。

- [ ] **Step 1: 写失败测试**(`tests/test_config.py` 末尾追加)

```python
def test_subagent_advanced_and_budget_env():
    s = Settings.from_env(
        {"HARNESS_SUBAGENT_ADVANCED": "1", "HARNESS_SUBAGENT_BUDGET": "12"}
    )
    assert s.subagent_advanced is True
    assert s.subagent_budget == 12

    d = Settings.from_env({})
    assert d.subagent_advanced is False
    assert d.subagent_budget == 40  # safe default


def test_subagent_budget_bad_value_falls_back():
    s = Settings.from_env({"HARNESS_SUBAGENT_BUDGET": "abc"})
    assert s.subagent_budget == 40
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_config.py::test_subagent_advanced_and_budget_env tests/test_config.py::test_subagent_budget_bad_value_falls_back -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'subagent_advanced'`

- [ ] **Step 3: 实现**

`src/harness/config.py` 字段区(`subagent_model` 之后):

```python
    # Multi-agent orchestration: register researcher/coder delegate tools
    subagents: bool = False
    subagent_model: str = ""  # cheaper model for subagents; empty => inherit parent
    subagent_advanced: bool = False  # advanced orchestration (nesting + concurrency)
    subagent_budget: int = 40  # per-run subagent turn budget (advanced-mode guardrail)
```

`from_env` 返回值:

```python
            subagents=get_bool("HARNESS_SUBAGENTS", False),
            subagent_model=get("HARNESS_SUBAGENT_MODEL"),
            subagent_advanced=get_bool("HARNESS_SUBAGENT_ADVANCED", False),
            subagent_budget=get_int("HARNESS_SUBAGENT_BUDGET", 40),
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS(新 2 个 + 既有 5 个)

- [ ] **Step 5: 提交**

```bash
git add src/harness/config.py tests/test_config.py
git commit -m "config: add subagent_advanced + subagent_budget settings"
```

---

### Task 2: Runner 并发工具执行(`run_streamed(concurrent=True)`)

**Files:**
- Modify: `src/harness/core/runner.py`(顶部 import、`run_streamed`/`resume_streamed` 签名、`_run_streamed` 工具循环)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `Runner.run_streamed`/`resume_streamed`(现有)、`ToolExecutor`、`ToolResult`、`Hooks.on_tool_call/on_tool_result`。
- Produces: `Runner.run_streamed(agent, user_input, *, session_id=None, concurrent=False)` 与 `resume_streamed(agent, state, *, session_id=None, concurrent=False)`。并发模式下同一 turn 的多个 tool_call 并行执行、结果保序、单失败不拖垮其余。Task 6/8 的 `SubagentTool` 与 web/cli 靠此开关。

- [ ] **Step 1: 写失败测试**(`tests/test_runner.py`,加 `import asyncio` 与 `from harness.tools.base import ToolResult`)

```python
@pytest.mark.asyncio
async def test_concurrent_tool_calls_preserve_order(make_provider):
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 1}'),
                ToolCall(id="c2", name="add", arguments='{"a": 2, "b": 2}'),
                ToolCall(id="c3", name="add", arguments='{"a": 3, "b": 3}'),
            ]
        ),
        LLMResponse(final_text="done"),
    ]
    runner = Runner(make_provider(script=script))
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    results: list[ToolResult] = []
    async for event in runner.run_streamed(agent, "sum them", concurrent=True):
        if isinstance(event, ToolResultEvent):
            results.append(event.result)
    assert [r.content for r in results] == ["2", "4", "6"]


@pytest.mark.asyncio
async def test_concurrent_one_failure_keeps_others(make_provider):
    from harness.core.runner import default_executor

    async def flaky(agent, tool_call):
        if tool_call.id == "c2":
            raise RuntimeError("boom")
        return await default_executor(agent, tool_call)

    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="c1", name="add", arguments='{"a": 1, "b": 1}'),
                ToolCall(id="c2", name="add", arguments='{"a": 9, "b": 9}'),
                ToolCall(id="c3", name="add", arguments='{"a": 3, "b": 3}'),
            ]
        ),
        LLMResponse(final_text="done"),
    ]
    runner = Runner(make_provider(script=script), tool_executor=flaky)
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    results: list[ToolResult] = []
    async for event in runner.run_streamed(agent, "sum them", concurrent=True):
        if isinstance(event, ToolResultEvent):
            results.append(event.result)
    assert [r.content for r in results] == ["2", "RuntimeError: boom", "6"]
    assert [r.is_error for r in results] == [False, True, False]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_runner.py::test_concurrent_tool_calls_preserve_order -v`
Expected: FAIL — `TypeError: run_streamed() got an unexpected keyword argument 'concurrent'`

- [ ] **Step 3: 实现**

`src/harness/core/runner.py` 顶部加 `import asyncio`。

`run_streamed` / `resume_streamed` 签名加参并透传:

```python
    def run_streamed(
        self,
        agent: Agent,
        user_input: str,
        *,
        session_id: str | None = None,
        concurrent: bool = False,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | RunDone]:
        """Stream events of a full run; a final :class:`RunDone` ends the stream.

        ``concurrent`` runs the tool calls of each multi-call turn in parallel
        (results preserved in call order; a failing call becomes an error
        result and does not abort its siblings). Default False = sequential.
        """
        return self._run_streamed(agent, user_input, session_id=session_id, concurrent=concurrent)

    def resume_streamed(
        self,
        agent: Agent,
        state: RunState,
        *,
        session_id: str | None = None,
        concurrent: bool = False,
    ) -> AsyncIterator[StreamEvent | ToolResultEvent | RunDone]:
        """Continue a paused run from its :class:`RunState` checkpoint."""
        return self._run_streamed(
            agent,
            None,
            session_id=session_id or state.session_id,
            resume_state=state,
            concurrent=concurrent,
        )
```

`_run_streamed` 签名加 `concurrent: bool = False`,把工具循环改为两分支:

```python
        for turn in range(start_turn, max_turns):
            ...
            if response.tool_calls:
                assistant_msg = Message.assistant(
                    content=response.final_text,
                    tool_calls=response.tool_calls,
                    reasoning_content=response.reasoning_content,
                )
                messages.append(assistant_msg)

                tool_messages: list[Message] = []
                if concurrent:
                    # Parallel: fire every on_tool_call first, gather results
                    # (a failing tool must not abort its siblings), then emit
                    # results back in call order so the model sees stable order.
                    for tool_call in response.tool_calls:
                        await self._hooks.emit(self._hooks.on_tool_call, tool_call, agent)
                    gathered = await asyncio.gather(
                        *(self._tool_executor(agent, tc) for tc in response.tool_calls),
                        return_exceptions=True,
                    )
                    results = [
                        res if isinstance(res, ToolResult)
                        else ToolResult.error(f"{type(res).__name__}: {res}")
                        for res in gathered
                    ]
                else:
                    results = []
                    for tool_call in response.tool_calls:
                        await self._hooks.emit(self._hooks.on_tool_call, tool_call, agent)
                        results.append(await self._tool_executor(agent, tool_call))

                for tool_call, tool_result in zip(response.tool_calls, results):
                    await self._hooks.emit(
                        self._hooks.on_tool_result, tool_call, tool_result, agent
                    )
                    tool_messages.append(
                        Message.tool(tool_call.id, tool_result.content, name=tool_call.name)
                    )
                    yield ToolResultEvent(tool_call, tool_result)

                messages.extend(tool_messages)
                await self._persist(session_id, messages)
                if self._pause_check is not None:
                    ...
```

(保持 `_run_streamed` 其余部分不变。)

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_runner.py -v`
Expected: PASS(新 2 个 + 既有 5 个;默认顺序路径回归通过)

- [ ] **Step 5: 提交**

```bash
git add src/harness/core/runner.py tests/test_runner.py
git commit -m "runner: concurrent tool-call execution (gather, order-preserved)"
```

---

### Task 3: 按文件路径互斥 —— `FileLockExecutor`

**Files:**
- Create: `src/harness/core/locking.py`
- Modify: `src/harness/core/compose.py`(import + 执行器链 ~line 154)
- Test: `tests/test_locking.py`

**Interfaces:**
- Consumes: `ToolExecutor`、`Agent`、`ToolCall`、`ToolResult`。
- Produces: `FileLockExecutor(inner: ToolExecutor)`,对 `write_file`/`read_file` 解析 `path` 参数并获取进程级按路径锁;其余工具透传。Task 8 之前链上常驻,无竞争零开销。
- 位置:执行器链 = 审批 → 沙箱 → **文件锁** → 快照 → 工具(bash 被沙箱拦截,到不了文件锁)。

- [ ] **Step 1: 写失败测试**(新建 `tests/test_locking.py`)

```python
"""Unit tests for FileLockExecutor (per-path mutual exclusion)."""

from __future__ import annotations

import asyncio

import pytest

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.locking import FileLockExecutor
from harness.tools.base import ToolResult


def _agent() -> Agent:
    return Agent(name="a", instructions="i", model="m")


@pytest.mark.asyncio
async def test_same_path_writes_serialize() -> None:
    in_flight = 0
    max_in_flight = 0

    async def inner(agent, tool_call):  # noqa: ARG001
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ToolResult.ok("done")

    ex = FileLockExecutor(inner)
    calls = [
        ToolCall(id="a", name="write_file", arguments='{"path": "x.txt", "content": "1"}'),
        ToolCall(id="b", name="write_file", arguments='{"path": "x.txt", "content": "2"}'),
    ]
    await asyncio.gather(*(ex(_agent(), tc) for tc in calls))
    assert max_in_flight == 1  # never two writers inside at once


@pytest.mark.asyncio
async def test_different_paths_run_in_parallel() -> None:
    in_flight = 0
    max_in_flight = 0

    async def inner(agent, tool_call):  # noqa: ARG001
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ToolResult.ok("done")

    ex = FileLockExecutor(inner)
    calls = [
        ToolCall(id="a", name="write_file", arguments='{"path": "x.txt", "content": "1"}'),
        ToolCall(id="b", name="write_file", arguments='{"path": "y.txt", "content": "2"}'),
    ]
    await asyncio.gather(*(ex(_agent(), tc) for tc in calls))
    assert max_in_flight == 2  # different paths do not block each other


@pytest.mark.asyncio
async def test_read_waits_for_write_on_same_path() -> None:
    events: list[str] = []

    async def inner(agent, tool_call):  # noqa: ARG001
        if tool_call.name == "write_file":
            events.append("write-start")
            await asyncio.sleep(0.01)
            events.append("write-end")
        else:
            events.append("read")
        return ToolResult.ok("ok")

    ex = FileLockExecutor(inner)
    calls = [
        ToolCall(id="w", name="write_file", arguments='{"path": "f", "content": "x"}'),
        ToolCall(id="r", name="read_file", arguments='{"path": "f"}'),
    ]
    await asyncio.gather(*(ex(_agent(), tc) for tc in calls))
    # the read must be fully outside the write's critical section
    assert events.index("read") > events.index("write-end")


@pytest.mark.asyncio
async def test_non_file_tools_pass_through() -> None:
    seen: list[str] = []

    async def inner(agent, tool_call):  # noqa: ARG001
        seen.append(tool_call.id)
        return ToolResult.ok("ok")

    ex = FileLockExecutor(inner)
    await ex(_agent(), ToolCall(id="b", name="bash", arguments='{"command": "ls"}'))
    assert seen == ["b"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_locking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.core.locking'`

- [ ] **Step 3: 实现**

新建 `src/harness/core/locking.py`:

```python
"""Per-path file locking so concurrent file access serializes per path.

A process-global registry of ``asyncio.Lock`` keyed by the resolved absolute
path. ``FileLockExecutor`` sits between the sandbox and the snapshot executor:
for ``write_file``/``read_file`` it acquires the path's lock, making
snapshot+write atomic and giving every path a single writer (readers wait).
``bash`` is intentionally not covered — the sandbox handles it before reaching
this layer, and a raw command string gives no reliable per-file signal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import ToolExecutor
from harness.tools.base import ToolResult

_LOCKS: dict[str, asyncio.Lock] = {}


def _path_lock(path: str) -> asyncio.Lock:
    key = str(Path(path).resolve())
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


class FileLockExecutor:
    """Serialize per-path file access under the process-global lock registry."""

    def __init__(self, inner: ToolExecutor) -> None:
        self._inner = inner

    async def __call__(self, agent: Agent, tool_call: ToolCall) -> ToolResult:
        if tool_call.name not in ("read_file", "write_file"):
            return await self._inner(agent, tool_call)
        path = str(tool_call.arguments_dict.get("path", ""))
        if not path:
            return await self._inner(agent, tool_call)
        async with _path_lock(path):
            return await self._inner(agent, tool_call)
```

`src/harness/core/compose.py`:顶部加 `from harness.core.locking import FileLockExecutor`,执行器链改为:

```python
    # Pre-write snapshots of every write_file target (rollback support). Sits
    # inside the sandbox (bash never reaches it) but outside default_executor.
    base_executor = tool_executor or default_executor
    base_executor = SnapshotExecutor(base_executor, store.sessions)
    base_executor = FileLockExecutor(base_executor)  # per-path mutual exclusion
    sandboxed = SandboxedExecutor(base_executor, sandbox)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_locking.py tests/test_snapshot.py tests/test_web_runtime.py -v`
Expected: PASS(新 4 个 + 快照/回滚回归 —— 文件锁不改变顺序语义)

- [ ] **Step 5: 提交**

```bash
git add src/harness/core/locking.py src/harness/core/compose.py tests/test_locking.py
git commit -m "executor: per-path file lock (serialize write/read of same file)"
```

---

### Task 4: 总预算护栏 —— `SubagentBudget`

**Files:**
- Modify: `src/harness/agents/orchestrator.py`(`+SubagentBudget` 类)
- Modify: `src/harness/core/compose.py`(`CoreStack` 字段 + `build_core_stack` 创建)
- Modify: `src/harness/web/runtime.py`(四个 run 入口重置预算)
- Modify: `src/harness/cli/main.py`(每次输入前重置)
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: `Settings.subagent_budget`。
- Produces: `SubagentBudget(total: int)` 类:`remaining() -> int`、`record(turns: int) -> None`、`reset() -> None`;`CoreStack.subagent_budget: SubagentBudget`。Task 6 的 `SubagentTool` 持有预算引用做检查/记录。

- [ ] **Step 1: 写失败测试**(`tests/test_agents.py` 末尾追加)

```python
def test_subagent_budget_tracks_and_resets() -> None:
    from harness.agents.orchestrator import SubagentBudget

    b = SubagentBudget(total=10)
    assert b.remaining() == 10
    b.record(3)
    assert b.remaining() == 7
    b.record(8)
    assert b.remaining() == -1  # over-run is recorded, not clamped
    b.reset()
    assert b.remaining() == 10
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agents.py::test_subagent_budget_tracks_and_resets -v`
Expected: FAIL — `ImportError: cannot import name 'SubagentBudget'`

- [ ] **Step 3: 实现**

`src/harness/agents/orchestrator.py` 末尾加:

```python
class SubagentBudget:
    """Per-run budget of subagent turns, shared across nesting levels.

    asyncio is single-threaded, so ``record``/``remaining`` are race-free even
    when several subagents run concurrently. ``remaining`` may go negative on
    over-run (a soft guardrail, not a hard cap mid-flight).
    """

    def __init__(self, total: int) -> None:
        self._total = total
        self._used = 0

    def remaining(self) -> int:
        return self._total - self._used

    def record(self, turns: int) -> None:
        self._used += turns

    def reset(self) -> None:
        self._used = 0
```

`src/harness/core/compose.py`:
- `from harness.agents.orchestrator import ...`? 注意 compose 用懒 import 避免耦合。`CoreStack` 字段类型标注需要 `SubagentBudget`。最简:直接在 compose 顶层 import(agents 包已通过 `add_example_subagents` 懒引用;加一个顶层 `from harness.agents.orchestrator import SubagentBudget` 可接受,orchestrator 不 import compose,无环)。加:
  `from harness.agents.orchestrator import SubagentBudget`
- `CoreStack` 增加字段:`subagent_budget: SubagentBudget`
- `build_core_stack` 构造:`subagent_budget=SubagentBudget(settings.subagent_budget)`,加入返回的 `CoreStack(...)`。

`src/harness/web/runtime.py`:在 `start_run`、`start_plan`、`resume`、`resume_checkpoint` 各加一行(放在 `_approver.drain()` 之后):

```python
        if self._stack is not None:
            self._stack.subagent_budget.reset()
```

`src/harness/cli/main.py`:在 `_run_chat` 的 run 循环里、`pause_after_turn[0] = False` 之后加:

```python
        stack.subagent_budget.reset()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agents.py tests/test_web_runtime.py -v`
Expected: PASS(新 1 个 + 全部回归)

- [ ] **Step 5: 提交**

```bash
git add src/harness/agents/orchestrator.py src/harness/core/compose.py src/harness/web/runtime.py src/harness/cli/main.py tests/test_agents.py
git commit -m "subagents: per-run turn budget (SubagentBudget, reset at run starts)"
```

---

### Task 5: run_id 实例标识(嵌套/同名并发不串卡片)

**Files:**
- Modify: `src/harness/agents/orchestrator.py`(`on_event` 类型、`invoke` 生成 run_id)
- Modify: `src/harness/core/compose.py`(`add_example_subagents` 的 `on_event` 类型)
- Modify: `src/harness/web/runtime.py`(`_forward_subagent_event` 签名 + 帧带 `run_id`)
- Modify: `src/harness/web/static/js/app.js`(`subagentStack` 以 run_id 为键)
- Test: `tests/test_web_runtime.py`(更新 `test_runtime_subagent_events_forwarded` 断言 run_id)

**Interfaces:**
- Consumes: 现有 `on_event` 回调链。
- Produces: `on_event` 签名变为 `Callable[[str, str, object], Awaitable[None]]`,即 `(run_id, agent, event)`;WS 帧 `subagent_start`/`subagent_event`/`subagent_end` 均带 `run_id`。前端 `subagentStack` 元素带 `runId`,按 runId 匹配。Task 6 的嵌套流(深度优先)依赖此键。

- [ ] **Step 1: 更新测试断言 run_id**(`tests/test_web_runtime.py::test_runtime_subagent_events_forwarded`)

在 `start = next(...)` 之后加:

```python
    assert start["run_id"]
    end = next(f for f in frames if f["type"] == "subagent_end")
    assert end["run_id"] == start["run_id"]
    for ev in sub_events:
        assert ev["run_id"] == start["run_id"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web_runtime.py::test_runtime_subagent_events_forwarded -v`
Expected: FAIL — `KeyError: 'run_id'`

- [ ] **Step 3: 实现**

`src/harness/agents/orchestrator.py`:
- 顶部 `import uuid`。
- `SubagentTool.__init__` 的 `on_event` 类型改为 `Callable[[str, str, object], Awaitable[None]] | None`。
- `invoke` 生成 run_id 并透传:

```python
        run_id = uuid.uuid4().hex
        if self._on_event is not None:
            await self._on_event(run_id, self.subagent.name, SubagentRunStart())
        ...
            async for event in self._runner.run_streamed(agent, brief, session_id=None):
                if self._on_event is not None and not isinstance(event, RunDone):
                    await self._on_event(run_id, self.subagent.name, event)
        ...
        if self._on_event is not None:
            await self._on_event(
                run_id, self.subagent.name,
                SubagentRunEnd(output=output, turns=turns, is_error=is_error),
            )
```

- `subagent_as_tool` 与 `add_subagents` 的 `on_event` 类型改为 `Callable[[str, str, object], Awaitable[None]] | None`。

`src/harness/core/compose.py`:同类型改动(`add_example_subagents` 的 `on_event` 参数)。

`src/harness/web/runtime.py`:

```python
    async def _forward_subagent_event(self, run_id: str, agent: str, event: object) -> None:
        """Forward a nested subagent run's event to the client.

        Each delegated run carries its own ``run_id`` so the UI can key nested
        run views by instance — two concurrent delegates of the same subagent,
        or a depth-2 nested run, each get their own card.
        """
        if isinstance(event, SubagentRunStart):
            await self._emit({"type": "subagent_start", "run_id": run_id, "agent": agent})
        elif isinstance(event, SubagentRunEnd):
            await self._emit(
                {
                    "type": "subagent_end",
                    "run_id": run_id,
                    "agent": agent,
                    "output": event.output,
                    "turns": event.turns,
                    "is_error": event.is_error,
                }
            )
        else:
            frame = serialize_event(event)
            if frame is not None:
                await self._emit(
                    {"type": "subagent_event", "run_id": run_id, "agent": agent, "event": frame}
                )
```

`src/harness/web/static/js/app.js`:
- `startSubagentRun` 改为 `function startSubagentRun(name, runId)`,压栈对象加 `runId: runId`(保留 `name` 用于标题)。
- `routeSubagentEvent` 改为 `function routeSubagentEvent(runId, agent, ev)`,按 `subagentStack[i].runId === runId` 查找。
- `endSubagentRun` 用 `msg.run_id` 匹配(`subagentStack[i].runId === msg.run_id`)。
- `handleMessage` 分支:

```js
      case 'subagent_start':
        startSubagentRun(msg.agent, msg.run_id);
        break;
      case 'subagent_event':
        routeSubagentEvent(msg.run_id, msg.agent, msg.event);
        break;
      case 'subagent_end':
        endSubagentRun(msg);
        break;
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_web_runtime.py -v && node --check src/harness/web/static/js/app.js`
Expected: PASS(JS 语法检查通过)

- [ ] **Step 5: 提交**

```bash
git add src/harness/agents/orchestrator.py src/harness/core/compose.py src/harness/web/runtime.py src/harness/web/static/js/app.js tests/test_web_runtime.py
git commit -m "subagents: run_id instance ids (per-run keying for nested/concurrent views)"
```

---

### Task 6: 嵌套委派 + 高级工具注册 + 预算执行 + 协议变体

**Files:**
- Modify: `src/harness/agents/subagent.py`(`as_agent` 加 `extra_tools`/`extra_instructions`)
- Modify: `src/harness/agents/orchestrator.py`(`SubagentTool` 加 `nested_delegates`/`concurrent`/`budget`/`nested_hint`;`add_subagents` 加 `advanced`;`attach_delegation_protocol` 可逆;`DELEGATION_PROTOCOL_ADVANCED`/`DELEGATION_HINT` 常量)
- Modify: `src/harness/core/compose.py`(`add_example_subagents` 加 `advanced`,内部派生 concurrent/budget)
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: Task 4 `SubagentBudget`;Task 5 `on_event(run_id,…)`;Task 2 `run_streamed(concurrent=…)`。
- Produces:
  - `Subagent.as_agent(model="", extra_tools=(), extra_instructions="") -> Agent`(extra 工具注册到 registry 副本,绝不动共享 `Subagent.tools`)。
  - `SubagentTool.__init__(..., on_event=None, concurrent=False, budget=None, nested_delegates=(), nested_hint="")`。
  - `subagent_as_tool(subagent, runner, default_model, *, on_event=None, concurrent=False, budget=None, nested_delegates=(), nested_hint="")`。
  - `add_subagents(agent, runner, subagents, *, default_model=None, on_event=None, concurrent=False, budget=None, advanced=False)`。
  - `add_example_subagents(stack, *, subagent_model="", on_event=None, advanced=False)`。
  - `attach_delegation_protocol(agent, *, advanced=False)`(可逆替换)。
  - `SubagentTool.invoke` 做预算检查(`budget.remaining() <= 0` → error)与记录(`budget.record(turns)`)。

- [ ] **Step 1: 写失败测试**(`tests/test_agents.py` 追加)

```python
def test_advanced_add_subagents_builds_nested_delegates(make_provider) -> None:
    from harness.agents.orchestrator import add_subagents

    agent = Agent(name="parent", instructions="p", model="m")
    runner = Runner(make_provider())
    add_subagents(agent, runner, [_subagent("a"), _subagent("b")], advanced=True)
    assert set(agent.tools.names()) == {"delegate_to_a", "delegate_to_b"}
    tool_a = agent.tools.get("delegate_to_a")
    assert tool_a is not None
    # every OTHER subagent is a nested delegate; never itself
    assert sorted(t.name for t in tool_a._nested_delegates) == ["delegate_to_b"]
    # nested delegates carry no further delegates (structural depth cap of 2)
    nested_b = tool_a._nested_delegates[0]
    assert nested_b._nested_delegates == ()
    assert nested_b._concurrent is True
    assert tool_a._concurrent is True


def test_attach_delegation_protocol_swaps_variants() -> None:
    from harness.agents.orchestrator import (
        DELEGATION_PROTOCOL,
        DELEGATION_PROTOCOL_ADVANCED,
        attach_delegation_protocol,
    )

    agent = Agent(name="parent", instructions="base", model="m")
    attach_delegation_protocol(agent)
    assert agent.instructions.count("Delegation protocol") == 1
    assert agent.instructions.endswith(DELEGATION_PROTOCOL)
    attach_delegation_protocol(agent, advanced=True)
    assert agent.instructions.count("Delegation protocol") == 1  # swapped, not appended
    assert agent.instructions.endswith(DELEGATION_PROTOCOL_ADVANCED)
    attach_delegation_protocol(agent)
    assert agent.instructions.count("Delegation protocol") == 1
    assert agent.instructions.endswith(DELEGATION_PROTOCOL)


async def test_advanced_nested_delegation_two_levels(make_provider) -> None:
    """parent -> a -> b: level-2 subagent runs inside the level-1 subagent's
    isolated stream, results bubble back through the delegate chain."""
    from harness.agents.orchestrator import add_subagents

    script = [
        LLMResponse(tool_calls=[ToolCall(id="p1", name="delegate_to_a", arguments='{"task": "outer"}')]),
        LLMResponse(tool_calls=[ToolCall(id="a1", name="delegate_to_b", arguments='{"task": "inner"}')]),
        LLMResponse(final_text="B delivered"),
        LLMResponse(final_text="A delivered"),
        LLMResponse(final_text="parent done"),
    ]
    provider = make_provider(script)
    agent = Agent(name="parent", instructions="p", model="m")
    runner = Runner(provider)
    add_subagents(agent, runner, [_subagent("a"), _subagent("b")], advanced=True)

    result = await runner.run(agent, "go", session_id=None)
    assert result.final_output == "parent done"


async def test_subagent_budget_exhausted_returns_error(make_provider) -> None:
    from harness.agents.orchestrator import SubagentBudget, subagent_as_tool

    budget = SubagentBudget(total=1)
    runner = Runner(make_provider())
    tool = subagent_as_tool(
        _subagent(), runner, default_model="m", budget=budget, advanced=True
    )
    assert not (await tool.invoke(task="x")).is_error
    budget.record(1)
    denied = await tool.invoke(task="y")
    assert denied.is_error
    assert "budget exhausted" in denied.content
    # nothing ran for the denied call
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agents.py -v`
Expected: FAIL(`TypeError: add_subagents() got an unexpected keyword argument 'advanced'`、`AttributeError: 'SubagentTool' object has no attribute '_nested_delegates'`)

- [ ] **Step 3: 实现**

`src/harness/agents/subagent.py`:`from harness.tools.base import Tool`;`as_agent` 改为:

```python
    def as_agent(
        self,
        model: str = "",
        extra_tools: tuple[Tool, ...] = (),
        extra_instructions: str = "",
    ) -> Agent:
        """Materialise as a runnable :class:`Agent` (empty model inherits later).

        ``extra_tools`` (nested delegate tools for advanced mode) register onto
        a *copy* of the subagent's registry so the shared ``Subagent.tools`` is
        never mutated. ``extra_instructions`` append after the subagent's own.
        """
        tools = ToolRegistry()
        for t in self.tools.all():
            tools.register(t)
        for t in extra_tools:
            tools.register(t)
        instructions = self.instructions
        if extra_instructions:
            instructions = f"{instructions.rstrip()}\n\n{extra_instructions}"
        return Agent(
            name=self.name,
            instructions=instructions,
            tools=tools,
            model=model or self.model,
            max_turns=self.max_turns,
        )
```

`src/harness/agents/orchestrator.py`:
- 顶部 `from harness.tools.base import Tool, ToolResult`(已有 `Tool`);`import uuid`。
- 加常量:

```python
# Advanced mode: appended after DELEGATION_PROTOCOL so a level-1 subagent knows
# it can hand off a sub-task once more (structurally capped at two levels).
DELEGATION_PROTOCOL_ADVANCED = DELEGATION_PROTOCOL + """

## Deeper delegation (advanced mode)

You can also delegate a sub-task to another subagent via `delegate_to_<name>`,
the same way your parent delegates to you. Give it a complete, self-contained
brief. Nested delegation is at most two levels deep — never hand off a task
you can do yourself just to chain subagents.
"""

# Short hint appended to level-1 subagents' agents in advanced mode.
DELEGATION_HINT = (
    "You can delegate a sub-task to another subagent via its `delegate_to_<name>` "
    "tool. Choose the best-fit subagent and give it a self-contained brief. "
    "Nested delegation is at most two levels deep."
)
```

- `SubagentTool.__init__` 加参并存储:

```python
        on_event: Callable[[str, str, object], Awaitable[None]] | None = None,
        concurrent: bool = False,
        budget: SubagentBudget | None = None,
        nested_delegates: tuple[Tool, ...] = (),
        nested_hint: str = "",
        advanced: bool = False,
```

```python
        self._on_event = on_event
        self._concurrent = concurrent
        self._budget = budget
        self._nested_delegates = nested_delegates
        self._nested_hint = nested_hint
        self._advanced = advanced
```

- `invoke` 开头(在组装 brief 之后、run 之前)加预算检查,并把 agent 构造与 run_streamed 改为带新参数。检查用 spec 7.2 的门控:`_advanced` 与 `_budget` 双条件 —— 普通模式工具 `advanced=False`(且 `budget=None`),永不检查预算:

```python
        if self._advanced and self._budget is not None and self._budget.remaining() <= 0:
            return ToolResult.error("subagent budget exhausted", agent=self.subagent.name)
        ...
        agent = self.subagent.as_agent(
            model=self._model,
            extra_tools=self._nested_delegates,
            extra_instructions=self._nested_hint,
        )
        run_id = uuid.uuid4().hex
        ...
            async for event in self._runner.run_streamed(
                agent, brief, session_id=None, concurrent=self._concurrent
            ):
        ...
        if self._budget is not None:
            self._budget.record(turns)
```

(预算记录放在 `SubagentRunEnd` 上报之前、is_error 判断之前。)

- `subagent_as_tool` 加参并透传:

```python
def subagent_as_tool(
    subagent: Subagent,
    runner: Runner,
    default_model: str,
    *,
    on_event: Callable[[str, str, object], Awaitable[None]] | None = None,
    concurrent: bool = False,
    budget: SubagentBudget | None = None,
    nested_delegates: tuple[Tool, ...] = (),
    nested_hint: str = "",
    advanced: bool = False,
) -> Tool:
    ...
    return SubagentTool(
        ...,
        on_event=on_event,
        concurrent=concurrent,
        budget=budget,
        nested_delegates=nested_delegates,
        nested_hint=nested_hint,
        advanced=advanced,
    )
```

- `add_subagents` 加 `concurrent`/`budget`/`advanced`,分普通/高级两条路径:

```python
def add_subagents(
    agent: Agent,
    runner: Runner,
    subagents: list[Subagent],
    *,
    default_model: str | None = None,
    on_event: Callable[[str, str, object], Awaitable[None]] | None = None,
    concurrent: bool = False,
    budget: SubagentBudget | None = None,
    advanced: bool = False,
) -> None:
    """Register every subagent as a delegation tool on ``agent``.

    ``advanced`` turns on nesting: each subagent's agent gains delegate tools
    for every *other* subagent (one more level, structurally capped — nested
    delegates carry no further delegates, so delegation can never cycle).
    Advanced mode also runs each subagent's own turns concurrently and passes
    ``budget`` so nested runs share the per-run turn budget.
    """
    base = default_model or agent.model
    if not advanced:
        for sa in subagents:
            agent.tools.register(
                subagent_as_tool(sa, runner, base, on_event=on_event)
            )
        return
    level2 = {
        sa.name: subagent_as_tool(
            sa, runner, base,
            on_event=on_event, concurrent=True, budget=budget, advanced=True,
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
                nested_hint=DELEGATION_HINT,
                advanced=True,
            )
        )
```

- `attach_delegation_protocol` 改为可逆:

```python
def attach_delegation_protocol(agent: Agent, *, advanced: bool = False) -> None:
    """Append (or replace) delegation guidance to a parent agent's instructions.

    Reversible: strips any previously-appended protocol block (either variant)
    before appending the one for the requested mode, so re-registering delegate
    tools on an advanced toggle never duplicates or leaves stale guidance.
    """
    stripped = agent.instructions.rstrip()
    for variant in (DELEGATION_PROTOCOL_ADVANCED, DELEGATION_PROTOCOL):
        if stripped.endswith(variant):
            stripped = stripped[: -len(variant)].rstrip()
            break
    protocol = DELEGATION_PROTOCOL_ADVANCED if advanced else DELEGATION_PROTOCOL
    agent.instructions = f"{stripped}\n\n{protocol}"
```

`src/harness/core/compose.py`:`add_example_subagents` 签名加 `advanced: bool = False`,`on_event` 类型改为 `Callable[[str, str, object], Awaitable[None]]`,内部:

```python
    add_subagents(
        stack.agent,
        stack.runner,
        example_subagents(),
        default_model=subagent_model or None,
        on_event=on_event,
        concurrent=advanced,
        budget=stack.subagent_budget if advanced else None,
        advanced=advanced,
    )
    attach_delegation_protocol(stack.agent, advanced=advanced)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agents.py tests/test_web_runtime.py -v`
Expected: PASS(新 5 个 + 全部回归;普通模式 `add_example_subagents` 默认 advanced=False 行为不变)

- [ ] **Step 5: 提交**

```bash
git add src/harness/agents/subagent.py src/harness/agents/orchestrator.py src/harness/core/compose.py tests/test_agents.py
git commit -m "subagents: nested delegation (depth-2), advanced tool registration, budget enforcement"
```

---

### Task 7: 审批协议 `tool_call_id` 关联(并发审批各自配对)

**Files:**
- Modify: `src/harness/web/runtime.py`(`WebApprover` 改为 per-id pending futures;`Runtime` 加 `approve()`;删 `self.decisions`)
- Modify: `src/harness/web/server.py`(`approval` 分支)
- Modify: `src/harness/web/static/js/app.js`(审批队列 + 回传 `tool_call_id`)
- Test: `tests/test_web_runtime.py`(重写 approver 单测 + 改 `_auto_approve`/内联 approve)、`tests/test_web_server.py`(approval 帧带 id 的回传)

**Interfaces:**
- Consumes: `ToolCall.id`(approval_required 帧已携带)。
- Produces: `WebApprover(outbox, *, timeout=...)`(删 decisions 参数)、`async approve(tool_call_id: str, decision: str)`、`Runtime.approve(tool_call_id, decision)`(同步)。服务端 WS 消息 `{type:"approval", tool_call_id, decision}`。前端审批队列逐条作答。
- 兼容桥:空/未知 id 且仅一个 pending → 回退到唯一待决项。

- [ ] **Step 1: 重写 WebApprover 单测并加关联测试**(`tests/test_web_runtime.py` 顶部 approver 段)

```python
async def test_web_approver_returns_allow_once() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    await approver.approve("t1", "y")
    assert await task == "y"
    frame = json.loads(outbox.get_nowait())
    assert frame["type"] == "approval_required"
    assert frame["tool_call"]["name"] == "bash"


async def test_web_approver_correlates_by_tool_call_id() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    t1 = _tool_call()
    t2 = ToolCall(id="t2", name="bash", arguments='{"command": "ls"}')
    task1 = asyncio.create_task(approver.prompt(t1))
    task2 = asyncio.create_task(approver.prompt(t2))
    await asyncio.sleep(0)
    await approver.approve("t2", "n")
    await approver.approve("t1", "y")
    assert await task2 == "n"
    assert await task1 == "y"  # each decision matched its own call


async def test_web_approver_unknown_id_dropped() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox, timeout=0.05)
    t1 = _tool_call()
    t2 = ToolCall(id="t2", name="bash", arguments='{"command": "ls"}')
    task1 = asyncio.create_task(approver.prompt(t1))
    task2 = asyncio.create_task(approver.prompt(t2))
    await asyncio.sleep(0)
    await approver.approve("nope", "y")  # matches nothing -> dropped
    assert await task1 == "n"  # both time out fail-closed
    assert await task2 == "n"


async def test_web_approver_single_pending_fallback() -> None:
    """Compat bridge: an empty/unknown id with exactly one pending approval
    resolves that pending one (old clients that omit the id keep working in
    sequential mode)."""
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    await approver.approve("", "y")
    assert await task == "y"


async def test_web_approver_timeout_fails_closed() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox, timeout=0.05)
    assert await approver.prompt(_tool_call()) == "n"


async def test_web_approver_drain_cancels_pending() -> None:
    outbox: asyncio.Queue[str] = asyncio.Queue()
    approver = WebApprover(outbox)
    task = asyncio.create_task(approver.prompt(_tool_call()))
    await asyncio.sleep(0)
    approver.drain()
    with pytest.raises(asyncio.CancelledError):
        await task
```

(删除旧的 `test_web_approver_deny`、`test_web_approver_edit_args_passthrough`、`test_web_approver_cancel_unblocks`、`test_web_approver_drains_stale_decisions` —— 语义被上面的覆盖/替代。)

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web_runtime.py -v`
Expected: FAIL(`TypeError: WebApprover.__init__() got an unexpected keyword argument 'decisions'`)

- [ ] **Step 3: 实现**

`src/harness/web/runtime.py` `WebApprover` 重写:

```python
class WebApprover:
    """Approval prompt that pushes a request to the outbox and awaits a decision.

    Each pending approval is keyed by its ``tool_call.id`` so concurrent
    approval prompts (advanced mode) are matched to the right decision. An
    empty/unknown id with exactly one pending resolves that one — the compat
    bridge for clients that omit the id in sequential mode. Timeout fails
    closed (``"n"``); ``drain`` cancels pending futures so a cancelled run's
    leftovers can't satisfy the next prompt.
    """

    def __init__(
        self,
        outbox: asyncio.Queue[str],
        *,
        timeout: float = APPROVAL_TIMEOUT,
    ) -> None:
        self._outbox = outbox
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[str]] = {}

    async def prompt(self, tool_call: ToolCall) -> str:
        payload = {
            "type": "approval_required",
            "tool_call": {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        }
        fut = asyncio.get_running_loop().create_future()
        self._pending[tool_call.id] = fut
        await self._outbox.put(json.dumps(payload, ensure_ascii=False))
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except TimeoutError:
            logger.warning("approval for %r timed out; fail closed", tool_call.name)
            return "n"
        finally:
            self._pending.pop(tool_call.id, None)

    async def approve(self, tool_call_id: str, decision: str) -> None:
        fut = self._pending.pop(tool_call_id, None)
        if fut is None and len(self._pending) == 1:
            (fut,) = list(self._pending.values())  # compat bridge: sole pending
        if fut is not None and not fut.done():
            fut.set_result(decision)

    def drain(self) -> None:
        """Cancel every pending approval so stale decisions can't survive a run."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
```

`Runtime.__init__`:删 `self.decisions`;`self._approver = WebApprover(self.outbox, timeout=approval_timeout)`。加:

```python
    async def approve(self, tool_call_id: str, decision: str) -> None:
        """Route one WS approval decision to the matching pending prompt."""
        await self._approver.approve(tool_call_id, decision)
```

(`WebApprover.approve` 与 `Runtime.approve` 均为 **async**,与 spec 5 一致;`approve` 只 resolve 一个 future,立即完成,不阻塞 `_dispatch`。)

`src/harness/web/server.py`(`_dispatch` 是 `async def`,`await` 合法):

```python
    elif mtype == "approval":
        await rt.approve(
            str(msg.get("tool_call_id", "")),
            str(msg.get("decision", "n")),
        )
```

- [ ] **Step 4: 更新运行时其余测试**(`tests/test_web_runtime.py`)

搜索替换所有 `rt.decisions.put_nowait(X)` → `await rt.approve("", X)`(`""` 走 single-pending 兼容桥;顺序场景恰好唯一 pending)。出现位置(均已核实):第 226 行 `"n"`、第 252 行 `"p"`、第 587/617/667 行 `"y"`。`_auto_approve`(第 383 行,async helper)第 391 行同样改为:

```python
            await rt.approve("", "y")
```

(测试第 76-133 行旧的 decisions-queue approver 单测全部由 Step 1 的改写版替代,不再保留 `decisions` 队列。)

`tests/test_web_server.py`:WS 层 `{"type":"approval","decision":"y"}` 保持可用(兼容桥)或显式带 id。把三处 `ws.send_json({"type": "approval", "decision": ...})` 改为带 id:

```python
            ws.send_json({"type": "approval", "tool_call_id": "t1", "decision": "y"})
```

(`test_ws_approval_edit_args` 用 `"t1"`;其余按对应脚本里的 ToolCall id。)

`tests/test_web_server.py`:WS 层 `{"type":"approval","decision":"y"}` 保持可用(兼容桥)或显式带 id。把三处 `ws.send_json({"type": "approval", "decision": ...})` 改为带 id:

```python
            ws.send_json({"type": "approval", "tool_call_id": "t1", "decision": "y"})
```

(`test_ws_approval_edit_args` 用 `"t1"`;其余按对应脚本里的 ToolCall id。)

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_web_runtime.py tests/test_web_server.py -v`
Expected: PASS(重写后的 approver 单测 + 全部运行时/服务端回归)

- [ ] **Step 6: 前端审批队列**(`src/harness/web/static/js/app.js`)

真实代码现状:`openApprovalDialog(tc)`(472 行,设置 `state.currentApproval` 并显示 overlay)、`closeApprovalDialog(decision)`(490 行,隐藏 overlay、清 `currentApproval`、markRunning 当前卡)、`submitDecision(decision)`(504 行,发送 `{type:'approval', decision}` 不带 id)。

改造:
- 模块顶部声明 `let approvalQueue = [];`(与 `subagentStack` 同区)。
- 新增 `showNextApproval()`(队列空则直接返回;否则弹下一张):

```js
  function showNextApproval() {
    if (!approvalQueue.length) return;
    openApprovalDialog(approvalQueue[0]);
  }
```

- 改写 `submitDecision` —— 队首出队,`tool_call_id` 原样回传(其余分支不动):

```js
  function submitDecision(decision) {
    const tc = approvalQueue.shift();
    if (tc) send({ type: 'approval', tool_call_id: tc.id, decision: decision });
    closeApprovalDialog();
    showNextApproval();
    setPhase('running');
  }
```

- 新增 `resetApprovals()`(清空队列 + 隐藏 overlay + 清 `currentApproval`),并**替换**现有的 4 处 `closeApprovalDialog()` 调用点(已核实:第 857 行 `run_done`、第 864 行 `run_error`、第 869 行 `run_cancelled`、第 923 行 `plan_done` —— 这些分支隐藏的是"整个运行"的审批,不只是当前卡):

```js
  function resetApprovals() {
    approvalQueue = [];
    els.approvalOverlay.hidden = true;
    state.currentApproval = null;
  }
```

- `clearTranscript()`(676 行)追加两行(与它已做的 `toolCards = {}` / `subagentStack = []` 并列):

```js
    approvalQueue = [];
    els.approvalOverlay.hidden = true;
```

- `handleMessage` 的 `approval_required` 分支(852 行)改为:

```js
      case 'approval_required':
        approvalQueue.push(msg.tool_call);
        showNextApproval();
        break;
```

- [ ] **Step 7: 运行确认 + 提交**

Run: `uv run pytest tests/test_web_runtime.py tests/test_web_server.py -v && node --check src/harness/web/static/js/app.js`
Expected: PASS

```bash
git add src/harness/web/runtime.py src/harness/web/server.py src/harness/web/static/js/app.js tests/test_web_runtime.py tests/test_web_server.py
git commit -m "approval: correlate decisions by tool_call_id (concurrent approvals)"
```

---

### Task 8: 前端总开关 + Runtime `set_advanced` + CLI 高级模式

**Files:**
- Modify: `src/harness/web/static/index.html`(状态栏开关)
- Modify: `src/harness/web/static/style.css`(`.advanced-toggle` 样式)
- Modify: `src/harness/web/static/js/app.js`(开关事件 + ready/advanced_changed)
- Modify: `src/harness/web/server.py`(`ready` 帧 + `set_advanced` 分支)
- Modify: `src/harness/web/runtime.py`(`_advanced` 状态、`set_advanced`、`_rebuild_subagents`、`_enable_subagents` 传 advanced、run 入口并发 + 预算重置)
- Modify: `src/harness/cli/main.py`(高级模式 + 并发 run + 预算重置)
- Test: `tests/test_web_runtime.py`、`tests/test_web_server.py`

**Interfaces:**
- Consumes: Task 6 的 `add_example_subagents(..., advanced=…)`、Task 2 的 `run_streamed(concurrent=…)`、Task 4 的 `stack.subagent_budget`。
- Produces: `Runtime.advanced: bool` 属性;`async set_advanced(flag) -> bool`(发 `advanced_changed`);WS `{type:"set_advanced", advanced: bool}`;`ready` 帧带 `advanced` + `subagents`。前端状态栏"高级编排"开关,`subagents` 关闭时禁用。

- [ ] **Step 1: 写失败测试**(`tests/test_web_runtime.py` 追加)

```python
async def test_runtime_set_advanced_roundtrip(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="x")], HARNESS_SUBAGENTS="1"
    )
    assert rt.advanced is False
    assert await rt.set_advanced(True) is True
    frames = await _collect_frames(rt, until="advanced_changed")
    assert frames[-1]["advanced"] is True
    assert rt.advanced is True
    # toggling advanced rebuilds the delegate tool set (unregister + register)
    tool = rt.stack.agent.tools.get("delegate_to_researcher")
    assert tool is not None and len(tool._nested_delegates) >= 1
    # idempotent: toggling again does not duplicate tools (register would raise)
    assert await rt.set_advanced(False) is True
    await _collect_frames(rt, until="advanced_changed")
    tool2 = rt.stack.agent.tools.get("delegate_to_researcher")
    assert tool2 is not None and tool2._nested_delegates == ()
    await rt.shutdown()
    await store.close()


async def test_runtime_start_run_resets_budget(make_provider, tmp_path) -> None:
    rt, store = await _make_runtime(
        tmp_path, make_provider, [LLMResponse(final_text="x")], HARNESS_SUBAGENTS="1"
    )
    rt.stack.subagent_budget.record(20)
    assert rt.stack.subagent_budget.remaining() == 20  # 40 - 20
    rt.start_run("go")
    frames = await _collect_frames(rt, until="run_done")
    assert frames[-1]["type"] == "run_done"
    assert rt.stack.subagent_budget.remaining() == 40  # reset at run start
    await rt.shutdown()
    await store.close()
```

`tests/test_web_server.py` 追加:

```python
def test_ws_ready_reports_subagents_and_advanced(tmp_path, make_provider) -> None:
    with _client(tmp_path, make_provider, HARNESS_SUBAGENTS="1") as client:
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["subagents"] is True
            assert ready["advanced"] is False
            ws.send_json({"type": "set_advanced", "advanced": True})
            assert _recv_until(ws, "advanced_changed")[-1]["advanced"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web_runtime.py::test_runtime_set_advanced_roundtrip tests/test_web_server.py::test_ws_ready_reports_subagents_and_advanced -v`
Expected: FAIL(`RuntimeError: 'Runtime' object has no attribute 'advanced'`、ready 帧缺 `advanced`)

- [ ] **Step 3: 实现**

`src/harness/web/runtime.py`:
- `__init__` 加 `self._advanced = settings.subagent_advanced`。
- `_enable_subagents` 改为:

```python
    def _enable_subagents(self) -> None:
        """Register the delegate tools under the current advanced setting.

        Advanced mode re-registers on toggle via ``_rebuild_subagents``; each
        connection starts from ``settings.subagent_advanced`` (the
        ``HARNESS_SUBAGENT_ADVANCED`` env default).
        """
        add_example_subagents(
            self.stack,
            subagent_model=self._settings.subagent_model,
            on_event=self._forward_subagent_event,
            advanced=self._advanced,
        )

    def _rebuild_subagents(self) -> None:
        """Unregister and re-register delegate tools for the new advanced value.

        ``add_example_subagents`` re-attaches the (reversible) delegation
        protocol, so a toggle swaps the protocol variant instead of appending.
        """
        for name in list(self.stack.agent.tools.names()):
            if name.startswith("delegate_to_"):
                self.stack.agent.tools.unregister(name)
        self._enable_subagents()
```

- 加属性与 setter:

```python
    @property
    def advanced(self) -> bool:
        """Whether advanced orchestration (nesting + concurrency) is on."""
        return self._advanced

    async def set_advanced(self, advanced: bool) -> bool:
        """Toggle advanced orchestration; effective from the next run.

        Per-connection and in-memory (like the permission mode). Rebuilds the
        delegate tool set when subagents are enabled.
        """
        self._advanced = bool(advanced)
        if self._settings.subagents:
            self._rebuild_subagents()
        await self._emit({"type": "advanced_changed", "advanced": self._advanced})
        return True
```

- run 入口并发 + 预算重置:
  - `_run_task_coro`:`run_streamed(stack.agent, content, session_id=self._active_session, concurrent=self._advanced)`。
  - `_resume_task_coro`:`resume_streamed(stack.agent, state, session_id=..., concurrent=self._advanced)`。
  - 预算重置已由 Task 4 加在 `start_run`/`start_plan`/`resume`/`resume_checkpoint`。

`src/harness/web/server.py`:
- `ready` 帧加两项:

```python
                    "mode": rt.mode,
                    "advanced": rt.advanced,
                    "subagents": settings.subagents,
                    "max_turns": settings.max_turns,
```

- `_dispatch` 加分支:

```python
    elif mtype == "set_advanced":
        await rt.set_advanced(bool(msg.get("advanced", False)))
```

`src/harness/web/static/index.html`:在 `<select id="mode-select">` 后加:

```html
    <label class="advanced-toggle" title="高级编排模式:嵌套 + 并发委派">
      <input type="checkbox" id="advanced-toggle" disabled>
      <span>高级编排</span>
    </label>
```

`src/harness/web/static/style.css`:

```css
.advanced-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
  user-select: none;
}
.advanced-toggle input { accent-color: var(--accent-dim); cursor: pointer; }
.advanced-toggle:has(input:disabled) { opacity: 0.5; }
```

`src/harness/web/static/js/app.js`:
- `els` 加 `advancedToggle: $('#advanced-toggle')`。
- `handleMessage` `ready` 分支加:

```js
        els.advancedToggle.checked = !!msg.advanced;
        els.advancedToggle.disabled = !msg.subagents;
```

- 加 case:

```js
      case 'advanced_changed':
        els.advancedToggle.checked = !!msg.advanced;
        break;
```

- 底部事件绑定:

```js
  els.advancedToggle.addEventListener('change', function () {
    send({ type: 'set_advanced', advanced: els.advancedToggle.checked });
  });
```

`src/harness/cli/main.py`:
- `if args.subagents:` 分支改为:

```python
    if args.subagents:
        from harness.core.compose import add_example_subagents

        add_example_subagents(
            stack, subagent_model=settings.subagent_model, advanced=settings.subagent_advanced
        )
```

- run 循环:`runner.run_streamed(agent, line, session_id=session_id, concurrent=settings.subagent_advanced)`(预算重置已由 Task 4 加)。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_web_runtime.py tests/test_web_server.py -v && node --check src/harness/web/static/js/app.js`
Expected: PASS(新增 + 全部回归;普通模式帧协议不变)

- [ ] **Step 5: 提交**

```bash
git add src/harness/web/runtime.py src/harness/web/server.py src/harness/web/static/index.html src/harness/web/static/style.css src/harness/web/static/js/app.js src/harness/cli/main.py tests/test_web_runtime.py tests/test_web_server.py
git commit -m "web: advanced-orchestration toggle (set_advanced + tool-set rebuild + CLI mode)"
```

---

### Task 9: 高级模式 e2e + 能力对比脚本

**Files:**
- Create: `scripts/e2e_subagents_advanced_web.py`
- Create: `scripts/e2e_subagents_compare.py`
- Test: 脚本自带断言(需要 `DEEPSEEK_API_KEY`;无 key 时退出码 2)

**Interfaces:**
- Consumes: 真实模型;web WS 协议(`ready`/`set_advanced`/`advanced_changed`/`message`/`approval`/`subagent_*`/`run_done`);`harness.web.server:create_app` 工厂。
- Produces: 两个可独立运行的 `uv run python scripts/e2e_*.py` 脚本;退出码 0/1/2。Task 10 文档引用它们。

- [ ] **Step 1: 写 `scripts/e2e_subagents_advanced_web.py`**

(参照 `scripts/e2e_subagents_web.py` 的骨架 —— `_free_port`/`_wait_health`/uvicorn 启动可整体复用。差异:连上后发 `set_advanced:true`,收到 `advanced_changed` 再发消息;提示词强制两个 `delegate_to_researcher` 调用;断言 ≥2 个**不同 run_id** 的 `subagent_start`、全部 `subagent_end`、`run_done`。)

```python
"""End-to-end test of the advanced-orchestration web run view (real model).

Boots uvicorn with HARNESS_SUBAGENTS=1, connects a websockets client, turns on
the advanced toggle, and runs two scenarios:

1. Parallel: a prompt that delegates to the researcher twice. Asserts the two
   delegated runs stream as DISTINCT-run_id subagent frames and the run
   completes:

       ready -> set_advanced -> advanced_changed -> run_started
           -> subagent_start(run_id A) ... subagent_end(A)
           -> subagent_start(run_id B) ... subagent_end(B)  (distinct run_ids)
           -> run_done

2. Nested (best effort): prompts a two-level delegation and verifies the run_id
   stack brackets depth-first and balances; the observed depth is reported, not
   hard-required (a real model may not choose to nest).

Exit codes:
    0  PASS
    1  FAIL (assertions)
    2  no API key configured
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PROMPT = (
    "Call the delegate_to_researcher tool twice, in one response if possible, "
    "with tasks: (1) 'Summarize what README.md says in three bullets' and "
    "(2) 'Summarize what docs/architecture.md says in three bullets'. "
    "Then, after both return, reply with both WHAT YOU DID lines."
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001 — server may still be booting
            time.sleep(0.5)
    raise RuntimeError("web server did not become healthy in time")


async def _run(port: int) -> None:
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        assert ready["subagents"] is True

        await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
        advanced = json.loads(await ws.recv())
        assert advanced["type"] == "advanced_changed" and advanced["advanced"] is True

        await ws.send(json.dumps({"type": "message", "content": PROMPT}))
        run_ids: set[str] = set()
        starts = ends = 0
        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "approval_required":
                await ws.send(
                    json.dumps(
                        {"type": "approval", "tool_call_id": frame["tool_call"]["id"], "decision": "y"}
                    )
                )
            elif t == "subagent_start":
                starts += 1
                run_ids.add(frame["run_id"])
                print(f"[subagent_start] run={frame['run_id'][:8]} agent={frame['agent']}")
            elif t == "subagent_end":
                ends += 1
                print(f"[subagent_end] run={frame['run_id'][:8]} turns={frame['turns']}")
            elif t == "run_done":
                print(f"[ok] run_done: turns={frame['result']['turns']}")
                break
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")

        assert starts >= 2, f"expected >=2 subagent starts, got {starts}"
        assert ends == starts, f"subagent_end {ends} != subagent_start {starts}"
        assert len(run_ids) == starts, (
            f"run_ids not unique per run: {sorted(run_ids)}"
        )
        print(f"[ok] advanced run: starts={starts} ends={ends} distinct_run_ids={len(run_ids)}")


NESTED_PROMPT = (
    "Delegate to the researcher exactly once with this task: "
    "'Research what src/harness/core/runner.py does, then delegate to doc_writer "
    "with task=\"Write a short summary of it to nested_report.md\". After the "
    "researcher returns, reply with its WHAT YOU DID line."
)


async def _run_nested(port: int) -> None:
    """Best-effort two-level nesting check (spec 10.2 nested scenario).

    A real model may or may not choose to nest — the prompt can only request it.
    So we verify what is guaranteed: the run completes, subagent start/end
    frames bracket depth-first (each end matches the top of the client's run_id
    stack), and the observed depth is reported. The deterministic depth-2 proof
    lives in the unit tests (test_advanced_nested_delegation_two_levels).
    """
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
        assert json.loads(await ws.recv())["type"] == "advanced_changed"

        await ws.send(json.dumps({"type": "message", "content": NESTED_PROMPT}))
        stack: list[str] = []
        max_depth = 0
        starts = 0
        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "approval_required":
                await ws.send(
                    json.dumps(
                        {"type": "approval", "tool_call_id": frame["tool_call"]["id"], "decision": "y"}
                    )
                )
            elif t == "subagent_start":
                stack.append(frame["run_id"])
                max_depth = max(max_depth, len(stack))
                starts += 1
                print(
                    f"[nested] start depth={len(stack)} run={frame['run_id'][:8]} "
                    f"agent={frame['agent']}"
                )
            elif t == "subagent_end":
                # depth-first: the ending run is always the stack top
                assert stack and stack[-1] == frame["run_id"], "subagent_end out of order"
                stack.pop()
                print(f"[nested] end depth={len(stack)} run={frame['run_id'][:8]}")
            elif t == "run_done":
                break
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")
        assert starts >= 1, "no subagent run started"
        assert not stack, f"unbalanced subagent stack: {stack}"
        print(f"[ok] nested: starts={starts} max_depth={max_depth} stack_balanced=True")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping e2e (exit 2)", file=sys.stderr)
        return 2

    port = _free_port()
    env = {**os.environ, "HARNESS_SUBAGENTS": "1"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "harness.web.server:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_health(port)
        asyncio.run(asyncio.wait_for(_run(port), timeout=300.0))
        asyncio.run(asyncio.wait_for(_run_nested(port), timeout=300.0))
        print("E2E SUBAGENTS ADVANCED WEB PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 — report any failure with exit 1
        print(f"E2E SUBAGENTS ADVANCED WEB FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 写 `scripts/e2e_subagents_compare.py`**(能力对比)

```python
"""Capability comparison: normal vs advanced orchestration on one complex task.

Boots a web server with HARNESS_SUBAGENTS=1, runs the SAME auto-checkable task
twice against a real model — once with the advanced toggle off, once on — and
scores each workspace deterministically (report exists / headings / section
lengths / source references). An optional LLM judge adds a 0-10 completeness
score as a secondary signal. Prints a comparison table.

This is a DEMONSTRATION, not a scientific benchmark: n=1 and the model is
nondeterministic. Its job is to show that advanced mode (nesting + concurrency)
tends to produce a more complete result on multi-subtask work.

Exit codes:
    0  PASS (both modes completed; table printed)
    1  FAIL (a mode errored or the scoring could not run)
    2  no API key configured
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MODULES = [
    "src/harness/core/runner.py",
    "src/harness/safety/approver.py",
    "src/harness/agents/registry.py",
]
MIN_SECTION_CHARS = 200


def _prompt(out: str) -> str:
    return (
        "Research the following three modules and write a markdown report: "
        f"{', '.join(MODULES)}. The report must: "
        f"(1) exist at {out}/report.md; "
        "(2) contain the three headings '## Runner', '## Approver', '## Registry' "
        "with a section describing each module; "
        f"(3) each section at least {MIN_SECTION_CHARS} characters; "
        "(4) end with a '## Sources' section listing the three file paths. "
        "Read each file before writing about it."
    )


def _score(out_dir: Path) -> dict[str, int]:
    """Deterministic rubric: report exists / headings / section lengths / sources."""
    report = out_dir / "report.md"
    if not report.exists():
        return {"report": 0, "sections": 0, "length": 0, "sources": 0, "total": 0}
    text = report.read_text(encoding="utf-8", errors="replace")

    score = 20  # report exists
    sections = 0
    for heading in ("## Runner", "## Approver", "## Registry"):
        if heading in text:
            sections += 1
    score += 15 * sections

    length_hits = 0
    lower = text.lower()
    # rough per-section length: chars between the heading and the next heading
    body = re.split(r"^## .*$", text, flags=re.M)[1:]
    for chunk in body:
        if len(chunk.strip()) >= MIN_SECTION_CHARS:
            length_hits += 1
    score += 10 * min(length_hits, 3)

    if all(m in lower for m in ("runner.py", "approver.py", "registry.py")):
        score += 5
    return {
        "report": 20 if report.exists() else 0,
        "sections": sections,
        "length": length_hits,
        "sources": 1 if score % 100 >= 0 and all(m in lower for m in ("runner.py", "approver.py", "registry.py")) else 0,
        "total": score,
    }


async def _judge(out_dir: Path, model: str) -> int:
    """Optional LLM judge: 0-10 completeness against the brief (best effort)."""
    try:
        from harness.core.messages import Message
        from harness.llm.registry import get_provider
        from harness.config import Settings

        report = (out_dir / "report.md").read_text(encoding="utf-8", errors="replace")
        provider = get_provider(Settings.load().replace(model=model))
        resp = await provider.complete(
            [
                Message.system(
                    "You are a strict grader. Rate the following report 0-10 for "
                    "completeness against the brief (three module summaries + a "
                    "Sources section). Reply with a single integer 0-10."
                ),
                Message.user(report[:4000]),
            ]
        )
        m = re.search(r"\b(?:10|[0-9])\b", resp.final_text or "")
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001 — the judge is optional, never fatal
        return -1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError("web server did not become healthy in time")


async def _run_mode(port: int, out: str, advanced: bool) -> None:
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        if advanced:
            await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
            assert json.loads(await ws.recv())["type"] == "advanced_changed"
        await ws.send(json.dumps({"type": "message", "content": _prompt(out)}))
        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "approval_required":
                await ws.send(
                    json.dumps(
                        {"type": "approval", "tool_call_id": frame["tool_call"]["id"], "decision": "y"}
                    )
                )
            elif t == "run_done":
                return
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping compare (exit 2)", file=sys.stderr)
        return 2

    port = _free_port()
    env = {**os.environ, "HARNESS_SUBAGENTS": "1"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "harness.web.server:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    tmp = Path(tempfile.mkdtemp(prefix="harness-compare-"))
    results: dict[str, dict] = {}
    try:
        _wait_health(port)
        asyncio.run(asyncio.wait_for(_run_mode(port, str(tmp / "normal"), False), timeout=300.0))
        asyncio.run(asyncio.wait_for(_run_mode(port, str(tmp / "advanced"), True), timeout=300.0))
        for mode in ("normal", "advanced"):
            out_dir = tmp / mode
            score = _score(out_dir)
            judge = asyncio.run(_judge(out_dir, os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")))
            results[mode] = {"score": score, "judge": judge}
            print(f"[{mode}] rubric={score['total']}/100  judge={judge}/10")
        n, a = results["normal"]["score"]["total"], results["advanced"]["score"]["total"]
        print(f"comparison: normal={n}  advanced={a}  delta={a - n}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"E2E SUBAGENTS COMPARE FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: 静态检查 + 手动冒烟**

Run: `uv run python -c "import ast; ast.parse(open('scripts/e2e_subagents_advanced_web.py').read()); ast.parse(open('scripts/e2e_subagents_compare.py').read())"`
Expected: 语法 OK;再跑一次 `scripts/e2e_subagents_web.py`(既有回归)确认骨架未破坏。

(真实模型跑 `uv run python scripts/e2e_subagents_advanced_web.py`,需 key;不阻塞提交。)

- [ ] **Step 4: 提交**

```bash
git add scripts/e2e_subagents_advanced_web.py scripts/e2e_subagents_compare.py
git commit -m "e2e: advanced-mode web run view + normal-vs-advanced capability comparison"
```

---

### Task 10: 文档

**Files:**
- Modify: `README.md`(Web UI 段落、Subagents 段落)
- Modify: `docs/architecture.md`(orchestrator 描述、Web UI 段落)

- [ ] **Step 1: 更新 `README.md` Subagents 段落**(在 "Web run view" 一行后追加)

```markdown
- **Advanced orchestration**: turn on the status-bar **高级编排** toggle (web)
  or set `HARNESS_SUBAGENT_ADVANCED=1` (CLI) for **nested + concurrent
  delegation** on hard, multi-subtask work. Subagents can hand off to each
  other one more level (structurally capped at depth 2), and multi-tool turns
  run in parallel — results stay in call order and a failing tool never aborts
  its siblings. A per-run **subagent turn budget** (`HARNESS_SUBAGENT_BUDGET`,
  default 40) guards cost: when it is spent, further delegates return an error
  the parent adapts to. Concurrent file access is serialized per path
  (`FileLockExecutor`), so two agents writing the same file never race, and
  concurrent approvals are matched to the right tool call by `tool_call_id`.
  `scripts/e2e_subagents_compare.py` runs one auto-checkable task in both modes
  and prints a rubric + judge comparison — a demonstration that advanced mode
  tends to produce more complete results on multi-subtask work.
```

- [ ] **Step 2: 更新 `README.md` Web UI 段落**(权限模式描述后补一句开关):

在 "Permission modes" 段落后追加:

```markdown
The **高级编排** toggle in the same status bar switches the connection into
advanced subagent orchestration (nesting + concurrency) — see *Subagents*
above. It is per-connection and takes effect from the next message.
```

- [ ] **Step 3: 更新 `docs/architecture.md`**

`agents/orchestrator.py` 注释行改为:

```text
│   └── orchestrator.py  # manager pattern: sub-agents exposed as tools;
│                        #   per-subagent model tiering + run-event sink;
│                        #   advanced mode: depth-2 nesting, concurrent runs,
│                        #   per-run turn budget (the web renders nested runs)
```

`core/` 段加一行:

```text
│   ├── locking.py       # FileLockExecutor: per-path mutual exclusion (asyncio)
```

`Core data flow` 的 executor 链注释改为:

```text
   ApprovalExecutor(SandboxedExecutor(FileLockExecutor(SnapshotExecutor(
       default_executor, sessions)), sandbox), permissions)
```

并在 Web UI 段追加:

```text
- **Advanced orchestration** — a status-bar 高级编排 toggle calls
  `{type:"set_advanced"}`; `Runtime.set_advanced` unregisters and re-registers
  the delegate tools (nesting on/off) and swaps the delegation-protocol
  variant, effective from the next message. Concurrent approvals are matched to
  their tool call by `tool_call_id`; concurrent file writes are serialized per
  path by the executor chain.
```

- [ ] **Step 4: 质量门全绿 + 提交**

Run: `uv run ruff check . && uv run mypy src && uv run pytest -q`
Expected: 全绿(无 API key 也通过;210+ 测试)

```bash
git add README.md docs/architecture.md
git commit -m "docs: advanced orchestration (toggle, budget, file lock, compare script)"
```

---

## 自检记录

**1. Spec 覆盖核对**(对着 `docs/superpowers/specs/2026-08-12-subagents-advanced-design.md` 逐节):
- 2 配置与开关 → Task 1、8 ✓
- 3 结构嵌套深度 2 → Task 6 ✓
- 4 并发委派 → Task 2、6、8 ✓
- 5 审批 tool_call_id 关联 → Task 7 ✓
- 6 run_id 实例标识 → Task 5 ✓
- 7 总预算护栏 → Task 4、6 ✓
- 8 按文件路径互斥 → Task 3 ✓
- 9 错误处理 → Task 2(单失败)、6(预算耗尽)、7(审批超时/取消)✓
- 10.1 单测 → 各任务内 ✓
- 10.2 e2e → Task 9 ✓
- 10.3 能力对比 → Task 9 ✓
- 11 改动点清单 → 全部覆盖 ✓

**2. 占位符扫描**:无 TBD/TODO;每个代码步骤都给出具体实现。

**3. 类型一致性**:
- `on_event` 全程 `Callable[[str, str, object], Awaitable[None]]`(Task 5 定义,Tasks 6/8 沿用)。
- `add_example_subagents(stack, *, subagent_model="", on_event=None, advanced=False)`(Task 6 定义,Task 8 用)。
- `SubagentTool._nested_delegates` / `._concurrent` / `._budget` / `._advanced` 在 Task 6 定义,Task 8 测试直接断言;预算检查用 spec 7.2 双条件 `_advanced and _budget is not None`。
- `WebApprover.approve` / `Runtime.approve` 均为 **async**(与 spec 5 一致);测试 `await approver.approve(...)`,服务端 `_dispatch` 里 `await rt.approve(...)`。
- `run_streamed(concurrent=…)` / `resume_streamed(concurrent=…)` 在 Task 2 定义,Task 6/8 使用。
