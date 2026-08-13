# Token 经济基准 · 后续优化计划（#4–#7）

> 日期：2026-08-13
> 背景：本会话完成了改进 #1–#3（per-request 读超时 / store 泄漏 / coordinator 转 tracked 配置）。
> 本文件记录用户在授权时明确列入后续计划的四项：**#4 失败轮丢弃累积数据、#5 门控子串误报、
> #6 flash 无升级到 pro 路径、#7 基准样本小**。与结果文档 §7 局限对应：
> [2026-08-13-token-economy-bench-results.md](2026-08-13-token-economy-bench-results.md)。

---

## #4 失败轮丢弃累积数据

**现状**：`scripts/e2e_token_economy.py::_run_all` 用 `asyncio.wait_for(_run_once, RUN_TIMEOUT)` 包裹每一轮。
超时时 `wait_for` 取消 `_run_once`，`asyncio.CancelledError`（`BaseException`，非 `Exception`）直接穿透
`_run_once` 内部的 `except Exception`，落到外层 `_salvage(label, i, out_dir, reason="timeout", ...)`，
而 `_salvage` 把 `pro_tokens / flash_tokens / cost_usd` 全部硬编码为 0（`scripts/e2e_token_economy.py:227-252`）。

**后果**：超时轮在 API 侧实际消费的 token（可能数十万）被整轮归零，既浪费账单数据，也让「降幅按干净轮算」的
口径（`clean()` 过滤 `reason == "ok"`）之外的异常轮失去可比信息。

**已缓解**：#1 的 per-chunk 读超时让「连接开着但不吐 chunk」的卡死快速以 `APITimeoutError`（属 `Exception`）失败，
会被 `_run_once` 的 `except Exception` 捕获 → 走正常 usage 收集路径 → token 保留。但仍有两类路径没覆盖：
外层 `wait_for` 整体超时（进程级卡死、`RUN_TIMEOUT` 到期），以及进程被外部强杀。

**建议方向**（后续实现）：
1. `_run_once` 把 usage 快照挪进 `finally`（或在 catch 里包一层 `BaseException`），使 `CancelledError`
   也先记下 `sum_usage(pro.usage_log) / sum_usage(flash.usage_log)` 再退出。
2. `_salvage` 增加可选 `partial: dict` 参数：超时轮带上快照后的 usage 与 `reason="timeout(partial)"`，
   统计时可按「干净轮 vs 部分轮」分别汇总，而不是全 0。
3. 结果文档 §3 的降幅口径保持不变（只比干净轮），但结果表新增一列标注部分轮的实际 token。

**范围**：`scripts/e2e_token_economy.py` + 结果表口径；不含 runner 层。

---

## #5 门控子串误报

**现状**：`scripts/pomodoro_verify_template.py` 的 5 道门是固定子串/弱结构断言，存在「凑词过门」的误报面：
- `assert "?" in src`（storage 门，L85）：只要源码里出现任意一个 `?`（注释、字符串、三目）就过，
  不能证明 SQL 占位符确实用于全部查询。
- `assert "timer" in idx.lower()`（static 门，L264）：`index.html` 里出现单词 "timer" 就过，
  即使没有真正的定时器元素；`assert "<button" in idx` 同理只看标签存在。
- README 门（L276）：`assert sec in text` 只要求小节名出现在文本里，空小节也能过。
- `assert "eval(" not in src and "exec(" not in src`（L71）只挡字面形式，`eval` 经拼接/别名照样绕。

**后果**：verify 分数可能虚高（假阳性 PASS），使「advanced 不降质量」的结论掺入噪声——
过门的不一定是真实现。

**建议方向**（后续实现，需权衡「门要硬但不可误杀」）：
1. 把子串门改成**最小行为门**：storage 门改为实际 `execute` 一条带占位符的查询并断言参数绑定生效
   （参照现有 engine/storage 的真实调用测试，见模板 L150-160 的 `store` 冒烟）。
