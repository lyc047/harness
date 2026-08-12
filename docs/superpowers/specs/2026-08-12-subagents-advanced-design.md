# 子智能体高级编排模式(嵌套 + 并发 + 前端开关)— 设计文档

日期:2026-08-12
状态:已获用户逐节确认

## 1. 背景与目标

现有子智能体是**单层、顺序**委派:父 agent 调用 `delegate_to_<name>` 时,子 agent 跑自己的轮次,返回结构化结果。对"单一明确子任务"够用,但对**需要分工协作、并行调研多个独立主题**的困难任务,单层顺序模式会低效甚至做不完。

用户要求:实现**嵌套委派**与**并发委派**,并在前端加一个**总开关**,由用户自行决定是否启用这个"更复杂但处理困难任务更有效"的模式。

两个已确认的设计决策:

- **开关粒度 = 一个总开关**:"高级编排模式"。
- **成本护栏 = 加总预算护栏**。

本设计不改变现有单层模式的行为(开关关闭时与现在完全一致)。

## 2. 总开关与配置

### 2.1 配置项(`config.py`)

```python
# Multi-agent orchestration
subagents: bool = False
subagent_model: str = ""
subagent_advanced: bool = False   # 高级编排模式(嵌套+并发);默认关
subagent_budget: int = 40         # 每次 run 的子 agent 累计 turn 预算
```

`from_env` 对应:

- `get_bool("HARNESS_SUBAGENT_ADVANCED", False)`
- `get_int("HARNESS_SUBAGENT_BUDGET", 40)`

CLI 端 `--subagents` 不变;高级模式主要面向 web(开关在浏览器里),但也支持 `HARNESS_SUBAGENT_ADVANCED=1` 环境变量让 CLI 直接以高级模式启动。

### 2.2 前端总开关

状态栏在权限模式切换器旁新增"高级编排"开关,复用现有 mode-switcher 模式:

```js
// #advanced-toggle change →
send({type:'set_advanced', advanced: els.advancedToggle.checked});
```

WS 协议:

- 客户端 → 服务端:`{type:"set_advanced", advanced: bool}`
- 服务端 → 客户端:`{type:"advanced_changed", advanced: bool}`(同步开关状态)
- `ready` 帧新增 `advanced` 字段(重连后恢复 UI 状态)

`Runtime` 新增 `advanced` 属性 + `set_advanced(bool)`。开关**每连接独立、内存态**(与权限模式一致,重连重置),**从下一次消息开始生效**(正在跑的 run 保持当前配置)。

### 2.3 开关如何生效(工具集随开关变化)

嵌套需要 level-1 子 agent 具备委托工具,普通模式不需要。`ToolRegistry.unregister` 已存在,所以开关可以中途切换:

`set_advanced(flag)` → 若 `settings.subagents` 开启,调用 `_rebuild_subagents()`:

1. `unregister` 全部 `delegate_to_*` 工具;
2. 按新 `advanced` 值重新 `add_example_subagents(stack, advanced=flag, ...)`。

开关只在 `settings.subagents` 为真时有实际效果;子 agent 未启用时,开关仅存状态、无工具可重建。

**协议文本可逆替换**:`add_example_subagents` 内部的 `attach_delegation_protocol` 目前是追加式,重建会重复。改为 `attach_delegation_protocol(agent, advanced=False)`,普通/高级两种协议文本是模块级常量,调用时**先移除已追加的任一变体再追加当前变体**(精确匹配常量,可测),保证 `_rebuild_subagents` 幂等、切换时协议随之换版。

## 3. 结构嵌套(深度上限 2)

### 3.1 结构定义

- **level 0**(父 agent):持有全部 `delegate_to_<name>` 工具。
- **level 1**(子 agent):高级模式下,其 agent 也持有委托工具(除自己外的其他子 agent),可再委派。
- **level 2**(孙 agent):**不带任何委托工具** —— 结构上封顶,天然无环。

这是**结构深度上限**,由工具注册决定,不依赖模型自觉;任何调用路径最长只有两层委派。

### 3.2 代码变化

