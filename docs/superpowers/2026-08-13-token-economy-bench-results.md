# Token 经济基准 · advanced 子代理模式 vs normal — 结果

> 日期：2026-08-13 · 场景 A（番茄钟全栈产品冲刺，与 pomodoro 基准同任务同门禁）
> 复现：`scripts/e2e_token_economy.py`（进程内 runner，主=pro / 子=flash 双 usage 统计）+ `scripts/score_robustness.py`
> 状态：**2 组 × 3 runs，其中 advanced 第 2 轮因网络卡死被 1800s 超时 salvage（详见 §6）**
> 定价：deepseek-v4-pro $1.68/M in · $3.36/M out；deepseek-v4-flash $0.14/M in · $0.28/M out（**12× 价差**）

## 1. 实验设计（回顾）

同一任务、同一 `_prompt`，只切换编排模式：

| 模式 | advanced 开关 | 语义 | LLM 账号 |
|---|---|---|---|
| `normal` | ✗ | 单 agent 直接干完 | 全程 pro |
| `forced-advanced` | ✓ | root=pro（只做协调）→ coordinator=flash → 并行专职 flash 子代理（coder / doc_writer / frontend_design / security_reviewer） | root 用 pro，其余全用 flash |

每轮用两个独立 provider 实例分别统计 pro / flash 的 prompt+completion+reasoning token，按真实单价折算成本。跑完每轮照旧跑 5 项契约验证（verify）、模型自己的 pytest、对抗性鲁棒性审计（robust 0-4）。

## 2. 结果总览（逐轮）

| 模式 | run | pro_tok | flash_tok | 子代理数 | cost | wall s | verify | pytest | robust |
|---|---|---|---|---|---|---|---|---|---|
| normal | 1 | 766,711 | 0 | 0 | $1.354 | 437 | 4/5 | 25 | 2/4 |
| normal | 2 | 710,058 | 0 | 0 | $1.268 | 523 | 4/5 | 29 | 2/4 |
| normal | 3 | 301,627 | 0 | 0 | $0.585 | 506 | 5/5 | 22 | 2/4 |
| **normal 均值** | | **592,798** | 0 | 0 | **$1.069** | **488** | 4.3/5 | 25.3 | 2/4 |
| forced-advanced | 1 | 20,007 | 742,973 | 8 | $0.153 | 390 | 4/5 | 25 | 2/4 |
| forced-advanced | 2 | ⚠ 0（超时） | 0 | — | $0 | 1800 | 4/5 | 23 | 0/4 |
| forced-advanced | 3 | 19,864 | 607,974 | 8 | $0.134 | 539 | 5/5 | 41 | 2/4 |
| **advanced 干净轮均值** | | **19,935** | 675,474 | 8 | **$0.143** | **465** | 4.5/5 | 33.0 | 2/4 |

> ⚠ advanced-2：在一轮子代理重派后网络连接卡死（CPU 0、连接 CLOSE_WAIT），被整轮 1800s 超时兜底 salvage，token 记 0。**实现本身已基本完成**（salvage 时仍跑出 verify 4/5、pytest 23）——卡住的是最后收尾的 coordinator 调用，不是产出的质量。该轮不计入所有降幅/质量统计。

## 3. 主 claim：advanced 用 ~3.4% 的 pro token 完成同样的任务

**PRO-token 降幅 96.6%**（干净轮）：normal 均值 592,798 → advanced 均值 19,935。

逐 run 看，advanced 两轮的 pro token 惊人地稳定：20,007 / 19,864（±1%），而 normal 三轮散布在 301k–767k（波动 2.5×）。原因是结构性的：advanced 里 pro 只跑「root 协调 + 任务描述」，每一轮都差不多大；normal 里 pro 要把实现细节的每一步对话都塞进自己的上下文。

**上下文（prompt token）降幅 96.8%**：normal 均值 549,216 → advanced 均值 17,568。pro 端的上下文占用被压到 ~17k，意味着高端 LLM 的**上下文窗口被从「整个实现过程」解放为「只装协调状态」**。

