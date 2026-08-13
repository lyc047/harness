# Token 经济基准 v2 · 更难任务 + 专家严格评分

> 日期：2026-08-13
> 前置：#4-#7 全部实现过质量门（见 [2026-08-13-token-economy-followups.md](2026-08-13-token-economy-followups.md)）。
> 本轮：真实 API 基准重跑（`normal=3, forced-advanced=5`）+ 更难任务 + 我以专家视角逐轮审阅产物并严格评分。

> **状态：已完成。** 8 轮全部 clean（`reason=ok`），降幅 97.7%，专家评分有区分度（6.4–9.0，无满分）。

---

## 1. 方法

- **脚本**：`scripts/e2e_token_economy.py`（in-process CoreStack，pro 主 provider + flash 子代理 provider，track_usage）。
- **任务**：`SPRINT_TASK` 扩展版——Pomodoro 服务含 `engine/storage/api/static/README`，api 加 `PATCH`/`DELETE /api/sessions/<id>` 与 `GET /api/stats`、重启持久化、前端实时计时+历史+统计+删除、全端点校验。
- **组**：
  - `normal`（pro 单 agent 全包）n=3
  - `forced-advanced`（root pro → coordinator flash → parallel flash 子代理）n=5
- **每轮产物**：verify 门控 5/5（`pomodoro_verify_template.py`，行为级）、模型自写 pytest、adversarial robustness 6 探针（`score_robustness.py`，黑盒 HTTP）。
- **env**：`HARNESS_COMPARE_RUNS=normal=3,forced-advanced=5`、`HARNESS_COMPARE_TIMEOUT=2400`、`HARNESS_TOKEN_ECON_RESULTS=$TEMP/harness-token-econ-v2.jsonl`。

## 2. 专家评审 rubric（5 维 × 0-10，严格）

评分原则：**8+ 罕见**——只有几乎挑不出毛病的实现才给 8+；中枢 5-7；明显缺陷 4 及以下。门控/探针是「通过线」，专家评分在通过线之上进一步区分**真实完成度与工程质量**。

| 维度 | 看什么 | 10 分锚点 | 扣分项 |
|---|---|---|---|
| **1 正确性 Correctness** | verify 5/5 + 自写 pytest 全绿 + robust 6/6，且我独立抽查边界（负/零/浮点/超界 id、重复删除、stats 聚合在真实数据上） | 三套门全过 + 抽查无例外 | 任一门不过、pytest 失败、抽查暴露崩溃/错值 |
| **2 完整性 Completeness** | 契约端点全实现、持久化（重启后数据在）、前端实时计时/历史/统计/删除全有、校验覆盖所有端点 | 全契约 + 深交互 | 缺端点、缺持久化、前端交互是摆设 |
| **3 代码质量 Code Quality** | 结构/命名/错误处理/无死代码/模块边界/可读性 | 干净、可读、无冗余 | 长函数、全局状态、复制粘贴、未用代码 |
| **4 安全鲁棒 Security/Robustness** | 全 `?` 占位符、输入校验完备（含 NaN/float/二进制 body、超界 id→4xx 而非 500）、错误体不泄漏 stack、并发安全 | 全占位符 + 校验无洞 + 无 500 | 可注入、校验有洞、某端点 500/崩溃 |
| **5 工程性 Engineering** | README 与实际实现一致、测试断言真实（非 trivial）、前端 wiring 真实（JS 真驱动计时）、可维护 | 文档准、测试硬、wiring 真 | README 空/失真、测试凑数、wiring 假 |

**逐轮评分方式**：读该轮全部产物文件 + 独立重跑 pytest + 用 robustness 探针思路手工抽查 + 对照 JSONL 指标。

---

## 3. 逐轮指标

结果文件 `$TEMP/harness-token-econ-v2.jsonl`；产物目录 `$TEMP/harness-token-econ-70rl51dw/`。

