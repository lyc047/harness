# 子代理优化 #1–#3 真实 API 实验（n=2×3 组）

> 日期：2026-08-14
> 背景：验证 [2026-08-13-subagent-contract-routing-repair.md](2026-08-13-subagent-contract-routing-repair.md)
> 实现的 #1（验证驱动自修复循环）/ #2（任务类型感知路由）的真实效果。#3 契约进 brief 软性生效于所有组
> （bundled YAML 已提交）；`check_contract` 机器钩子本轮未接线（用户决定跳过）。
> 任务：pomodoro sprint（`scripts/e2e_token_economy.py`，in-process CoreStack，主 pro + 子代理 flash）。

## 实验设计

| 组 | HARNESS_SUBAGENT_ROUTER | HARNESS_COMPARE_REPAIR | 观察目标 |
|---|---|---|---|
| 基线 | `""` | `0` | 新口径基线（有契约提示，无路由/修复） |
| 路由 | `auto` | `0` | #2：pro 路由对质量/成本/时间的影响 |
| 修复 | `""` | `2` | #1：repair_rounds、是否捞回失败门 |

每组 2 轮 forced-advanced；normal 不重跑。**实际花费 $3.22 / 6 轮**（路由组 pro 计价拉高，超出预估 $1.5）。

## 结果（组均值，n=2）

| metric | 基线 | 路由 | 修复 |
|---|---|---|---|
| pro_tokens | 20442 | 20421 | 20244 |
| flash_tokens | 914787 | 1097243 | **1897533** |
| cost_usd | **$0.18** | **$1.10** | $0.33 |
| verify_pass | 4.5/5 | 3.5/5 | **5.0/5** |
| pytest_passed | 58 | 45 | 36 |
| robust_pass | **2.0/6** | **5.0/6** | 2.0/6 |
| repair_rounds | 0 | 0 | 1.0 |
| seconds | 546 | 1303 | 1375 |
| subagent_runs | 7 | 8 | 16 |

相对基线：路由 cost **6.1×** / flash 1.2× / wall 2.4×；修复 cost **1.8×** / flash 2.1× / wall 2.5×。

## #2 任务路由：质量-成本旋钮的实证

- **robust 2/6 → 5/6（+3 对抗探针，两轮稳定）**：pro 模型写的 api.py 明显更能处理 hostile input
  （huge_id 超界→400 而非 500、deep_nested 防 RecursionError）。这是 #2 的强正面证据。
- **但代价真实**：cost **6.1×**（$0.18→$1.10）、wall 2.4×。verify 略降（4.5→3.5）、pytest 波动大。
- **指标盲区**：`pro_tokens` 三组持平（~20k）是假象——路由升到 pro 的 token 记在**子代理 provider**
  （flash.usage_log）里，pro_tok 度量的是主流程 provider。`cost_usd`（按 model 字段计价）才反映真实成本。
- 结论：路由用 6× 成本换 robustness +3 探针，verify/pytest 无提升。值不值取决于 robustness 的业务价值；
  它**不是省钱工具**（与 v2"advanced 全 flash 省 pro token"的主张是张力关系）。

## #1 修复循环：机制对、本案例捞不回

- **机制工作**：两轮都触发 `repair round 1: ['coder']`（verify 已 5/5 时仍把 4 个 robustness 失败打包派给
  coder）；修复后无改善 → `no gate improvement — stopping`（预算止损生效，不崩）。
- **效果不佳**：robust 停在 2/6。修复派发的 **flash coder 修不动** hostile-input 探针——改了 api.py 但探针仍失败。
- verify 保持 5.0/5（两轮全过，主流程模型自己过的，非修复功劳）；成本 flash 2.1× / cost 1.8× / wall 2.5×。
- **根因指向**：修复子代理也需要更强模型——正是 #2 的路由 / #6 的 `fallback_model` 能提供的。本实验里
  #2（主流程 pro 写 api.py → robust 5/6）和 #1（flash 修复 → robust 不动）形成对照：**flash 修复能力不足**。

## 综合洞察

1. **两机制互补但需组合**：#2 让主流程把 api.py 写对（贵但有效）；#1 负责兜底但**修复 agent 必须升级到 pro**
   才有用（本实验 flash coder 修不动）。建议 `_dispatch_fix` 的 advanced 路径给修复子代理路由到 pro，
   或复用 `fallback_model`——这是 #1 下一轮最直接的改进。
2. **契约钩子（#3 后半）仍缺实测**：本轮只验证了契约进 brief 的软性影响（所有组带契约），`check_contract`
   机器校验的误杀率/价值未量化。
3. **指标设计启示**：`pro_tokens` 对子代理 pro 路由无感，成本分析应以 `cost_usd` 为准；若要 per-model 归因，
   子代理 provider 的 usage_log 需按请求 model 分流统计。

## 局限

- n=2/组，单任务（pomodoro），未跑 normal 对照（只 advanced）；结论是方向性而非定量。
- 路由组 verify/pytest 的下降可能是单轮噪声（轮1 pytest=32 vs 轮2=58，波动大）。
- 契约软性提示在所有组存在，其独立影响未隔离。
- 原始数据：`bench-2026-08-14/results-{baseline,router,repair}.jsonl`（本地，未 commit）。

