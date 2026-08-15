# 上下文压缩机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 harness 加三层上下文压缩——工具输出卸载（>20K token 落盘留引用）、自动摘要（85%×1M 窗口触发）、`compact_conversation` 按需压缩工具——全部默认开启、可配置。

**Architecture:** 卸载是一个「结果处理器」`OffloadExecutor`（由 Runner 按 run 绑定 session_id 的闭包调用，因为 session_id 是 run 参数而非 executor 链属性）；自动摘要是注入 Runner 的 `ContextCompactor`，每 turn 顶部 model call 前调用；按需工具通过共享 `CompactRequest` flag 通知 compactor。两者共用 harness 自有目录 `./harness-context/<session_id>/` 的 `ContextStore`。

**Tech Stack:** Python 3.11+ / asyncio / pytest / SQLite（现有 session store，无需改动 schema）

**Spec:** [docs/superpowers/specs/2026-08-15-context-compression-design.md](docs/superpowers/specs/2026-08-15-context-compression-design.md)（本计划从 spec 论证，执行者两个都读）

## Global Constraints

- `settings` 是 frozen dataclass；新增字段后测试/组合用 `Settings.replace(...)` 或 `from_env` 注入。
- 默认值（用户已确认）：`context_enabled=True`、`context_window=1_000_000`、`context_trigger=0.85`、`context_offload_threshold=20_000`、`context_keep=20`、`context_dir="harness-context"`。
- 卸载/压缩文件是 harness 记账，**不经 sandbox、不经 approval**，本机直接 `Path.write_text`。
- `Message` 保持现有 wire format 不变；卸载只改 `ToolResult.content`（`dataclasses.replace`），压缩只改 runner 持有的 `messages` 列表。
- `_prepare_messages` 的「`messages[0]` 恒为 system 指令」不变量必须保持；压缩后 `messages[0]` 仍是原 system 指令。
- 卸载/压缩逻辑在 `session_id is None` 时静默跳过（不 crash）。
- 压缩若摘要失败必须 fallback（纯截断），**绝不阻塞 turn**。
- spec 偏差（plan 级细化，已在对应 task 注明）：① OffloadExecutor 因 session_id 是 run 参数，改为结果处理器 + runner 闭包绑定（§3/§7 spec 原文的「executor 链最外层」在 runner 侧等价实现）；② 保留最近消息追加 **10% 窗口 token 预算**（`int(window*0.1)`），防止保留的大输出让压缩失效——计数与 token 双上限，取先到者。

---

### Task 1: 配置项 + .gitignore

**Files:**
- Modify: `src/harness/config.py`（新增 6 个字段 + `get_float` helper + `from_env` 映射）
- Modify: `.gitignore`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.context_enabled: bool`, `.context_window: int`, `.context_trigger: float`, `.context_offload_threshold: int`, `.context_keep: int`, `.context_dir: str`。env 名 `HARNESS_CONTEXT_*`。后续 task 从 `settings` 读取这些字段。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 追加（追加到现有测试文件末尾，保持 import 一致——该文件已 import `Settings`）：

```python
def test_context_defaults():
    s = Settings.from_env({})
    assert s.context_enabled is True
    assert s.context_window == 1_000_000
    assert s.context_trigger == 0.85
    assert s.context_offload_threshold == 20_000
    assert s.context_keep == 20
    assert s.context_dir == "harness-context"


def test_context_env_overrides():
    s = Settings.from_env(
        {
            "HARNESS_CONTEXT_ENABLED": "false",
            "HARNESS_CONTEXT_WINDOW": "64000",
            "HARNESS_CONTEXT_TRIGGER": "0.5",
            "HARNESS_CONTEXT_OFFLOAD_THRESHOLD": "5000",
            "HARNESS_CONTEXT_KEEP": "5",
            "HARNESS_CONTEXT_DIR": "tmp/ctx",
        }
    )
    assert s.context_enabled is False
    assert s.context_window == 64_000
    assert s.context_trigger == 0.5
    assert s.context_offload_threshold == 5_000
    assert s.context_keep == 5
    assert s.context_dir == "tmp/ctx"


def test_context_trigger_bad_env_falls_back():
    s = Settings.from_env({"HARNESS_CONTEXT_TRIGGER": "garbage"})
    assert s.context_trigger == 0.85
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL（AttributeError: 'Settings' object has no attribute 'context_enabled'）

- [ ] **Step 3: 实现**

在 `src/harness/config.py` 的 `Settings` dataclass 末尾（`web_search_backend`/`tavily_api_key` 之后、`log_level` 之前或之后均可）加字段：

```python
    # Context compression (offload oversized tool output + auto-summarize)
    context_enabled: bool = True
    context_window: int = 1_000_000
    context_trigger: float = 0.85
    context_offload_threshold: int = 20_000
    context_keep: int = 20
    context_dir: str = "harness-context"
```

`from_env` 里在 `get_int` helper 后加 `get_float`：

```python
        def get_float(name: str, default: float) -> float:
            raw = env.get(name)
            try:
                return float(raw) if raw not in (None, "") else default
            except ValueError:
                return default
```

在 `from_env` 的 `cls(...)` 中追加映射：

```python
            context_enabled=get_bool("HARNESS_CONTEXT_ENABLED", True),
            context_window=get_int("HARNESS_CONTEXT_WINDOW", 1_000_000),
            context_trigger=get_float("HARNESS_CONTEXT_TRIGGER", 0.85),
            context_offload_threshold=get_int("HARNESS_CONTEXT_OFFLOAD_THRESHOLD", 20_000),
            context_keep=get_int("HARNESS_CONTEXT_KEEP", 20),
            context_dir=get("HARNESS_CONTEXT_DIR", default="harness-context"),
```

`.gitignore` 在 `/memory/` 后追加（锚定根目录，与 `/skills/` `/memory/` 同风格）：

