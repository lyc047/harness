# 上下文压缩机制 — 设计

> 日期：2026-08-15
> 场景：给 harness 加三层上下文压缩（工具输出卸载 / 自动摘要 / 按需压缩工具），对齐 langchain deepagents 的 context engineering 思路
> 复现入口：`harness chat`（默认开启）· 单测 · 复用 `scripts/e2e_*.py` 基准防回归
> 前置：主模型 `deepseek-v4-pro`，官方上下文窗口 **1M token**（`deepseek-v4-flash` 同为 1M）

## 1. 背景与目标

harness 目前**没有任何上下文压缩机制**（已全库核实）：token 记账（`usage_log`）存在但只有基准在读它，超大工具输出原样进上下文，长会话消息列表无限增长。基准显示 advanced 编排已省 pro token，但上下文占用仍有失控风险。

本轮引入三层压缩，全部默认开启、可配置：

1. **工具输出卸载（offload）** — 单次工具结果超阈值时，全文落盘，上下文只留「路径 + 预览」。省 token 的主力。
2. **自动摘要（auto-summarize）** — 上下文逼近模型窗口时，LLM 把历史压缩成摘要，原文落盘。超长会话才触发。
3. **按需压缩工具（`compact_conversation`）** — 代理主动请求压缩，不依赖窗口阈值。手动逃生阀。

窗口 1M 时 85% 阈值 = 850K token，普通会话几乎到不了 —— 所以 **offload 是主力、按需工具是关键逃生阀、自动摘要是兜底**。

## 2. 成功判据

- 超大工具输出进入上下文前被替换为引用，后续 model call 的上下文显著缩小（有单测断言消息列表内容为引用而非全文）。
- 长会话（模拟跨阈值）后消息列表保持有界（≈ system + 摘要 + 最近 N 条）。
- `compact_conversation` 工具调用后，下一轮 model call 前上下文被压缩。
- 压缩全程可追溯：原文/全文都在 `harness-context/` 下，摘要消息内嵌 transcript 路径。
- 默认开启不破坏既有行为：全部现有测试通过；`scripts/e2e_token_economy.py` 等基准质量门禁不降级。

## 3. 架构决策

| 子功能 | 结构 | 理由 |
|---|---|---|
| 卸载 | 新 executor 层 `OffloadExecutor` 包在审批链**最外层** | 复用现有组合模式（Approval→Sandbox→FileLock→Snapshot），看到所有工具（含 bash）的最终结果 |
| 自动摘要 | 新 `ContextCompactor` 注入 Runner（`compactor` 参数），turn 边界 model call **前**调用 | 与 `pause_check`/`tool_executor` 注入风格一致；compactor 可独立单测 |
| 按需工具 | 共享 `CompactRequest` flag；工具置位，Runner 每 turn 检查 | 工具是无状态单发（Tool.invoke），不能直接改 Runner 局部 `messages`，flag 是唯一干净的信号通道 |
| 存储 | 新 `ContextStore`（harness 自有目录 `./harness-context/`） | 与用户工作区分离、不进 git、不经过 sandbox、清理/生命周期好管 |

已否决：纯 middleware 包 `provider.stream`（中间件需访问 session store、消息可变性差、违背「Runner 无状态」原则）；全塞 Runner（膨胀、违反单一职责）。

## 4. 数据存储 — `ContextStore`（新 `src/harness/context/store.py`）

目录布局（`context_dir` 默认 `./harness-context/`，相对 cwd，同 `skills_dir` 惯例）：

```
harness-context/
└── <session_id>/
    ├── offload_<tool_call_id>.txt  # 卸载的完整工具输出（tool_call_id 天然唯一）
    └── transcript_<turn>.jsonl     # 压缩前的完整消息历史
```

API：

```python
class ContextStore:
    def __init__(self, root: Path) -> None: ...
    def offload(self, session_id: str, tool_call_id: str, content: str) -> Path  # 写 offload_<id>.txt
    def write_transcript(self, session_id: str, turn: int, messages: list[Message]) -> Path
    def cleanup(self, session_id: str) -> None                           # 删整个 session 子目录
    def relpath(self, path: Path) -> str                                 # 嵌入消息的相对路径，稳定
```

文件名用 **`tool_call_id`**（模型生成的全局唯一 id）而非消息序号 —— `OffloadExecutor` 从 `ToolCall.id` 直接拿到，无需 runner 传入任何游标，天然防重。

要点：

- 文件内容是 harness 记账，**不经 sandbox、不经 approval**，本机直接 `Path.write_text`（SSH 沙箱模式下也成立，harness 进程跑在本地）。
- session 删除时挂 `cleanup`（`web/server.py` 与 CLI 的删除路径）。
- 相对路径写入消息内容（`offload_<idx>.txt`），跨重启稳定，代理可用 `read_file` 自行读取。