`SubagentTool.__init__(..., nested_delegates: tuple[Tool, ...] = ())`:

```python
async def invoke(self, **kwargs):
    agent = self.subagent.as_agent(model=self._model, extra_tools=self._nested_delegates)
    ...
```

`Subagent.as_agent(model, extra_tools=())`:materialize 时若给了 `extra_tools`,注册到**自身 registry 的副本**上(绝不改动共享的 `Subagent.tools` 本体,避免跨子 agent 泄漏/重复注册)。

`add_subagents(..., advanced: bool = False)`:

- `advanced=False`(现状):每个子 agent 的 `nested_delegates=()`。
- `advanced=True`:每个子 agent 的 `nested_delegates` = 除自己外的全部其他子 agent 的委托工具。

`attach_delegation_protocol` 在高级模式下追加一段提示:"你可以把子任务再委派给其他子 agent(深度最多两层);孙 agent 没有进一步委派能力,所以任务要拆到可执行为止。"

## 4. 并发委派

### 4.1 Runner 侧

`Runner.run_streamed(..., concurrent: bool = False)`。当前 `_run_streamed` 对同一 turn 的多个 tool_call **顺序** `await`。并发时改为:

```python
# 先发全部 on_tool_call 钩子
for tc in response.tool_calls:
    await self._hooks.emit(self._hooks.on_tool_call, tc, agent)

# 并行执行,保序归位
results = await asyncio.gather(
    *(self._tool_executor(agent, tc) for tc in response.tool_calls),
    return_exceptions=True,
)
for tc, result in zip(response.tool_calls, results):
    if isinstance(result, BaseException):
        result = ToolResult.error(f"{type(result).__name__}: {result}")
    await self._hooks.emit(self._hooks.on_tool_result, tc, result, agent)
    tool_messages.append(Message.tool(tc.id, result.content, name=tc.name))
    yield ToolResultEvent(tc, result)
```

- `return_exceptions=True`:单个工具失败**不影响**同 turn 其他工具;失败映射成 `ToolResult.error`。
- 结果按调用顺序归位,`tool_messages` 顺序不变(模型看到的结果顺序稳定)。
- `concurrent=False`(默认)走原顺序路径,行为不变。

### 4.2 穿透到子 agent

`SubagentTool.__init__(..., concurrent: bool = False)`,invoke 时 `run_streamed(agent, brief, session_id=None, concurrent=self._concurrent)`。高级模式下父 run 和 level-1 子 run 都以并发方式执行自己的多工具回合。

## 5. 审批协议 tool_call_id 关联

现状:`WebApprover` 把所有审批决策塞进同一个 `decisions` 队列,`prompt()` 无差别取队首 —— **并发下两个审批会互相错配**。

改动:`WebApprover` 按 `tool_call.id` 关联等待:

```python
class WebApprover:
    def __init__(...): self._pending: dict[str, asyncio.Future[str]] = {}

    async def prompt(self, tool_call):
        fut = asyncio.get_event_loop().create_future()
        self._pending[tool_call.id] = fut
        await self._outbox.put(json.dumps({... "tool_call": {"id": tool_call.id, ...}}))
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except TimeoutError:
            return "n"          # 超时 fail-closed
        finally:
            self._pending.pop(tool_call.id, None)

    async def approve(self, tool_call_id, decision):
        fut = self._pending.pop(tool_call_id, None)
        if fut is not None and not fut.done():
            fut.set_result(decision)   # 未知 id => 过期决策,丢弃

    def drain(self):
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
```

- 单线程 asyncio,get/set/pop 无竞态。
- 协议变化:
  - 服务端 WS 循环:`elif mtype == "approval": await rt.approve(msg.get("tool_call_id", ""), str(msg.get("decision", "n")))`(`decisions.put_nowait` 移除)。
  - 前端:审批对话框把 `approval_required.tool_call.id` **原样回传**给 `{type:"approval", tool_call_id, decision}`。多个并发审批到来时,前端排队显示,逐条作答,各自配对。