```
# Context-compression runtime artifacts (offloaded tool output, transcripts)
/harness-context/
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS（含新增 3 个 + 既有用例）

- [ ] **Step 5: 提交**

```bash
git add src/harness/config.py .gitignore tests/test_config.py
git commit -m "feat(context): config flags for context compression (default-on)"
```

---

### Task 2: ContextStore + token 估算

**Files:**
- Create: `src/harness/context/__init__.py`
- Create: `src/harness/context/store.py`
- Test: `tests/test_context_store.py`

**Interfaces:**
- Consumes: `Message`（`harness.core.messages`）、`Settings.context_dir`（str，Task 1）
- Produces（后续 task 依赖的精确签名）：
  - `harness.context.store.estimate_tokens(text: str) -> int`（默认 `len(text)//4`）
  - `harness.context.store.estimate_message_tokens(messages: list[Message]) -> int`
  - `class ContextStore`:
    - `__init__(self, root: str | Path)`
    - `root: Path` 只读属性
    - `offload(self, session_id: str, tool_call_id: str, content: str) -> Path`
    - `write_transcript(self, session_id: str, turn: int, messages: list[Message]) -> Path`
    - `relpath(self, path: Path) -> str`（返回 root 相对路径，`/` 分隔）
    - `cleanup(self, session_id: str) -> None`

- [ ] **Step 1: 写失败测试**

Create `tests/test_context_store.py`：

```python
"""ContextStore: offload files + compaction transcripts on disk."""

from __future__ import annotations

from harness.context.store import ContextStore, estimate_tokens
from harness.core.messages import Message


def test_estimate_tokens():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 400) == 100


def test_offload_writes_and_relpath(tmp_path):
    store = ContextStore(tmp_path)
    path = store.offload("sess1", "call-abc", "x" * 100)
    assert path.read_text(encoding="utf-8") == "x" * 100
    assert store.relpath(path) == "sess1/offload_call-abc.txt"


def test_write_transcript_jsonl(tmp_path):
    store = ContextStore(tmp_path)
    msgs = [Message.system("hi"), Message.user("yo")]
    path = store.write_transcript("sess1", 3, msgs)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"role": "user"' in lines[1]
    assert store.relpath(path) == "sess1/transcript_3.jsonl"


def test_cleanup_removes_session_dir(tmp_path):
    store = ContextStore(tmp_path)
    store.offload("sess1", "c1", "data")
    store.write_transcript("sess1", 0, [Message.user("x")])
    store.cleanup("sess1")
    assert not (tmp_path / "sess1").exists()
    # 其他 session 不受影响
    store.offload("sess2", "c2", "data2")
    assert (tmp_path / "sess2").exists()


def test_session_id_path_traversal_guarded(tmp_path):
    store = ContextStore(tmp_path)
    store.offload("../evil", "c1", "x")
    assert not (tmp_path.parent / "evil").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_context_store.py -q`
Expected: FAIL（ModuleNotFoundError: No module named 'harness.context'）

- [ ] **Step 3: 实现**

Create `src/harness/context/__init__.py`：

```python
"""Context compression: tool-output offload, auto-summarize, compact tool."""
```

Create `src/harness/context/store.py`：

```python
"""ContextStore: harness-owned storage for offloaded tool output and
compaction transcripts, plus a token estimator.

All artifacts live under ``<context_dir>/<session_id>/`` on the local
filesystem, kept separate from the agent workspace so they never touch the
sandbox, git, or rollback snapshots.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from harness.core.messages import Message


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) used for offload/compaction."""
    return len(text) // 4


def estimate_message_tokens(messages: list[Message]) -> int:
    """Token estimate for a whole message list."""
    return sum(estimate_tokens(m.content or "") for m in messages)


def _safe_session_dir(root: Path, session_id: str) -> Path:
    """Resolve ``<root>/<session_id>``, guarding against path traversal."""
    return root / Path(session_id).name


class ContextStore:
    """Filesystem store for compression artifacts, keyed by session id."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def offload(self, session_id: str, tool_call_id: str, content: str) -> Path:
        """Write the full tool output and return the file path."""
        session_dir = _safe_session_dir(self._root, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"offload_{tool_call_id}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def write_transcript(
        self, session_id: str, turn: int, messages: list[Message]
    ) -> Path:
        """Write a JSONL transcript of the pre-compaction message history."""
        session_dir = _safe_session_dir(self._root, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"transcript_{turn}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for msg in messages:
                fh.write(json.dumps(msg.to_openai_dict(), ensure_ascii=False))
                fh.write("\n")
        return path

    def relpath(self, path: Path) -> str:
        """Path relative to the store root (stable, '/'-separated)."""
        return str(path.relative_to(self._root)).replace("\\", "/")

    def cleanup(self, session_id: str) -> None:
        """Remove every artifact for a session (called on session delete)."""
        session_dir = _safe_session_dir(self._root, session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_context_store.py -q`
Expected: PASS（5 个用例）

- [ ] **Step 5: 提交**

```bash
git add src/harness/context/ tests/test_context_store.py
git commit -m "feat(context): ContextStore + token estimators"
```

---

### Task 3: OffloadExecutor（工具输出卸载）

**Files:**
- Create: `src/harness/context/offload.py`
- Test: `tests/test_offload_executor.py`

**Interfaces:**
- Consumes: `ContextStore`、`estimate_tokens`（Task 2）、`ToolResult`（`harness.tools.base`）、`ToolCall`（`harness.core.messages`）
- Produces（runner 闭包绑定用）：
  - `class OffloadExecutor`:
    - `__init__(self, store: ContextStore, *, threshold: int = 20_000, token_estimator: Callable[[str], int] = estimate_tokens)`
    - `async process(self, session_id: str | None, tool_call: ToolCall, result: ToolResult) -> ToolResult`