---

## 第二轮：repair 升级到 pro（#1+#2 组合，2026-08-14 晚）

> 背景：第一轮结论指向"修复子代理需升 pro"。本轮把修复派发的模型强制为 pro 跑 2 轮 advanced，
> 检验 **pro 修复能否捞回失败门**（flash 已证修不动）。
> 实现：`SubagentTool.invoke` 加 per-call `model` 覆盖（`first_model = override or routed or default`）；
> 基准加 `HARNESS_COMPARE_REPAIR_MODEL=pro` → `_dispatch_fix` 透传 `model=stack.agent.model`。
> 开关默认关，v2 口径不变。**实际花费 $1.67 / 2 轮**。

### 结果（四组对比，组均值 n=2，均 forced-advanced）

| metric | 基线 | 路由 | 修复(flash) | **修复-pro** |
|---|---|---|---|---|
| pro_tokens | 20442 | 20421 | 20244 | **20216** |
| flash_tokens | 914787 | 1097243 | 1897533 | **1568156** |
| cost_usd | **$0.18** | **$1.10** | $0.33 | **$0.83** |
| verify_pass | 4.5/5 | 3.5/5 | **5.0/5** | 4.0/5 |
| pytest_passed | 58 | 45 | 36 | 51.5 |
| robust_pass | **2.0/6** | **5.0/6** | **2.0/6** | **5.0/6** |
| repair_rounds | 0 | 0 | 1.0 | 1.0 |
| seconds | 546 | 1303 | 1375 | 920 |

### 本轮 2 轮的修复过程（日志摘录）

- **轮1**：主流程后 `FAIL engine: elapsed did not resume`（verify 4/5）、robust **5/6**。
  `repair round 1: ['coder'] model=deepseek-v4-pro` → **pro coder 撞 max_turns=8**、verify 仍 4/5、
  pytest **40→39**（pro 修复把已通过的测试改挂了）→ `no gate improvement — stopping`。
- **轮2**：主流程后 `FAIL readme: README missing section 'overview'`（verify 4/5）、robust **5/6**。
  `repair round 1: ['coder', 'doc_writer'] model=deepseek-v4-pro` → 双 pro 修复后 verify 仍 4/5 → stop。

### 核心发现

1. **pro 修复依然零捞回**：4 个 case（上轮 flash×2 + 本轮 pro×2）全部 `no gate improvement — stopping`。
   修复目标从 api.py 换到 engine（并发计时）和 readme 后，pro 也修不动。**#1 的瓶颈不再是"flash 能力不足"，
   而是修复派发设计本身**——brief 粒度、max_turns=8/12、修复后的验证闭环。
2. **robust 5/6 归因于主流程，不归因于 repair**：本轮 repair 根本没被派去修 api.py（主流程已写好抗 hostile input）。
   robust 与"谁写 api.py"强相关：pro 写 → 5/6（路由、修复-pro），flash coder 写 → 2/6（基线、修复）。
   这再次确认 **#2 路由是"预防"价值，与 #1"修复"无关**。
3. **pro 修复贵 2.5× 于 flash 修复**：$0.83 vs $0.33 vs 基线 $0.18/轮。轮2 双 pro 修复单轮 $1.13。
   且 pro 修复有**回归风险**（轮1 pytest 40→39），`_repair_loop` 只比较 (verify, robust)，不把 pytest 纳入
   止损判断、也无回滚——修复可以越修越糟不被拦。
4. **主流程产物随机性是混杂因素**：每轮主流程是否委托 flash coder 写 api.py 决定 robust 基线（2/6 或 5/6），
   n=2 下 repair 的增量无法与主流程随机性分离——"repair 修 robustness"在本轮样本里甚至没被测到。

### 结论

- **修复路线（#1）在两轮 4 个 case 下被证伪**：升 pro 不改变"捞不回 gate"，反而更贵、有回归风险。
  若要继续，方向应是修复派发设计（brief 注入 verify_impl.py 上下文 + 更大 max_turns + pytest 纳入止损/回滚），
  而不是继续换模型。
- **预防路线（#2）的证据更强**：pro 写 api.py 的两组 robust 稳定 5/6，flash 写的两组稳定 2/6。
  六探针里 api.py 的 hostile-input 类是主因；成本上路由 $1.10 仍是最贵，值不值取决于 robustness 的业务价值。
- 原始数据：`bench-2026-08-14/results-repair-pro.jsonl` + `run-repair-pro.log`（本地，未 commit）。

### 封档（2026-08-14）

用户决定不再跑定向对照实验，按现有证据封档：**#2 路由归为"强证据非严格因果"**——
能证明"pro 写 api.py → robust 5/6"（三组 api.py 代码特征 + 四组数据一致），能佐证"router 触发 coder 升 pro"
（cost 6.1× + api.py 在 coder 撞 max_turns 窗口生成），但每轮主流程"自己写 vs 委托 coder"随机、n=2 无对照，
router 的独立增量未严格证明。遗留改进项：修复派发设计（见 #1 结论）与 router 增量定向实验，均只入 backlog。
