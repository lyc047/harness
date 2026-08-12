# Concurrent Implementation Sprint — Benchmark Design (v5)

## 1. 背景与目标

上一轮(open-ended arch-judge, v4)用"单篇架构报告"任务做 LLM 盲评,结论是 **normal/advanced/depth2 质量无统计差异**,且分析出根因:该任务形状不需要 advanced 的机制。随后确认了 advanced 相对 normal 的 **3 个结构性差异**:

| 能力 | normal | advanced |
|------|--------|----------|
| 两级委派(coordinator→专家) | ✗ 只有 root 能 delegate 一次 | ✓ level-1 subagent 也带 `delegate_to_*`,深度上限 2 |
| 并发 fan-out | ✗ 全串行 | ✓ `run_streamed(concurrent=True)`,一个 turn 内多个 delegate 经 `asyncio.gather` 并行 |
| 上下文隔离 | ✗ 所有子结果堆积在 root 上下文 | ✓ 分支结果由 coordinator 持有,root 只见摘要 |

**本实验目标**:设计一个能真正用上并突出 advanced 优势的任务,用**客观通过门**测出差异——不再依赖易被噪声淹没的 LLM judge。

**已与用户确认的两个决策**:
- **任务形状**:并发实现冲刺(scratch 临时 Python 包,N 个独立模块 + 测试 + README)。
- **分组**:三组含对照(normal 自由基线 / forced-normal / forced-advanced)。

## 2. 任务定义

在临时 scratch 目录(harness 用 `tempfile.mkdtemp(prefix="harness-sprint-")` 创建)实现一个小型独立 Python 包,4 个相互独立的工具模块,每个模块一个 `.py` + 一个 `test_<name>.py`,外加 `README.md`。

**模块契约**(任务提示词里固化,verify 按此断言):

- `token_bucket.py` — `class TokenBucket(rates: float, capacity: float)`,方法 `acquire(n: float = 1.0) -> bool`(令牌充足返回 True 并扣减,否则 False),线程安全。
- `lru_cache.py` — `class LRUCache(capacity: int)`,方法 `get(key: K, default: V | None = None) -> V | None`、`put(key: K, value: V) -> None`,容量满时逐出最久未用项,线程安全。
- `retry.py` — `async def retry_with_backoff(fn: Callable[[], Awaitable[T]], *, attempts: int = 3, base_delay: float = 0.1, max_delay: float = 2.0) -> T`,指数退避,最后一次异常向上抛。
- `counter.py` — `class AtomicCounter(initial: int = 0)`,方法 `increment() -> int`(返回新值)、`value() -> int`,线程安全。

**工程要求**:纯标准库;`from __future__ import annotations`;builtin generics;`ruff check` 干净;每模块写自己的 pytest 测试文件;最终 `uv run pytest -q <out>` 全绿;`uv run ruff check <out>` 干净;报告测试摘要行(如 `4 passed`)。

## 3. 分组与因果设计

三组共用同一套 `coordinator.yaml` 运行时注册(gitignored,用完 unlink,coordinator 无 write/bash,只有 read/glob/grep/web_search——同 v4),**唯一变量是 advanced 开关**:

| 组 | 提示词 | advanced | 预期 coordinator 行为 |
|----|--------|----------|------------------------|
| **normal** | `SPRINT_TASK`(无委派指令) | False | 模型自己实现,可能根本不 delegate,或只派一次 coder/doc_writer |
| **forced-normal** | `SPRINT_TASK_FORCED`(强制 delegate_to_coordinator) | False | coordinator **无 delegate 工具**→ 空转返回;root 兜底自己实现。展示"结构上做不到分工" |
| **forced-advanced** | `SPRINT_TASK_FORCED`(同) | True | coordinator **有 delegate_to_coder** → 4 个模块并行 fan-out 给 coder;root 只见摘要。展示 advanced 的深链+并发 |

`SPRINT_TASK_FORCED` 提示词措辞(要求先 delegate 给 coordinator,coordinator 负责把实现委派出去):
```
DELEGATE THE ENTIRE TASK to a single subagent — call the delegate_to_coordinator
tool once and hand it the FULL task below plus the target directory {out}. Do NOT
scaffold, write, read, search, or implement anything yourself — the coordinator
owns the whole job.

NOTE: the coordinator cannot write files or run bash. It is expected to hand the
implementation work to coder subagents (one per module, in parallel where possible)
via its delegate tools, then verify. Wait for the coordinator's summary and report back.
```

## 4. 客观通过门(防糊弄)

模型自己写的测试可能弱断言,所以硬门由 harness 自带的验证脚本承担:

- **`verify_impl.py`**(harness 生成,写进每个 `{out}` 目录,不参与模型运行):对 4 个模块各做固定断言——契约存在性(类/方法签名)+ 关键行为 + **并发 smoke**(多线程并发调用不崩溃且结果正确,如 8 线程 × 1000 次 `increment()` 后 `value()==8000`)。逐个输出 `PASS/FAIL`,harness 跑 `uv run python {out}/verify_impl.py` 收集。
- **模型 pytest**:`uv run pytest -q {out}` 的 passed 数作为**次级指标**(反映模型的自测覆盖,不作为硬门——因为模型可写假测试)。

**通过率 = verify 的 4 项 PASS 数(0-4)**,这是组间对比的主轴。

## 5. 指标

每组 × RUNS(=3,env `HARNESS_COMPARE_RUNS`):

**客观通过门**:
- `verify_pass`:verify_impl.py 的 4 项中 PASS 数(0-4)——主指标。
- `pytest_passed`:模型 pytest 的 passed 数(次级)。

**结构**(WS 帧追踪,复用 v3/v4 的链追踪逻辑):
- `depth`、`max_concurrency`、`waves`、`types`、`delegations`、`sub_turns`
- `chain`:委派链集合(如 `coordinator -> coder` ×4)。
- `web_searches`、`bash` 次数。

**成本**:
- `seconds`(墙钟)、`turns`(root 轮数)。

## 6. 脚本结构

**新建 `scripts/e2e_subagents_compare_v5.py`**:

- 复用 v2 的 `REPO_ROOT/_fmt_spread/_free_port/_wait_health`,v4 的 WS 追踪(`_run_mode` 改造为可传 `prompt`/`advanced`)。
- `SPRINT_TASK`(任务提示词,含 `{out}` 占位)。
- `SPRINT_TASK_FORCED`(强制 coordinator 版)。
- `VERIFY_SOURCE`(verify_impl.py 的源码字符串)。
- `COORDINATOR_YAML_TEXT` + `_write_coordinator()`(复用 v4)。
- `_run_mode(port, out, *, prompt, advanced)` → metrics(结构 + 成本)+ 客观门。
- 三组循环:`GROUPS = [("normal", False, False), ("forced-normal", True, False), ("forced-advanced", True, True)]`,字段 = `(forced, advanced)`。normal 组也写 coordinator.yaml(三组共用),但 normal 提示词不引导使用。
- 每 run 后跑 verify + pytest,收集 `verify_pass`/`pytest_passed`。
- 汇总表:每组 median verify_pass(0-4)、median wall、median depth/concurrency、chain 去重样例。
- finally:terminate server + unlink coordinator.yaml。

## 7. 验证方式

1. 质量门:`uv run ruff check . && uv run mypy src && uv run pytest -q` 全绿。
2. 真实 WS 冒烟:1 个 forced-advanced run,断言形成 `coordinator -> coder` 深链 + 并发峰值 ≥2 + verify_pass ≥3(先手工核对 verify_impl.py 契约与 4 个模块可测)。
3. 完整 n=3 × 3 组 ≈ 9 个 run,后台跑(每 run 200–600s,预计 1.5–3 小时),如实记录超时/失败。

## 8. 风险与对策

- **R1 模型不写 `{out}` 而写别处** → verify 失败,如实记录;提示词里把 `{out}` 路径写死。
- **R2 模块 API 名与契约不符** → verify 按契约断言,FAIL 计入通过率(规范遵从度本身是测点)。
- **R3 forced-normal 超时**:coordinator 空转 → root 兜底,可能慢。timeout 900s,超时 skip(如实记录 n)。
- **R4 并发 4 个 coder 同时打 LLM API** → 可能慢或限流。这是 advanced 的核心机制,如实记录;不因慢而失败,只看 verify 与结构。
- **R5 verify 断言过严导致全组 0 分** → 断言设计为"合理实现必过、糊弄过不去"(如并发 smoke、容量驱逐、退避次数),先手工验证一份人工实现能全过。

## 9. 交付物

- `scripts/e2e_subagents_compare_v5.py`(生成 + 客观门 + 汇总)
- 结果文档 `docs/superpowers/2026-08-12-concurrent-sprint-results.md`(中文)
- 9 份 `{out}` 目录(report 无,含 4 模块 + 测试 + README + verify_impl.py)

## 10. 成功判据

- 客观可分辨:forced-advanced 的 verify_pass 中位数 ≥ forced-normal,且链结构(深度 2、并发峰值 ≥2)在 forced-advanced 稳定出现、在 forced-normal 不出现。
- 若 forced-advanced 与 forced-normal 的 verify_pass 也持平 → 如实报告"此规模下无差异",不强行包装。