**设计说明（spec §6 细化）:** session_id 是 run 参数而非 executor 链属性，所以这不是一个 `ToolExecutor`，而是「结果处理器」：runner 在 `_run_streamed` 里用闭包把 `session_id` 绑进去（Task 5）。`process` 对超大成功结果：全文写 `offload_<tool_call_id>.txt`，content 替换为 `[offloaded to <relpath> — ~N tokens]` + 前 10 行预览，`metadata["offloaded"]=<relpath>`。

- [ ] **Step 1: 写失败测试**

Create `tests/test_offload_executor.py`：

```python
"""OffloadExecutor: oversized tool results go to disk, context gets a reference."""

from __future__ import annotations

import pytest

from harness.context.offload import OffloadExecutor
from harness.context.store import ContextStore
from harness.core.messages import ToolCall
from harness.tools.base import ToolResult


def _call(session_id, result, store, *, threshold=20_000):
    offload = OffloadExecutor(store, threshold=threshold)
    return result


@pytest.mark.asyncio
async def test_offloads_oversized_result(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    big = "line one\n" + "x" * 400
    result = await offload.process(
        "sess1", ToolCall(id="c1", name="bash", arguments="{}"), ToolResult.ok(big)
    )
    # 上下文里是引用而非全文
    assert "x" * 400 not in result.content
    assert "[offloaded to" in result.content
    assert "offload_c1.txt" in result.content
    # 全文落盘
    assert (tmp_path / "sess1" / "offload_c1.txt").read_text(encoding="utf-8") == big
    assert result.metadata["offloaded"] == "sess1/offload_c1.txt"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_small_result_passthrough(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    result = await offload.process(
        "sess1", ToolCall(id="c2", name="bash", arguments="{}"), ToolResult.ok("tiny")
    )
    assert result.content == "tiny"
    assert "offloaded" not in result.metadata
    assert not (tmp_path / "sess1").exists()


@pytest.mark.asyncio
async def test_error_result_not_offloaded(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    result = await offload.process(
        "sess1", ToolCall(id="c3", name="bash", arguments="{}"),
        ToolResult.error("e" * 500),
    )
    assert result.content == "e" * 500
    assert result.is_error is True
    assert not (tmp_path / "sess1").exists()


@pytest.mark.asyncio
async def test_none_session_skips(tmp_path):
    store = ContextStore(tmp_path)
    offload = OffloadExecutor(store, threshold=10)
    result = await offload.process(
        None, ToolCall(id="c4", name="bash", arguments="{}"), ToolResult.ok("x" * 500)
    )
    assert result.content == "x" * 500
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_offload_executor.py -q`
Expected: FAIL（ModuleNotFoundError: harness.context.offload）

- [ ] **Step 3: 实现**

Create `src/harness/context/offload.py`：

```python
"""OffloadExecutor: write oversized tool results to disk and hand the model a
small reference (path + preview) instead of the full output.

Not a :class:`ToolExecutor` — ``session_id`` is a per-run argument, so the
Runner binds it per run (see ``Runner._run_streamed``). ``process`` returns
the (possibly replaced) :class:`ToolResult`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from harness.context.store import ContextStore, estimate_tokens
from harness.core.messages import ToolCall
from harness.tools.base import ToolResult

PREVIEW_LINES = 10
PREVIEW_CHARS = 2_000


def _preview(content: str) -> str:
    head = "\n".join(content.splitlines()[:PREVIEW_LINES])
    if len(head) > PREVIEW_CHARS:
        head = head[:PREVIEW_CHARS] + "…"
    return head


class OffloadExecutor:
    """Post-processes a tool result, offloading oversized output to disk."""

    def __init__(
        self,
        store: ContextStore,
        *,
        threshold: int = 20_000,
        token_estimator: Callable[[str], int] = estimate_tokens,
    ) -> None:
        self._store = store
        self._threshold = threshold
        self._token_estimator = token_estimator

    async def process(
        self, session_id: str | None, tool_call: ToolCall, result: ToolResult
    ) -> ToolResult:
        if session_id is None:
            return result
        if result.is_error:
            return result
        if self._token_estimator(result.content) <= self._threshold:
            return result
        n = self._token_estimator(result.content)
        path = self._store.offload(session_id, tool_call.id, result.content)
        rel = self._store.relpath(path)
        content = f"[offloaded to {rel} — ~{n} tokens]\n{_preview(result.content)}"
        return replace(result, content=content, metadata={**result.metadata, "offloaded": rel})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_offload_executor.py -q`
Expected: PASS（4 个用例）

- [ ] **Step 5: 提交**

```bash
git add src/harness/context/offload.py tests/test_offload_executor.py
git commit -m "feat(context): OffloadExecutor — oversized tool output → disk reference"
```

---

### Task 4: CompactRequest + ContextCompactor + compact 工具

**Files:**
- Create: `src/harness/context/compactor.py`
- Test: `tests/test_context_compactor.py`

**Interfaces:**
- Consumes: `ContextStore`、`estimate_tokens`/`estimate_message_tokens`（Task 2）、`LLMProvider.complete`、`Message`、`Tool`（`harness.tools.base`）
- Produces（Task 5 runner 用）：
  - `class CompactRequest:` `set(self) -> None`、`take(self) -> bool`（原子读取并复位）
  - `@dataclass CompactionResult:` `messages: list[Message]`、`changed: bool`、`transcript_path: str | None = None`、`kept: int = 0`、`freed_tokens: int = 0`
  - `class ContextCompactor`:
    - `__init__(self, store: ContextStore, provider: LLMProvider, *, window: int = 1_000_000, trigger: float = 0.85, keep: int = 20, token_estimator: Callable[[list[Message]], int] = estimate_message_tokens)`
    - `async maybe_compact(self, messages: list[Message], *, session_id: str | None, turn: int) -> CompactionResult`
    - `request_compaction(self) -> None`
  - `make_compact_conversation_tool(request: CompactRequest) -> Tool`