| run | verify | pytest | robust | pro_tok | flash_tok | sub | cost $ | wall s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| normal-1 | 4/5 | 43 | 2/6 | 564,094 | 0 | 0 | 1.02 | 488 |
| normal-2 | 4/5 | 63 | 2/6 | 859,073 | 0 | 0 | 1.54 | 640 |
| normal-3 | 4/5 | 55 | 2/6 | 1,274,686 | 0 | 0 | 2.24 | 651 |
| forced-advanced-1 | 5/5 | 48 | 5/6 | 20,783 | 1,294,680 | 9 | 0.24 | 957 |
| forced-advanced-2 | 5/5 | 46 | 2/6 | 20,725 | 757,544 | 6 | 0.16 | 582 |
| forced-advanced-3 | **5/5** | 43 | **6/6** | 20,679 | 1,111,793 | 9 | 0.21 | 765 |
| forced-advanced-4 | 4/5 | 56 | 2/6 | 20,749 | 983,453 | 9 | 0.19 | 715 |
| forced-advanced-5 | 5/5 | 59 | 5/6 | 19,448 | 1,627,572 | 12 | 0.29 | 1,113 |

**鲁棒性探针（黑盒 HTTP codes，重跑确认）**：`huge_duration`（10^15 s → 应 400）、`huge_id`/`patch_huge_id`/`delete_huge_id`（超界 id → 应 400）、`deep_nested`、`after_alive`。

| run | huge_dur | huge_id | patch_id | delete_id | deep | alive | /6 |
|---|---|---:|---:|---:|---:|---:|---:|
| normal-1/2/3 | 201 ✗ | **500** ✗ | **500** ✗ | **500** ✗ | 400/201 | 200 | 2 |
| forced-advanced-1 | 201 ✗ | 400 | 400 | 400 | 201 | 200 | 5 |
| forced-advanced-2 | 201 ✗ | **500** ✗ | **500** ✗ | **500** ✗ | 201 | 200 | 2 |
| forced-advanced-3 | 400 | 400 | 400 | 400 | 201 | 200 | **6** |
| forced-advanced-4 | 201 ✗ | **500** ✗ | **500** ✗ | **500** ✗ | 201 | 200 | 2 |
| forced-advanced-5 | 201 ✗ | 400 | 400 | 400 | 201 | 200 | 5 |

> 500 = 未捕获异常（`sqlite3` 绑 `>2^63-1` int 抛 `OverflowError`）；201 = 接受荒谬时长。500 响应体经抽查为干净的 `{"error": "internal server error"}`，**无 traceback 泄漏**。

## 4. 专家审阅发现（门控之上）

**F1 · engine 并发竞态是 normal 的系统性缺陷（4 轮全中，normal 3/3 + fa-4）**
- 失败引擎模式：`_advance()` 先检查 `_last_tick is None` 再于 `now - _last_tick` 二次读取，`reset()` 并发置 `None` → TOCTOU → `float - None`。
- 通过轮两种解法：**(a)** 全方法加 `threading.RLock()`（fa-1/2/3）；**(b)** 免锁但 `_transition_time` 初始 `0.0` 永不置 None、`reset()` 不碰它（fa-5）——比加锁更优雅。
- **单 pro agent（normal）3/3 产出无锁竞态 engine；flash 并行子代理 3/5 产出锁/免锁正确版本。** n=3 样本下的规律性发现，措辞谨慎。

**F2 · API 上界校验缺口 = robust 分水岭（fa-3 有、其余无）**
- 唯一 6/6 的 fa-3 在 `int()` 前做**长度预检**（`len(raw) > len(str(2**63-1))` → 400）、`duration_s` 上界 `1e9`、拒绝 chunked、捕获 `RecursionError`、每连接 10s 超时。
- fa-1/fa-5 有 `_MAX_INT64` 上界（id→400）但 duration 上界设成 INT64 max（无效，10^15 照收）；fa-2/fa-4/normal 连 id 上界也没有（`int()` 后直接绑 sqlite → 500）。
- **verify 门（校验 happy-path 400/404/413）覆盖不到这一层**——fa-2 与 fa-3 同 5/5，robust 却 2/6 与 6/6。

**F3 · 前端全部真实 wiring，无悬空引用**
- 抽查 3 轮：app.js 所有 `getElementById` 目标均存在于 index.html（normal-1: 6/6、fa-3: 12/12、fa-5: 11/11）；计时真实由 `setInterval(tick)` 驱动，完成即 `POST /api/sessions`，历史/删除/stats 真实 fetch。