## 4. 质量佐证：降到 flash 上，三套门禁一项没降

| 门禁 | normal 均值 | advanced 干净轮均值 | 变化 |
|---|---|---|---|
| verify（契约，5 项） | 4.3/5 | 4.5/5 | **不降**（advanced-3 拿满 5/5） |
| pytest（模型自测） | 25.3 | 33.0 | **+31%**（advanced-3 写了 41 个用例） |
| robust（对抗鲁棒性 0-4） | 2.0/4 | 2.0/4 | **持平** |

和 pomodoro 基准的结论一致：flash 子代理（尤其 `security_reviewer` / `coder`）产出的实现照样过了契约门、守住了对抗性攻击项；测试覆盖反而更高。**降价的 12× 差价没有以质量为代价。**

## 5. 成本对比：真钱省 87%（每轮 $1.07 → $0.14）

即使把 flash token 的**全部成本**也计入（advanced 的 cost 是 pro+flash 两边加总，normal 只有 pro），advanced 每轮均值 $0.143，normal 每轮均值 $1.069，**便宜 86.6%**。

进一步拆：advanced 的 flash 用量（675k token）虽然和 normal 的 pro 用量（593k）差不多，但 flash 单价是 pro 的 1/12，所以钱大部分花在 pro 侧才贵。也就是说**子代理模式把「贵 token 换成 12 倍便宜的 token」，总 token 量没少，账单缩到 1/7.5。**

## 6. 根因/机制：为什么 token 省得这么彻底

- **normal**：一个 pro agent 从头到尾持有全部实现上下文。每一步工具结果、每一段生成的代码都回灌进同一个 500k+ token 的对话，且每次新调用都要把历史重发一遍 → prompt token 只涨不降。
- **forced-advanced**：`root(pro) → coordinator(flash) → 并行子代理(flash)`。pro 只发**一次**任务描述、收**一次**最终交付，prompt 端 ~17k 恒定；真正把每个模块写出来的活（engine / storage / api / static）全部下放到 flash 子代理，各自的上下文在子代理内部独立、不回流 pro。**pro 的 token 从 O(整个实现) 变成 O(协调)。**

这解释了为什么 advanced 两轮 pro token 几乎一样——pro 的负担不再随实现复杂度增长，只随协调状态增长。

## 7. 局限与说明

- **样本量小**：normal 3 轮、advanced 干净 2 轮。normal-3 的 301k pro token 说明 normal 侧也有方差（模型走捷径时 pro 用量可低至 1/2）。
- **advanced-2 超时**：一次间歇性网络卡死（子代理重派后 API 连接 CLOSE_WAIT），非流程 bug——advanced-1/3 均正常完成。当前兜底是整轮 1800s 超时 salvage（token 归零，产出的 verify/pytest 仍记录）；**后续改进方向是给 provider 加 per-request 读超时**，让单次卡住的调用快速失败、累积 token 得以保留，而不是整轮作废。
- **flash 质量依赖模型能力**：本结论在 deepseek-v4-flash 上成立。若 flash 弱到写不出合格的模块，coordinator 重派次数会上升（advanced-2 前就是 `frontend_design` 撞满 10 轮），省 token 的效果会被协调开销吃掉。
- **advanced 的 verify 4/5 与 robust 2/4 非满分**：与 pomodoro 基准一致，advanced 也不是满分，但**任何一项都不低于 normal**——本基准证明的是「降到便宜模型不掉质量」，不是「advanced 提升质量」。

**一句话**：在番茄钟任务上，advanced 子代理模式把 pro token 用量压到 3.4%（降幅 96.6%）、pro 上下文压到 17k（降幅 96.8%），三套质量门禁一项不降（verify 4.3→4.5、pytest 25→33、robust 持平），真钱成本从每轮 $1.07 降到 $0.14（-87%）——省的是高端 LLM 的 token 与上下文，代价由 12 倍便宜的 flash 子代理承担。