**触发与保留规则:** 触发 = `CompactRequest.take()` 为真 **或**（`len(messages) > keep+1` 且 `estimate_message_tokens(messages) > int(window*trigger)`）。保留 = system + 摘要消息 + 最近消息（从最新往回，同时受 `keep` 条数和 `int(window*0.1)` token 预算限制，**至少保留最新 1 条**）。摘要生成失败/异常 → fallback 截断摘要，不抛异常。

- [ ] **Step 1: 写失败测试**

Create `tests/test_context_compactor.py`：

```python
"""ContextCompactor: auto-summarize at window trigger, on-demand via request."""

from __future__ import annotations

import pytest

from harness.context.compactor import (
    CompactRequest,
    CompactionResult,
    ContextCompactor,
    make_compact_conversation_tool,
)
from harness.context.store import ContextStore
from harness.core.messages import Message
from harness.llm.base import LLMResponse
from harness.tools.base import Tool


class _FakeComplete:
    """Minimal provider: only ``complete`` (compactor needs nothing else)."""

    def __init__(self, script=None, *, raise_on_complete=False):
        self.script = list(script or [])
        self.raise_on_complete = raise_on_complete

    async def complete(self, messages, *, tools=None, model=None):
        if self.raise_on_complete:
            raise RuntimeError("boom")
        resp = self.script.pop(0) if self.script else LLMResponse(final_text="(no script)")
        return resp


def _big_history(n=4, size=500):
    return [Message.system("sys")] + [Message.user("a" * size) for _ in range(n)]


@pytest.mark.asyncio
async def test_trigger_by_size(tmp_path):
    store = ContextStore(tmp_path)
    provider = _FakeComplete(script=[LLMResponse(final_text="summarized.")])
    comp = ContextCompactor(store, provider, window=100, trigger=1.0, keep=2)
    result = await comp.maybe_compact(_big_history(), session_id="s1", turn=0)
    assert result.changed is True
    msgs = result.messages
    assert msgs[0].content == "sys"                      # system 指令保留
    assert "summarized." in msgs[1].content              # 摘要消息
    assert "compacted transcript: s1/transcript_0.jsonl" in msgs[1].content
    assert len(msgs) <= 4                                # system + 摘要 + 保留 ≤ 2
    assert (tmp_path / "s1" / "transcript_0.jsonl").exists()
    assert result.freed_tokens > 0


@pytest.mark.asyncio
async def test_no_trigger_when_small(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(store, _FakeComplete(), window=1_000_000, trigger=0.85, keep=20)
    msgs = [Message.system("sys"), Message.user("hi")]
    result = await comp.maybe_compact(msgs, session_id="s1", turn=0)
    assert result.changed is False
    assert result.messages == msgs


@pytest.mark.asyncio
async def test_trigger_by_request(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(
        store, _FakeComplete(script=[LLMResponse(final_text="summarized.")]),
        window=1_000_000, trigger=0.85, keep=20,
    )
    comp.request_compaction()
    msgs = [Message.system("sys"), Message.user("hi")]
    result = await comp.maybe_compact(msgs, session_id="s1", turn=1)
    assert result.changed is True
    # 请求被消费：再次调用且无请求/小历史 → 不变
    result2 = await comp.maybe_compact(msgs, session_id="s1", turn=2)
    assert result2.changed is False


@pytest.mark.asyncio
async def test_keeps_recent_bounded_by_tokens(tmp_path):
    store = ContextStore(tmp_path)
    provider = _FakeComplete(script=[LLMResponse(final_text="summarized.")])
    comp = ContextCompactor(store, provider, window=100, trigger=1.0, keep=2)
    # 5 条大消息：token 预算 int(100*0.1)=10，任何单条 125 token 都超 → 只留最新 1 条
    msgs = [Message.system("sys")] + [Message.user("b" * 500) for _ in range(5)]
    result = await comp.maybe_compact(msgs, session_id="s1", turn=0)
    assert result.changed is True
    kept = [m for m in result.messages[2:]]
    assert len(kept) == 1


@pytest.mark.asyncio
async def test_fallback_on_provider_error(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(store, _FakeComplete(raise_on_complete=True),
                            window=100, trigger=1.0, keep=2)
    result = await comp.maybe_compact(_big_history(), session_id="s1", turn=0)
    assert result.changed is True          # 压缩仍然发生（fallback 截断）
    assert "History truncated" in result.messages[1].content


@pytest.mark.asyncio
async def test_none_session_skips(tmp_path):
    store = ContextStore(tmp_path)
    comp = ContextCompactor(store, _FakeComplete(), window=100, trigger=1.0, keep=2)
    msgs = _big_history()
    result = await comp.maybe_compact(msgs, session_id=None, turn=0)
    assert result.changed is False
    assert result.messages == msgs


def test_compact_tool_sets_request():
    request = CompactRequest()
    assert request.take() is False
    tool = make_compact_conversation_tool(request)
    assert isinstance(tool, Tool)
    assert tool.name == "compact_conversation"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_context_compactor.py -q`
Expected: FAIL（ModuleNotFoundError: harness.context.compactor）

- [ ] **Step 3: 实现**

Create `src/harness/context/compactor.py`：