## 5. 配置项（`config.py` 新增，`Settings` frozen dataclass + env）

| 字段 | env | 默认 | 说明 |
|---|---|---|---|
| `context_enabled` | `HARNESS_CONTEXT_ENABLED` | `True` | 总开关（默认开，已确认） |
| `context_window` | `HARNESS_CONTEXT_WINDOW` | `1_000_000` | DeepSeek V4 官方窗口 |
| `context_trigger` | `HARNESS_CONTEXT_TRIGGER` | `0.85` | 摘要触发比例 |
| `context_offload_threshold` | `HARNESS_CONTEXT_OFFLOAD_THRESHOLD` | `20_000` | 卸载 token 阈值（对齐 deepagents） |
| `context_keep` | `HARNESS_CONTEXT_KEEP` | `20` | 压缩后保留最近消息数（已确认） |
| `context_dir` | `HARNESS_CONTEXT_DIR` | `harness-context` | 存储根目录 |

`.gitignore` 追加 `harness-context/`。

## 6. 工具输出卸载 — `OffloadExecutor`（新 `src/harness/context/offload.py`）

```python
class OffloadExecutor:
    def __init__(self, inner: ToolExecutor, store: ContextStore,
                 threshold: int, estimate_tokens: Callable[[str], int] = ...) -> None:
        ...
    async def __call__(self, agent: Agent, tool_call: ToolCall) -> ToolResult:
        result = await self._inner(agent, tool_call)
        if result.is_error:                       # 错误不卸载（通常小，且原文要喂给模型）
            return result
        if estimate_tokens(result.content) <= self._threshold:
            return result
        path = self._store.offload(session_id, tool_call.id, result.content)  # 完整原文落盘
        preview = first_10_lines(result.content)
        content = f"[offloaded to {relpath} — ~{n} tokens]\n{preview}"
        return replace(result, content=content,
                       metadata={**result.metadata, "offloaded": str(path)})
```

- 在 `compose.py` 里 `OffloadExecutor(approval, ...)` 包住审批后的执行器，Runner 的 `tool_executor` 指向它。
- `tool_call.id` 由模型生成、每个调用唯一，无需 runner 传游标。
- 预览固定前 10 行（或前 ~2000 字符，取先到者），保证引用本身永不再超阈值。
- 卸载只影响「模型看到的上下文」；`ToolResultEvent` 仍带着原 content 渲染给 CLI/web 卡片（可选：web 卡片对 `offloaded` metadata 加样式）。

## 7. 自动摘要 — `ContextCompactor`（新 `src/harness/context/compactor.py`）

```python
class CompactRequest:
    def __init__(self) -> None: self.requested = False
    def set(self) -> None: self.requested = True
    def take(self) -> bool:  # 原子读取并复位
        v, self.requested = self.requested, False
        return v

class ContextCompactor:
    def __init__(self, store: ContextStore, provider: LLMProvider, *,
                 window: int, trigger: float, keep: int,
                 estimate_tokens: Callable[[list[Message]], int] = ...) -> None: ...
    async def maybe_compact(self, messages: list[Message], *,
                            session_id: str, turn: int) -> tuple[list[Message], bool]:
        if not (self._request.take() or estimate_tokens(messages) > int(window * trigger)):
            return messages, False
        return await self._compact(messages, session_id=session_id, turn=turn)
```

触发：

- **大小信号**：默认 `estimate_tokens = sum(len(m.content or "") // 4 ...)`（字符估算，可插拔 callable）。窗口由配置定，触发 = `> window × trigger`。
- **显式信号**：`CompactRequest` flag 已置位（按需工具设置）。

压缩动作：

1. 原文落盘：`store.write_transcript(session_id, turn, messages)`。
2. 生成摘要：`provider.complete(SUMMARY_PROMPT + 序列化消息)` → 结构化摘要（会话意图 / 已产出的产物 / 下一步 / 未决项）。`complete` 失败或超时 → fallback 为纯截断摘要（取前若干条），**绝不让压缩阻塞 turn**。
3. 重建消息：`[messages[0]（system 指令不变）, 摘要消息（role=system，内容=摘要 + "\ncompacted transcript: <path>"）, *最近 keep 条]`。
4. 返回 `(new_messages, True)`；Runner 负责 `_persist` + 发 hook。

Runner 改动（`src/harness/core/runner.py`）：

- `__init__` 加 `compactor: ContextCompactor | None = None`。
- `_run_streamed` 每 turn 顶部（model call 前）：`if self._compactor is not None: messages, changed = await self._compactor.maybe_compact(messages, session_id=..., turn=turn); if changed: await self._persist(...); emit on_compacted; yield CompactionEvent`。
- 卸载在 `OffloadExecutor` 内部完成（用 `tool_call.id`），runner 无需感知。
- 暂停/resume：`RunState.messages` 是压缩后的列表，resume 从压缩后继续，一致。