**F4 · 自写测试全绿且可复现（专家独立重跑确认）**
- 独立重跑 fa-3 与 normal-1 各 43 passed。断言密度 2-3/test。normal-1 的 engine 竞态不被其自写测试暴露（单线程），正是门控的 8 线程锤存在的理由。

**F5 · README 全部完整诚实；normal-1 有一处 doc/impl 微不一致**
- normal-1 README 声称「invalid session ids → 400」，但超界（纯数字）id 实际 500。其余 README 均准确，fa-5 甚至诚实标注无 `__main__` 入口。

**F6 · #6 升级关闭下的观察**
- 5/5 advanced 轮均有子代理撞 `max_turns`（`coder`×8、`frontend_design`×10、`coordinator`×12），但并行委托吸收后 verify 仍 5/5。这是 flash 能力边界的直接证据，也是 #6 升级路径存在的理由（本轮基准按设计关闭升级）。

## 5. 专家评分表（5 维 × 0-10，8+ 罕见）

| run | 正确性 | 完整性 | 代码质量 | 安全鲁棒 | 工程性 | **均分** |
|---|---:|---:|---:|---:|---:|---:|
| normal-1 | 6 | 8 | 7 | 4 | 7 | **6.4** |
| normal-2 | 6 | 8 | 7 | 4 | 7 | **6.4** |
| normal-3 | 6 | 8 | 7 | 4 | 7 | **6.4** |
| forced-advanced-1 | 9 | 8 | 8 | 7 | 8 | **8.0** |
| forced-advanced-2 | 9 | 8 | 7 | 4 | 8 | **7.2** |
| forced-advanced-3 | 9 | 9 | 9 | 9 | 9 | **9.0** |
| forced-advanced-4 | 6 | 8 | 6 | 4 | 8 | **6.4** |
| forced-advanced-5 | 9 | 8 | 8 | 7 | 8 | **8.0** |
| **组均** | | | | | | normal **6.4** / advanced **7.7** |

**逐轮评分理由**（一句话）：
- **fa-3 = 9.0（唯一 9 分，无满分）**：verify 5/5 + robust 6/6 双满分，id 长度预检/duration 上界/chunked 拒绝/RecursionError/超时全齐，前端最深（dial+阶段循环+mark），README 精确诚实。教科书级。
- **fa-1 / fa-5 = 8.0**：verify 5/5、engine 正确（锁 / 免锁优雅设计）、id 上界正确（400）；唯一真缺陷是 duration 上界失效 → 接受 10^15。
- **fa-2 = 7.2**：verify 5/5 但 id `int()` 无预检 → 500（robust 2/6），Security 4 拉低。
- **normal-1/2/3 = fa-4 = 6.4**：engine 竞态（正确性 6）+ api 无上界（安全 4）；storage/api 其余结构干净、无 stack 泄漏、前端真实、README 完整——「扎实但未加固」。

## 6. Token 经济结论

- **PRO-TOKEN REDUCTION（clean 轮）97.7%**：normal 均值 899,284 → advanced 均值 20,477（中位数 859,073 → 20,725）。比 v1 的 96.6% 略升——更难任务让 normal 涨到 564k–1.27M，advanced 的 root 协调只耗 ~20k（σ≈576，极稳定）。
- **成本**：normal 均值 $1.60 → advanced $0.22，**省 86.3%**。
- **质量**：verify 组均 normal 4.0/5 vs advanced 4.8/5；robust 组均 normal 2.0/6 vs advanced 4.0/6；自写 pytest 两组均绿。**「省 97.7% pro token 且质量不降」在更难任务 + 更严门控下成立，且 advanced 质量还略占优**（含一个 6/6 的 fa-3）。
- **样本**（#7）：advanced n=5 后降幅区间收窄（pro_tok σ 从 normal 的 ±357k 降到 advanced ±576，中位数/均值几乎重合）——v1 的「3 轮波动 2.5×」疑虑在 n=5 下显著缓解。

## 7. 局限

- normal n=3、advanced n=5，单任务（pomodoro）单模型对（pro/flash）——engine 竞态「normal 全中」是 3 样本观察，非强声明。
- 专家评分主观性由 rubric 锚点 + 一句理由约束，可复核。
- 500 响应体虽无 traceback，但「超界 id → 500 而非 400」仍是契约违约（robust 探针判 fragile）。