```python
"""ContextCompactor: auto-summarize long histories at the window trigger, and
the on-demand ``compact_conversation`` tool (via a shared CompactRequest flag).

The full pre-compaction history is written to a JSONL transcript under the
ContextStore; the summary message embeds its path so it stays recoverable.
Compaction never blocks a turn: a summary provider failure falls back to a
plain truncation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from harness.context.store import (
    ContextStore,
    estimate_message_tokens,
    estimate_tokens,
)
from harness.core.messages import Message
from harness.llm.base import LLMProvider
from harness.tools.base import Tool, tool

SUMMARY_PROMPT = """\
You are compressing a conversation so it can continue without the full history.
Write a concise structured summary covering: 1) session intent, 2) artifacts or
files produced, 3) key facts and decisions, 4) the next step, 5) open or
unresolved items. The summary will replace the entire history below. Keep it
dense and factual.
"""

_RECENT_TOKEN_FRACTION = 0.1


class CompactRequest:
    """One-shot flag: the compact tool sets it; the compactor consumes it."""

    def __init__(self) -> None:
        self.requested = False

    def set(self) -> None:
        self.requested = True

    def take(self) -> bool:
        requested = self.requested
        self.requested = False
        return requested


@dataclass
class CompactionResult:
    messages: list[Message]
    changed: bool
    transcript_path: str | None = None
    kept: int = 0
    freed_tokens: int = 0


def _fallback_summary(messages: list[Message]) -> str:
    head = [f"{m.role}: {m.content}" for m in messages[:10] if m.content]
    return "History truncated for context.\n" + "\n".join(head)


class ContextCompactor:
    def __init__(
        self,
        store: ContextStore,
        provider: LLMProvider,
        *,
        window: int = 1_000_000,
        trigger: float = 0.85,
        keep: int = 20,
        token_estimator: Callable[[list[Message]], int] = estimate_message_tokens,
    ) -> None:
        self._store = store
        self._provider = provider
        self._window = window
        self._trigger = trigger
        self._keep = keep
        self._token_estimator = token_estimator
        self._request = CompactRequest()
        self._recent_budget = int(window * _RECENT_TOKEN_FRACTION)

    def request_compaction(self) -> None:
        self._request.set()

    async def maybe_compact(
        self, messages: list[Message], *, session_id: str | None, turn: int
    ) -> CompactionResult:
        if session_id is None:
            return CompactionResult(messages=messages, changed=False)
        threshold = int(self._window * self._trigger)
        big_enough = len(messages) > self._keep + 1 and self._token_estimator(messages) > threshold
        if not (self._request.take() or big_enough):
            return CompactionResult(messages=messages, changed=False)
        return await self._compact(messages, session_id=session_id, turn=turn)

    # -- internals -- #

    async def _compact(
        self, messages: list[Message], *, session_id: str, turn: int
    ) -> CompactionResult:
        before = self._token_estimator(messages)
        transcript_path = self._store.write_transcript(session_id, turn, messages)
        summary = await self._summarize(messages)
        recent = self._recent(messages)
        summary_msg = Message.system(
            f"{summary}\n\ncompacted transcript: {self._store.relpath(transcript_path)}"
        )
        new_messages = [messages[0], summary_msg, *recent]
        after = self._token_estimator(new_messages)
        return CompactionResult(
            messages=new_messages,
            changed=True,
            transcript_path=self._store.relpath(transcript_path),
            kept=len(recent),
            freed_tokens=before - after,
        )

    def _recent(self, messages: list[Message]) -> list[Message]:
        """Newest ``keep`` messages, bounded by the token budget (≥1 newest)."""
        recent: list[Message] = []
        tokens = 0
        for msg in reversed(messages[1:]):  # skip the system instructions
            if len(recent) >= self._keep:
                break
            t = estimate_tokens(msg.content or "")
            if recent and tokens + t > self._recent_budget:
                break
            recent.append(msg)
            tokens += t
        recent.reverse()
        return recent

    async def _summarize(self, messages: list[Message]) -> str:
        body = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt = f"{SUMMARY_PROMPT}\n\n{body}"
        try:
            resp = await self._provider.complete([Message.user(prompt)])
            text = (resp.final_text or "").strip()
            return text or _fallback_summary(messages)
        except Exception:  # noqa: BLE001 — a summary failure must never block the turn
            return _fallback_summary(messages)


def make_compact_conversation_tool(request: CompactRequest) -> Tool:
    """On-demand compaction tool; sets the request consumed next turn boundary."""

    @tool(
        name="compact_conversation",
        description=(
            "Compress the conversation history now to free context. Call this "
            "when the session feels heavy or you want to reduce context usage."
        ),
    )
    def compact_conversation(reason: str = "") -> str:
        request.set()
        return "OK — the conversation will be compacted before the next model call."

    return compact_conversation
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_context_compactor.py -q`
Expected: PASS（7 个用例）

- [ ] **Step 5: 提交**

```bash
git add src/harness/context/compactor.py tests/test_context_compactor.py
git commit -m "feat(context): ContextCompactor + CompactRequest + compact_conversation tool"
```

---

### Task 5: Runner 集成（CompactionEvent、Hooks 字段、构造参数、turn 边界压缩、每-run 卸载绑定）

**Files:**
- Modify: `src/harness/core/runner.py`
- Modify: `src/harness/core/hooks.py`（加 `on_compacted` 字段——runner 在本 task 里 emit 它，字段必须同 task 落地）
- Test: `tests/test_runner_compaction.py`（新）

**Interfaces:**
- Consumes: `ContextCompactor`、`CompactionResult`（Task 4）、`OffloadExecutor`（Task 3）
- Produces（Task 6/7 依赖）：
  - `harness.core.runner.CompactionEvent`：`@dataclass`，字段 `transcript_path: str`、`kept: int`、`freed_tokens: int`
  - `Runner.__init__` 新增两个关键字参数：`offload_processor: OffloadExecutor | None = None`、`compactor: ContextCompactor | None = None`
  - `Hooks.on_compacted: AsyncHook | None`（emit 时调用 `(transcript_path, kept, freed_tokens)`）
  - 行为：`_run_streamed` 每 turn 顶部压缩；卸载处理器按 run 绑定 session_id