2. static 门改为解析 `index.html` 里的 `id`/`class`，要求出现真实控件（如 `id="timer"`）而非单词。
3. README 门改为「每个小节头之后必须跟 ≥1 行非空内容」。
4. 保留 1–2 道子串门作为廉价的「结构性存在」信号，但单独计分（existence vs behavior），
   报告里区分，避免混合成单一 0-5。

**范围**：`scripts/pomodoro_verify_template.py` + 基准任务 prompt 的契约描述 + 结果文档 §4 的口径说明。

---

## #6 flash 无升级到 pro 路径

**现状**：`src/harness/agents/orchestrator.py::SubagentTool.invoke`（L243-308）：flash 子代理
`MaxTurnsExceeded` 或异常时统一返回 `ToolResult.error(...)`（L284-306），没有「用更强模型重试一次」的机制。
结果文档 §7 明确记过：`frontend_design` 撞满轮数时 coordinator 只能同模型重派，省 token 的效果被协调开销吃掉。

**后果**：弱模型子代理在难任务上反复失败 → 反复重派（同模型）→ 撞满 `SubagentBudget` 后整个 delegation
报 "subagent budget exhausted"（L255-256）→ 一轮作废。这是 advanced 模式在「任务难、flash 能力不足」时
质量与 token 的失效面。

**建议方向**（后续实现）：
1. 在 `SubagentTool` 上加**降级升级**字段：`fallback_model: str | None`。当本轮子代理
   `MaxTurnsExceeded` 或连续 N 次 `ToolResult.error` 时，用 `fallback_model`（pro）重派同 task 一次，
   budget 记账区分「升级重试」的轮次。
2. `add_example_subagents` / `build_core_stack` 透传 `subagent_fallback_model` 设置项（默认
   `HARNESS_SUBAGENT_FALLBACK_MODEL`，未设则关闭）。
3. 事件流新增 `SubagentEscalated` 事件（或复用 `SubagentRunStart` 带 `escalated: True`），
   web/审计能区分「首次 flash 失败 → pro 兜底成功」。
4. 基准侧在 `_make_sink` 里统计 escalated 次数，作为质量门之一（escalated 多的任务说明 flash 不够用）。

**范围**：`src/harness/agents/orchestrator.py`、`src/harness/core/compose.py`、`src/harness/config.py`、
`scripts/e2e_token_economy.py`。

---

## #7 基准样本小

**现状**：每组 3 轮（`HARNESS_COMPARE_RUNS`，默认 3），advanced 干净轮只有 2。结果文档 §7 已承认：
normal 侧 pro token 从 301k 到 767k（波动 2.5×），单凭 3 轮均值下的「96.6% 降幅」置信度有限。

**建议方向**（后续实现）：
1. 默认 `RUNS` 提到 5（`HARNESS_COMPARE_RUNS` 已是环境变量，只需调默认值并跑一轮长的）。
2. 汇总里加**每组的方差/四分位**（`_spread` 已有 min–max，补 stddev），降幅报告带
   「normal 中位数 → advanced 中位数」而非仅均值，抗单点 outlier。
3. 支持 `HARNESS_COMPARE_RUNS=normal=5,forced-advanced=5` 式分组不等跑数，便于 advanced 省钱跑更多轮。
4. 文档承诺：结论标注「n=3 轮，区间见 spread」，不写「确定降幅」。

**范围**：`scripts/e2e_token_economy.py` 汇总段 + 结果文档。

---

## 优先级与依赖建议

| 项 | 依赖 | 理由 |
|---|---|---|
| **#7**（样本量） | 无 | 最便宜，先跑长样本让 #3/#4 的口径变化有数据支撑 |
| **#4**（保留失败轮数据） | 无 | 纯脚本侧，独立；#1 已缓解一半 |
| **#5**（门误报） | 无 | 独立；会改变 verify 分数口径，建议在 #7 重跑前定稿 |
| **#6**（升级路径） | #7 重跑前 | 改动最大（orchestrator + config + web 事件），放进功能计划而非纯基准 |

建议顺序：#7 → #5 → #4 → #6（#6 单独成功能计划，其余可并进基准工具链迭代）。
