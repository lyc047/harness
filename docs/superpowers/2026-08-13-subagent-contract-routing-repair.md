# 子代理优化 #1–#3：验证驱动自修复 / 任务类型感知路由 / 接口契约强制

> 日期：2026-08-13
> 背景：多智能体 token-economy 研究收尾（v2 结果见
> [2026-08-13-token-economy-v2-results.md](2026-08-13-token-economy-v2-results.md)）。
> 本批次为用户授权的三项 harness 优化，编号与
> [2026-08-13-token-economy-followups.md](2026-08-13-token-economy-followups.md) 的旧 #4–#7
> **独立**（旧 doc 的 #1–#3 是 per-request 读超时 / store 泄漏 / coordinator 转 tracked 配置，已完成）。

> **状态（2026-08-13）：#1–#3 全部实现并通过质量门（ruff + mypy + pytest 299 passed）。
> 未跑真实 API 实验——三个功能开关默认全部关闭，v2 结果口径不受影响。**
> 多智能体研究到此为止；后续 #4–#6 只列入 backlog，不实现。

---

## 实现记录（2026-08-13）

| # | 落地 | 验证 |
|---|---|---|
| **#3 接口契约显式强制** | `SubagentSpec`/`Subagent` 加 `contract` 字段（YAML `contract:` 解析）；`SubagentTool.invoke` 在 brief 尾部追加 `Contract:` 节；`check_contract: Callable[[str], str\|None]` 钩子返回违规消息则把该次 attempt 标记为 error → 走既有 `fallback_model` 升级路径。5 个 bundled YAML（coder/frontend_design/security_reviewer/doc_writer/coordinator）补 `contract:` 块 | `test_contract_parsed_from_yaml`、`test_bundled_contract_carriers_are_nonempty`、`test_contract_appended_to_subagent_brief`、`test_contract_violation_triggers_escalation` |
| **#2 任务类型感知模型路由** | 新增 `src/harness/agents/routing.py`：纯函数 `classify_subtask(name, task, scope) -> "pro"\|""`（`DESIGN_HEAVY_SUBAGENTS` = frontend_design/security_reviewer/researcher；`REASONING_HINTS` 关键词命中）+ 工厂 `make_task_router(pro_model)`。`SubagentTool.invoke` 里 `first_model = routed or self._model`；升级条件改 `fallback_model != first_model`（路由已上 pro 则不再重复升级）。config 加 `subagent_router`（env `HARNESS_SUBAGENT_ROUTER`，`"auto"` 时 compose 接线）；CLI/web/REST/基准 4 调用点透传 | `test_classify_subtask_hints`、`test_task_router_routes_design_subtask_to_pro`、`test_task_router_keeps_mechanical_on_default`、`test_router_routed_failure_does_not_escalate_same_model`、config env 测试 |
| **#1 验证驱动自修复循环** | `e2e_subagents_compare_v6._run_verify` 改签名返回 `(verify_pass, FAIL 行)`（逐门失败详情不再丢弃）；`e2e_token_economy.py` 加 `REPAIR_MAX`（env `HARNESS_COMPARE_REPAIR`，默认 0=关）+ 纯函数 `GATE_FILE`/`GATE_SUBAGENT`/`ROBUST_EXPECT`/`_parse_fail`/`_robust_failures`/`_build_repair_brief` + `_dispatch_fix`/`_repair_loop`。两种模式都能修：advanced 走 `delegate_to_<name>` 子代理（计入 budget），normal 走 fresh pro 修复 agent。`store.close()` 挪最外层 finally（修复子代理 write_file 走 SnapshotExecutor 需 store 活着）；`_metrics` 加 `repair_rounds`/`repair_dispatches` | `test_parse_fail`、`test_gate_to_file_and_subagent_mapping`、`test_build_repair_brief*`、`test_robust_failures`；`REPAIR_MAX=0` 时 `_run_once` 行为与 v2 完全一致 |

**开关默认值**：`HARNESS_COMPARE_REPAIR=0`（修复关）、`HARNESS_SUBAGENT_ROUTER=""`（路由关）、
`check_contract=None`（契约钩子关，仅测试/显式配置生效）。三者全关 ⇒ v2 基准结果口径不变。

**附带改动**：`_run_verify` 4 处调用点解包（v6 `_run_mode`/`_salvage_run`、token_economy `_run_once`/`_salvage`）；
grep 确认无测试直接调 `_run_verify`，签名改动安全；`_run_all` 每轮打印与汇总表加 `repair_rounds` 列。

---

## #4–#6 后续计划（backlog，只写不实现）

多智能体研究到此为止；以下三项留作后续研究方向（等新研究主题再评估）：

- **#4 子代理 token 效率**：advanced 总 token 比 normal 多 ~30%（20k pro + 1.15M flash），flash 波动 ~2×
  （757k–1.63M）。方向：brief 瘦身（`_compose_brief` 去冗余）、共享 scratchpad 减少重复读文件、
  对只读子代理复用已缓存文件内容、`SubagentBudget` 粒度下探到每子代理。
- **#5 子代理 checkpoint/resume**：`_invoke_attempt` 的 `MaxTurnsExceeded` 分支把累积 messages 整个丢弃
  （orchestrator.py:301-310，`output = str(exc)`）。方向：partial output 落盘、resume 时从断点续跑、
  budget 记账区分"续跑"轮次。这是 advanced 在难任务撞 max_turns 时（coder×8 等）的质量损失面。
- **#6 架构演进**：deeper hierarchy（coordinator 下多级）、shared scratchpad（跨子代理共享中间产物，
  避免 coordinator 单向传话）、speculative parallel（对独立子任务并发猜测 + 择优）。超出当前两层嵌套
  结构，需重设计 `add_subagents` 的 `nested_delegates` 拓扑。

---

## 与旧 followups 的关系

本 doc 记录新一批 harness 优化（#1 修复循环 / #2 路由 / #3 契约），编号独立于
[2026-08-13-token-economy-followups.md](2026-08-13-token-economy-followups.md)（旧 #1–#7 全部完成）。
若后续要实现 backlog #4–#6，新建计划文档并沿用本 doc 的编号上下文即可。