- CLI 交互式审批(`_make_approval_prompt`)逐调用阻塞于 stdin,无并发,无需改动;**文档标注**:CLI 并发模式下多个审批会依次占用同一 stdin,属已知 UX 限制(高级模式的主战场是 web)。

## 6. run_id 实例标识

现状:`on_event(agent, event)`,前端 `subagentStack` 是**单栈**,同名子 agent 并发或嵌套会串卡片。

改动:`SubagentTool.invoke` 每次调用生成一个 `run_id`(uuid4 hex 短码),`on_event` 签名变为 `(run_id, agent, event)`。web 帧携带 `run_id`:

```python
async def _forward_subagent_event(self, run_id, agent, event):
    if isinstance(event, SubagentRunStart):
        await self._emit({"type": "subagent_start", "run_id": run_id, "agent": agent})
    elif isinstance(event, SubagentRunEnd):
        await self._emit({"type": "subagent_end", "run_id": run_id, "agent": agent, ...})
    else:
        frame = serialize_event(event)
        if frame is not None:
            await self._emit({"type": "subagent_event", "run_id": run_id, "agent": agent, "event": frame})
```

前端 `subagentStack` 改为**以 run_id 为键**:`subagent_start` 压入、`subagent_end` 弹出,事件按 run_id 路由到对应卡片。嵌套流是深度优先的 —— level-2 的 run_id 在 level-1 之上压栈/弹栈,天然正确;同名并发子 agent 各得一张卡片。

## 7. 总预算护栏

### 7.1 结构

`SubagentBudget`(放 `agents/orchestrator.py`):

```python
class SubagentBudget:
    """Per-run budget of subagent turns, shared across nesting levels."""
    def __init__(self, total: int): self._total = total; self._used = 0
    def remaining(self) -> int: return self._total - self._used
    def record(self, turns: int) -> None: self._used += turns
    def reset(self) -> None: self._used = 0
```

- **挂在 `CoreStack` 上**:`build_core_stack` 用 `settings.subagent_budget` 创建,`add_example_subagents` 从 `stack.subagent_budget` 取用。CLI 与 web 共用同一来源。
- **每次 run 开始时重置**:web 在 `start_run` / `start_plan` / `resume` / `resume_checkpoint` 里 `stack.subagent_budget.reset()`;CLI 在每次用户输入前重置。若某入口漏重置,行为是累积计数(文档标注,单测覆盖)。

### 7.2 执行

`SubagentTool.invoke` 开头(仅高级模式启用检查):

```python
if self._advanced and self._budget is not None and self._budget.remaining() <= 0:
    return ToolResult.error("subagent budget exhausted", agent=self.subagent.name)
```

run 结束后 `self._budget.record(turns)`。**耗尽不中断父 run**:新委派直接返回错误 ToolResult,父 agent 看到错误文本、自行调整策略。

- asyncio 单线程,`record`/`remaining` 无竞态;并发下各子 agent 的计数累加正确。
- 普通模式(开关关)不检查预算 —— 行为与现状完全一致。

## 8. 按文件路径互斥(FileLockExecutor)

### 8.1 目标

并发下多个 agent 可能同时读写同一文件:写写串行、读写互斥 —— "一个文件只设一个入口,某 agent 写入时其他 agent 等待",并且快照与写入同临界区,保证并发下 rollback 快照不抓错状态。

### 8.2 实现

新模块 `src/harness/core/locking.py`:

```python
_LOCKS: dict[str, asyncio.Lock] = {}   # 进程级,按解析后的绝对路径

def _path_lock(path: str) -> asyncio.Lock:
    key = str(Path(path).resolve())
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock

class FileLockExecutor:
    """Serialize per-path file access under the asyncio lock registry."""
    def __init__(self, inner: ToolExecutor): self._inner = inner

    async def __call__(self, agent, tool_call):
        if tool_call.name not in ("read_file", "write_file"):
            return await self._inner(agent, tool_call)
        path = str(tool_call.arguments_dict.get("path", ""))
        if not path:
            return await self._inner(agent, tool_call)
        lock = _path_lock(path)
        async with lock:
            return await self._inner(agent, tool_call)
```