- [ ] **Step 1: 写失败测试**

Create `tests/test_runner_compaction.py`：

```python
"""Runner integration: turn-boundary compaction + per-run offload binding."""

from __future__ import annotations

import pytest

from harness.context.compactor import ContextCompactor
from harness.context.offload import OffloadExecutor
from harness.context.store import ContextStore
from harness.core.agent import Agent
from harness.core.messages import ToolCall
from harness.core.runner import CompactionEvent, Runner
from harness.llm.base import LLMResponse
from harness.tools.base import tool
from harness.tools.registry import ToolRegistry

from tests.conftest import FakeProvider


class _CapturingProvider(FakeProvider):
    """FakeProvider that also records the message list of every stream() call."""

    def __init__(self, script=None):
        super().__init__(script)
        self.seen: list[list] = []

    async def stream(self, messages, *, tools=None, model=None):
        self.seen.append(list(messages))
        async for e in super().stream(messages, tools=tools, model=model):
            yield e


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


def _registry():
    r = ToolRegistry()
    r.register(echo)
    return r


@pytest.mark.asyncio
async def test_compaction_at_turn_boundary(tmp_path):
    ctx = ContextStore(tmp_path / "ctx")
    summary_provider = FakeProvider(script=[LLMResponse(final_text="summarized.")])
    compactor = ContextCompactor(ctx, summary_provider, window=50, trigger=1.0, keep=2)

    main_provider = _CapturingProvider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", arguments='{"text": "x" * 200}')]),
            LLMResponse(final_text="done"),
        ]
    )
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    runner = Runner(main_provider, compactor=compactor)

    events = [e async for e in runner.run_streamed(agent, "hello", session_id="s1")]
    compacted = [e for e in events if isinstance(e, CompactionEvent)]
    assert len(compacted) == 1
    assert compacted[0].transcript_path.startswith("s1/transcript_")
    # turn 1 的 model call 看到压缩后历史（system + 摘要 + ≤keep 条）
    assert len(main_provider.seen) == 2
    assert len(main_provider.seen[1]) <= 4
    assert (tmp_path / "ctx" / "s1").exists()


@pytest.mark.asyncio
async def test_offload_binding_reduces_context(tmp_path):
    ctx = ContextStore(tmp_path / "ctx")
    offload = OffloadExecutor(ctx, threshold=10)
    provider = _CapturingProvider(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", arguments='{"text": "x" * 200}')]),
            LLMResponse(final_text="done"),
        ]
    )
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    runner = Runner(provider, offload_processor=offload)

    events = [e async for e in runner.run_streamed(agent, "hello", session_id="s1")]
    # 第二次 model call 里工具消息是引用而非全文
    assert len(provider.seen) == 2
    tool_msg = next(m for m in provider.seen[1] if m.role == "tool")
    assert "x" * 200 not in tool_msg.content
    assert "[offloaded to" in tool_msg.content
    assert list((tmp_path / "ctx" / "s1").glob("offload_*.txt"))


@pytest.mark.asyncio
async def test_no_context_no_change(tmp_path):
    provider = _CapturingProvider(script=[LLMResponse(final_text="plain")])
    agent = Agent(name="test", instructions="sys", tools=_registry(), max_turns=5)
    runner = Runner(provider)  # 不注入 compactor / offload
    events = [e async for e in runner.run_streamed(agent, "hi", session_id="s1")]
    assert not [e for e in events if isinstance(e, CompactionEvent)]
    assert len(provider.seen) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_runner_compaction.py -q`
Expected: FAIL（`Runner.__init__` 报 TypeError: unexpected keyword argument 'compactor'）

- [ ] **Step 3: 实现**

修改 `src/harness/core/runner.py`：

① 在 `RunDone`/`ToolResultEvent` dataclass 附近加 `CompactionEvent`：

```python
@dataclass
class CompactionEvent:
    """Yielded after a turn-boundary compaction (CLI/web render a notice)."""

    transcript_path: str
    kept: int
    freed_tokens: int
```

② `Runner.__init__` 加两个参数：

```python
    def __init__(
        self,
        provider: LLMProvider,
        *,
        hooks: Hooks | None = None,
        session_store: SessionStore | None = None,
        tool_executor: ToolExecutor | None = None,
        pause_check: PauseCheck | None = None,
        offload_processor: OffloadExecutor | None = None,
        compactor: ContextCompactor | None = None,
    ) -> None:
        self._provider = provider
        self._hooks = hooks or Hooks()
        self._session_store = session_store
        self._tool_executor = tool_executor or default_executor
        self._pause_check = pause_check
        self._offload_processor = offload_processor
        self._compactor = compactor
```

顶部 import 补两行：

```python
from harness.context.compactor import ContextCompactor
from harness.context.offload import OffloadExecutor
```

③ `_run_streamed` 里，在 `tool_schemas = agent.tool_schemas()` 之前绑定每-run 卸载闭包：

```python
        # Per-run offload binding: session_id is a run argument, so the
        # processor is wrapped here (it is not a static chain layer).
        executor: ToolExecutor = self._tool_executor
        if self._offload_processor is not None:
            inner, offload = executor, self._offload_processor

            async def bound_executor(agent: Agent, tc: ToolCall) -> ToolResult:
                return await offload.process(session_id, tc, await inner(agent, tc))

            executor = bound_executor

        tool_schemas = agent.tool_schemas()
```

④ `src/harness/core/hooks.py` 的 `Hooks` dataclass 在 `on_final` 前加字段：

```python
    on_compacted: AsyncHook | None = None
```

⑤ turn 循环顶部（`await self._hooks.emit(self._hooks.on_turn_start, turn, agent)` 之前）插压缩检查：