## 8. 按需工具 — `compact_conversation`（`src/harness/context/compactor.py` 或 `tools/`）

```python
def make_compact_conversation_tool(request: CompactRequest) -> Tool:
    @tool(name="compact_conversation",
          description="Compress the conversation history now (frees context). Call when the session feels heavy.")
    def compact_conversation(reason: str = "") -> str:
        request.set()
        return "Conversation will be compacted before the next model call."
```

- compose.py 里 `context_enabled` 时注册到 `agent.tools`。
- `CompactRequest` 由 compose 构建、同时注入 compactor 与工具（同一个实例）。

## 9. 组合接线（`src/harness/core/compose.py`）

```
context_store = ContextStore(Path(settings.context_dir)) if settings.context_enabled else None
approval = ApprovalExecutor(sandboxed, permissions, ...)
if context_enabled:
    request = CompactRequest()
    approval = OffloadExecutor(approval, context_store, threshold, ...)
    compactor = ContextCompactor(context_store, provider, window=..., trigger=..., keep=...)
    agent.tools.register(make_compact_conversation_tool(request))
else:
    compactor, request = None, None
runner = Runner(provider, session_store=store.sessions, tool_executor=approval,
                pause_check=..., hooks=..., compactor=compactor)
```

- `CoreStack` 加 `context_store` 字段（供 web/CLI 清理与展示）。
- `build_core_stack` 已有 `store` 测试注入 seam；`context_dir` 允许测试传临时目录（新增可覆盖参数或读 settings）。

## 10. 可观测性 / Web / CLI

- `Hooks` 加 `on_compacted: AsyncHook | None`（`(summary_path, kept_messages, freed_tokens_est)`）。Runner 压缩后 emit；CLI 打印一行 `上下文已压缩 (transcript: …)`；web 发 `compacted` frame。
- web `events.py::serialize_event` 加 `compacted` 帧；前端显示一条提示气泡。
- 卸载的 tool 消息内容本身是引用文本 —— 现有工具卡自然渲染「路径+预览」；`metadata["offloaded"]` 让前端（可选）加「已卸载」徽标。
- tracing：`tool_result` 事件带 `offloaded: true`（可选增强）。

## 11. 已知限制与兼容性

| 项 | 处理 |
|---|---|
| 回退跨压缩边界 | 压缩后 DB 只有摘要消息，pre-compaction 原文在 transcript 文件；v1 回退限于压缩后 epoch，跨边界重载留 follow-up（摘要消息已嵌 transcript 路径，具备重载条件） |
| 卸载后 DB 存截断版 | 全文在 offload 文件，可追溯（对齐 deepagents 的 evidence artifact 模式） |
| 子代理隔离 run | v1 不做压缩（其消息列表不绑定 session）；仅主 run 生效 |
| 分支 | branch 复制 DB 消息（压缩后视图），一致 |
| 暂停/resume | RunState 持压缩后列表，一致 |
| DeepSeek 系统消息位置 | 摘要消息插在 `messages[0]`（指令）之后，`_prepare_messages` 的「system 打头」不变量保持 |
| 摘要失败 | fallback 纯截断，压缩不阻塞 turn |

## 12. 测试

- **ContextStore**：offload/write_transcript/cleanup 写读删；relpath 稳定；根外路径不泄露。
- **OffloadExecutor**：超阈值卸载+引用格式；不超阈值透传；error 透传不卸载；`is_error`/`metadata` 保留。
- **ContextCompactor**：大小触发/不触发；flag 触发；`take()` 原子复位；保留 system + 摘要 + 最近 keep 条；transcript 落盘；provider 失败 fallback 截断。
- **Runner 集成**（fake provider）：卸载后模型收到的工具消息是引用；跨阈值后消息列表有界；`compact_conversation` 工具置 flag → 下一轮压缩；压缩后 `_persist` 落库。
- **回归**：`uv run pytest -q` 全绿；复跑 `scripts/e2e_token_economy.py` 质量门禁不降级（context 默认开，确认卸载不误伤小输出）。

## 13. 实施顺序（供 writing-plans 细化）

1. `config.py` 配置项 + `.gitignore`
2. `ContextStore` + 单测
3. `OffloadExecutor` + 单测 + compose 接线
4. `CompactRequest` / `ContextCompactor` + 单测 + Runner 注入 + compose 接线
5. `compact_conversation` 工具注册
6. `Hooks.on_compacted` + CLI 打印 + web `compacted` frame
7. session 删除挂 `cleanup`；回归跑基准