- **位置**:插入执行器链的沙箱内层、快照外层:

  ```
  审批 → 沙箱 → 文件锁 → 快照 → 工具
  ```

  `compose.py`:`base_executor = FileLockExecutor(SnapshotExecutor(base_executor, store.sessions))`,再包 `SandboxedExecutor`。对 `write_file`,临界区覆盖"读旧状态 → 写入 → 存快照"整段,原子成立。
- **覆盖范围**:`write_file` / `read_file` 的单路径访问。`glob_files`/`grep_files` 是目录扫描,不锁。**`bash` 不受覆盖**(沙箱直接执行,到不了快照层,且无法预测其碰哪些文件)—— 文档标注为边界。
- **无死锁**:单次调用至多持一把路径锁,无锁序依赖。
- **无竞争时零开销**:顺序模式从无竞争,常驻链上无害,不按模式门控,行为统一。

## 9. 错误处理

| 场景 | 处理 |
|---|---|
| 子 agent `MaxTurnsExceeded` / 异常 | 已有:返回 `ToolResult.error`,父 agent 看到错误文本继续 |
| 并发中一个子任务炸掉 | `gather(..., return_exceptions=True)`,失败映射 error ToolResult,其余不受影响,结果保序 |
| 预算耗尽 | 新委派返回 "subagent budget exhausted" 错误,父 run 继续 |
| 文件锁写失败 | 按正常工具错误走,锁在 `async with` 退出时必然释放 |
| 审批超时 / 用户点 n | 现有 fail-closed 路径(`"n"`);并发下各自配对自己的调用 |
| 取消(run 被新消息替换 / 断开) | 现有 `_cancel_current` 取消 run task;`WebApprover.drain()` 取消全部 pending 审批 future,不留残影 |

## 10. 测试验证

### 10.1 单元测试(质量门内,无 API key)

1. **嵌套深度**:高级模式下 level-1 子 agent 有 `delegate_to_*` 工具、level-2 没有(结构封顶,无环)。
2. **并发执行**:mock provider 单回合多 tool_call,断言 `gather` 并行、结果按调用顺序归位、单失败不拖垮其余。
3. **预算护栏**:计数到 40 后下一个委派返回 budget exhausted;`reset()` 后恢复。
4. **文件锁**:两个并发 `write_file` 同一路径,断言串行(结果顺序确定)且快照正确;并发 `read_file` 等写完成后才读。
5. **审批关联**:两个并发审批,决策帧带 `tool_call_id`,各自配对正确;过期 id 被丢弃;`drain` 取消 pending。
6. **开关重建**:`set_advanced` 后 `delegate_to_*` 工具集随之重建(unregister 再 register)。

### 10.2 e2e(真实模型)

扩展 `scripts/e2e_subagents_web.py`(或新增):

- **高级模式 e2e**:开高级开关,发"并行调研三个独立主题"提示词,断言收到 3 个 `subagent_start`(各自独立 run_id)、审批帧带 `tool_call_id` 且回传正确、`subagent_end` 全部到达。
- **嵌套 e2e**:发"先调研 A 再让 doc_writer 写报告"提示词,断言出现两层 run_id(level-1 卡片内嵌 level-2 卡片)。

### 10.3 能力对比脚本(新增 `scripts/e2e_subagents_compare.py`)

**目的**:用可复现证据证明高级模式在"多独立子任务"任务上更有效。

流程:

1. 同一复杂任务跑两遍:普通模式(开关关)vs 高级模式(嵌套+并发开)。
2. 任务选**可自动检查**型,例如:"并行调研仓库里三个独立模块,产出 `report.md`,含三个对应章节、每节 ≥ N 字、文末列出来源文件"。
3. **打分双轨**:
   - **确定性 rubric(主)**:文件存在 + 章节标题齐全 + 每节字数达标 + 引用文件数量,逐项给分 —— 结果可复现,不依赖裁判模型;
   - **LLM 裁判(辅)**:一次独立 `complete` 调用,按同一 rubric 打 0–10 完整度。