```python
        for turn in range(start_turn, max_turns):
            if self._compactor is not None:
                compacted = await self._compactor.maybe_compact(
                    messages, session_id=session_id, turn=turn
                )
                if compacted.changed:
                    messages = compacted.messages
                    await self._persist(session_id, messages)
                    await self._hooks.emit(
                        self._hooks.on_compacted,
                        compacted.transcript_path,
                        compacted.kept,
                        compacted.freed_tokens,
                    )
                    yield CompactionEvent(
                        compacted.transcript_path, compacted.kept, compacted.freed_tokens
                    )
            await self._hooks.emit(self._hooks.on_turn_start, turn, agent)
```

⑥ 把循环内两处 `self._tool_executor(...)` 换成 `executor(...)`（顺序路径和并发路径各一处）：

顺序路径：

```python
                    for tool_call in response.tool_calls:
                        await self._hooks.emit(self._hooks.on_tool_call, tool_call, agent)
                        results.append(await executor(agent, tool_call))
```

并发路径：

```python
                    gathered = await asyncio.gather(
                        *(executor(agent, tc) for tc in response.tool_calls),
                        return_exceptions=True,
                    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_runner_compaction.py tests/test_runner.py -q`
Expected: PASS（新增 3 个 + 既有 runner 全部用例；若 test_runner.py 有对 `Runner` 构造位置的断言也要通过——确认无破坏）

- [ ] **Step 5: 提交**

```bash
git add src/harness/core/runner.py tests/test_runner_compaction.py
git commit -m "feat(context): runner integrates turn-boundary compaction + per-run offload"
```

---

### Task 6: 可观测性（Hooks + CLI 渲染 + Web 帧）

**Files:**
- Modify: `src/harness/cli/render.py`
- Modify: `src/harness/web/events.py`
- Test: `tests/test_web_events.py`

**Interfaces:**
- Consumes: `CompactionEvent`（Task 5）
- Produces: CLI 压缩通知行；web 帧 `{"type": "compacted", ...}`；`tool_result` 帧新增 `offloaded` 键

- [ ] **Step 1: 写失败测试**

在 `tests/test_web_events.py` 追加（文件已有 `serialize_event` 相关用例，直接追加以下函数，保持 import 一致）：

```python
def test_compaction_event_serializes():
    from harness.core.runner import CompactionEvent

    frame = serialize_event(CompactionEvent("s1/transcript_0.jsonl", 3, 12_000))
    assert frame["type"] == "compacted"
    assert frame["transcript"] == "s1/transcript_0.jsonl"
    assert frame["kept"] == 3
    assert frame["freed_tokens"] == 12_000


def test_tool_result_offloaded_flag():
    from harness.core.runner import ToolResultEvent
    from harness.core.messages import ToolCall
    from harness.tools.base import ToolResult

    event = ToolResultEvent(
        ToolCall(id="c1", name="bash", arguments="{}"),
        ToolResult.ok("preview", offloaded="s1/offload_c1.txt"),
    )
    frame = serialize_event(event)
    assert frame["type"] == "tool_result"
    assert frame["offloaded"] == "s1/offload_c1.txt"


def test_tool_result_no_offloaded_key_ok():
    from harness.core.runner import ToolResultEvent
    from harness.core.messages import ToolCall
    from harness.tools.base import ToolResult

    event = ToolResultEvent(
        ToolCall(id="c2", name="bash", arguments="{}"), ToolResult.ok("tiny")
    )
    frame = serialize_event(event)
    assert frame["offloaded"] == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_web_events.py -q`
Expected: FAIL（test_compaction_event_serializes: CompactionEvent 已存在但 serialize_event 不识别返回 None；test_tool_result_offloaded_flag: 无 `offloaded` 键）

- [ ] **Step 3: 实现**

`src/harness/cli/render.py` 顶部 import 加 `CompactionEvent`，并在 `render_stream_event` 的 `RunDone` 分支前加：

```python
    elif isinstance(event, CompactionEvent):
        console.print(
            f"\n[bold magenta]⟲ 上下文已压缩[/] 保留最近 {event.kept} 条，"
            f"释放 ~{event.freed_tokens} tokens（transcript: {event.transcript_path}）"
        )
```

`src/harness/web/events.py`：

① `tool_result_to_dict` 返回值加键：

```python
    return {
        "content": content,
        "is_error": result.is_error,
        "truncated": truncated,
        "offloaded": result.metadata.get("offloaded", ""),
    }
```

② `serialize_event` 顶部 import 补 `CompactionEvent`（或函数内从 runner 引入），并在 `ToolResultEvent` 分支后加：

```python
    if isinstance(event, CompactionEvent):
        return {
            "type": "compacted",
            "transcript": event.transcript_path,
            "kept": event.kept,
            "freed_tokens": event.freed_tokens,
        }
```

import 方式：把 `events.py` 第 13 行改成 `from harness.core.runner import CompactionEvent, RunDone, ToolResultEvent`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_web_events.py -q`
Expected: PASS（新增 3 个 + 既有用例）。另跑 `uv run pytest tests/test_cli_stdio.py -q` 确认 CLI 渲染未破坏。

- [ ] **Step 5: 提交**

```bash
git add src/harness/core/hooks.py src/harness/cli/render.py src/harness/web/events.py tests/test_web_events.py
git commit -m "feat(context): on_compacted hook + CLI notice + web compacted/offloaded frames"
```

---

### Task 7: compose 接线 + CoreStack.context_store + web 删除清理

**Files:**
- Modify: `src/harness/core/compose.py`
- Modify: `src/harness/web/server.py`
- Test: `tests/test_compose.py`（追加）

**Interfaces:**
- Consumes: `OffloadExecutor`（Task 3）、`ContextCompactor`/`CompactRequest`/`make_compact_conversation_tool`（Task 4）、`Runner` 新参数（Task 5）、`Settings.context_*`（Task 1）
- Produces: `CoreStack.context_store: ContextStore | None`；`build_core_stack` 在 `context_enabled` 时完成全部接线；`DELETE /api/sessions/{id}` 清理 context 目录

- [ ] **Step 1: 写失败测试**

在 `tests/test_compose.py` 追加（沿用该文件既有模式：`Settings.from_env` + 注入 `provider=_FakeProvider("main")` + `async def`）：

```python
def _ctx_settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "HARNESS_DB_PATH": str(tmp_path / "harness.db"),
            "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),
            "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),
            "HARNESS_CONTEXT_ENABLED": "true",
            "HARNESS_CONTEXT_DIR": str(tmp_path / "ctx"),
        }
    )