4. 输出对比表:两模式的 rubric 分 / 裁判分 / 耗时 / turn 数。

**定位如实标注**:演示性验证(n=1、模型有随机性),证明高级模式在"多独立子任务"型任务上倾向更完整,不是严格科学 benchmark。

## 11. 改动点清单

| 文件 | 改动 |
|---|---|
| `src/harness/config.py` | `+subagent_advanced`、`+subagent_budget` 及 `from_env` 解析 |
| `src/harness/core/runner.py` | `run_streamed(concurrent=False)`;`_run_streamed` 并发分支(gather + 保序 + 异常映射) |
| `src/harness/core/locking.py` | **新增** `FileLockExecutor` + 进程级路径锁注册表 |
| `src/harness/core/compose.py` | 链上插入 `FileLockExecutor`;`build_core_stack` 创建 `SubagentBudget` 并挂 `CoreStack`;`add_example_subagents` 接受 `advanced`、穿透 `on_event(run_id,…)`/`concurrent`/`budget` |
| `src/harness/core/snapshot.py` | 无改动(锁在外层,快照逻辑不变) |
| `src/harness/agents/subagent.py` | `as_agent(model, extra_tools=())`,注册到 registry 副本 |
| `src/harness/agents/orchestrator.py` | `SubagentRunStart/End` 不变;`SubagentTool` 加 `nested_delegates`/`concurrent`/`advanced`/`budget`;`invoke` 生成 run_id、预算检查/记录、`on_event(run_id,…)`;`subagent_as_tool`/`add_subagents` 穿透新参数;`+SubagentBudget`;`DELEGATION_PROTOCOL` 拆普通/高级两变体,`attach_delegation_protocol` 可逆替换 |
| `src/harness/web/runtime.py` | `WebApprover` 改 per-id pending futures + `approve()` + 新 `drain()`;`Runtime` 加 `advanced`/`set_advanced`/`_rebuild_subagents`,run 入口重置预算;`_forward_subagent_event(run_id,…)` |
| `src/harness/web/server.py` | WS `approval` 分支改 `rt.approve(tool_call_id, decision)`;`+set_advanced` 分支 |
| `src/harness/web/events.py` | `ready` 帧带 `advanced`(如有 serialize 入口) |
| `src/harness/web/static/js/app.js` | 高级开关;审批回传 `tool_call_id`;`subagentStack` 以 run_id 为键;`advanced_changed`/`set_advanced` 处理 |
| `src/harness/web/static/style.css` | 开关样式(复用现有控件风格) |
| `src/harness/cli/main.py` | `--subagents` 时按 `settings.subagent_advanced` 传 advanced;每次输入前 `stack.subagent_budget.reset()` |
| `tests/test_web_runtime.py` 等 | 第 10.1 节全部单测 |
| `scripts/e2e_subagents_web.py` | 高级模式 / 嵌套 e2e 场景 |
| `scripts/e2e_subagents_compare.py` | **新增** 能力对比脚本 |
| `README.md` / `docs/architecture.md` | 高级编排模式、文件锁、预算、能力对比脚本说明 |

## 12. 风险与注意点

- **R1 CLI 并发审批**:交互式 stdin 审批在并发下串行,UX 受限 —— 文档标注;web 端无此问题(tool_call_id 关联)。
- **R2 bash 不受文件锁覆盖**:任意 bash 文件访问无法预测 —— 文档标注为边界;结构化工具受保护。
- **R3 LLM 裁判偏差**:能力对比以确定性 rubric 为主,裁判分仅作辅助信号,结论不依赖裁判偏好。
- **R4 预算入口漏重置**:各 run 入口统一 `stack.subagent_budget.reset()`;单测覆盖 web 四入口。
- **R5 并发下的快照顺序**:文件锁保证同一文件快照+写入原子;不同文件互不阻塞,rollback 按时间倒序还原的语义不变。
- **R6 开关中途切换**:`_rebuild_subagents` 重建工具集;正在跑的 run 保持旧配置,下一个消息生效 —— 与权限模式语义一致。