@pytest.mark.asyncio
async def test_build_core_stack_wires_context(tmp_path):
    stack = await build_core_stack(_ctx_settings(tmp_path), provider=_FakeProvider("main"))
    assert stack.context_store is not None
    assert "compact_conversation" in stack.agent.tools.names()


@pytest.mark.asyncio
async def test_build_core_stack_disabled_context(tmp_path):
    settings = _ctx_settings(tmp_path).replace(context_enabled=False)
    stack = await build_core_stack(settings, provider=_FakeProvider("main"))
    assert stack.context_store is None
    assert "compact_conversation" not in stack.agent.tools.names()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_compose.py -q`
Expected: FAIL（`CoreStack` 无 `context_store` 属性 / 无 `compact_conversation` 工具）

- [ ] **Step 3: 实现**

`src/harness/core/compose.py`：

① 顶部 import 追加：

```python
from harness.context.compactor import (
    CompactRequest,
    ContextCompactor,
    make_compact_conversation_tool,
)
from harness.context.offload import OffloadExecutor
from harness.context.store import ContextStore
```

② `CoreStack` dataclass 加字段：

```python
    subagent_provider: LLMProvider | None = None  # own key/base_url for subagents
    context_store: ContextStore | None = None    # context-compression artifacts
```

③ `build_core_stack` 里，在 `approval = ApprovalExecutor(...)` 之后、`runner = Runner(...)` 之前加接线：

```python
    # Context compression: offload oversized tool output, auto-summarize long
    # histories, and expose the on-demand compact tool (all default-on).
    context_store: ContextStore | None = None
    offload_processor: OffloadExecutor | None = None
    compactor: ContextCompactor | None = None
    if settings.context_enabled:
        context_store = ContextStore(Path(settings.context_dir))
        offload_processor = OffloadExecutor(
            context_store, threshold=settings.context_offload_threshold
        )
        request = CompactRequest()
        compactor = ContextCompactor(
            context_store,
            provider,
            window=settings.context_window,
            trigger=settings.context_trigger,
            keep=settings.context_keep,
        )
        agent.tools.register(make_compact_conversation_tool(request))
```

④ `Runner(...)` 调用加两个参数，`CoreStack(...)` 返回加 `context_store=context_store`：

```python
    runner = Runner(
        provider,
        session_store=store.sessions,
        tool_executor=approval,
        pause_check=pause_check,
        hooks=hooks,
        offload_processor=offload_processor,
        compactor=compactor,
    )
    ...
    return CoreStack(
        store=store,
        provider=provider,
        agent=agent,
        skill_registry=skill_registry,
        permissions=permissions,
        sandbox=sandbox,
        sandboxed=sandboxed,
        approval=approval,
        runner=runner,
        planner=planner,
        subagent_budget=SubagentBudget(settings.subagent_budget),
        subagent_provider=subagent_provider,
        context_store=context_store,
    )
```

`src/harness/web/server.py` 的 `DELETE /api/sessions/{session_id}` handler（第 107-110 行）：

```python
    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        await app.state.store.sessions.delete_session(session_id)
        ctx: ContextStore | None = getattr(app.state.read_ctx, "context_store", None)
        if ctx is not None:
            ctx.cleanup(session_id)
        return {"ok": True}
```

顶部 import 加 `from harness.context.store import ContextStore`（若触发循环 import 则移到函数内 import）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_compose.py tests/test_web_server.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/harness/core/compose.py src/harness/web/server.py tests/test_compose.py
git commit -m "feat(context): wire compression stack in build_core_stack + web session-delete cleanup"
```

---

### Task 8: 全量回归

**Files:**
- 无新增；跑全量测试 + 抽查 e2e 基准

- [ ] **Step 1: 全量测试**

Run: `uv run pytest -q`
Expected: 全绿（新增 `tests/test_context_store.py`、`test_offload_executor.py`、`test_context_compactor.py`、`test_runner_compaction.py` 均通过；既有用例无回归）

- [ ] **Step 2: 静态检查**

Run: `uv run ruff check src/harness/context tests/test_context_store.py tests/test_offload_executor.py tests/test_context_compactor.py tests/test_runner_compaction.py`
Expected: 无 error（`uv run ruff check .` 若基线干净则全量跑）
Run: `uv run mypy src/harness/context`
Expected: 无 error

- [ ] **Step 3: 真实模型冒烟（可选，有 key 时）**

Run: `uv run harness chat --session smoke-ctx` 输入「先跑 `python -c "print('a'*20000)"` 再用 read_file 读回去」，观察：bash 大输出在上下文里显示为 `[offloaded to …]` 引用而非全文。

- [ ] **Step 4: 基准防回归（可选，慢）**

Run: `uv run python scripts/e2e_token_economy.py`
Expected: 质量门禁不降级（与上轮结果可比；context 默认开，确认卸载不误伤小输出）。

- [ ] **Step 5: 提交（如有临时改动）**

```bash
git status --short   # 确认无遗漏的临时文件
git commit -am "test(context): full regression passes"   # 仅当有遗留改动
```
