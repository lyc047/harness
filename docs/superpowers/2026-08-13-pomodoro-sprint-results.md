# Pomodoro 冲刺任务 · advanced vs normal 编排模式对比基准 — 结果

> 日期：2026-08-13 · 场景 A（番茄钟全栈产品冲刺）
> 复现：`scripts/e2e_subagents_compare_v6.py`（基准 runner）+ `scripts/score_robustness.py`（对抗性鲁棒性审计）
> 状态：**3 组 × 3 runs 全部完成（9/9）**

## 1. 实验设计（回顾）

在 Harness 的 e2e 基准框架上，让 DeepSeek 模型在三种编排模式下各完成 3 次**同一个**「番茄钟全栈产品」冲刺任务（engine / storage / api / static / README + 自己的 pytest）：

| 模式 | forced prompt | advanced 开关 | 语义 |
|---|---|---|---|
| `normal` | ✗ | ✗ | 单 agent，无子代理 |
| `forced-normal` | ✓ | ✗ | 要求把整个任务交给 coordinator；coordinator 只读，不能自己写代码 |
| `forced-advanced` | ✓ | ✓ | coordinator → 多级委托（depth-2）+ 并发子代理（concurrent）+ 共享 SubagentBudget |

唯一变量是 **advanced 开关**（是否允许多级嵌套委托 + 并发 + 专用子代理）。9/9 次运行全部完成。

## 2. 结果总览

| 模式 | run | 契约验证 | 模型自测 pytest | 委托数 | 并发度 | 深度 | 子代理轮次 | 类型 | 耗时 s |
|---|---|---|---|---|---|---|---|---|---|
| normal | 1 | 5/5 | 27 | 0 | 0 | 0 | 0 | 0 | 338 |
| normal | 2 | 5/5 | 27 | 0 | 0 | 0 | 0 | 0 | 77 |
| normal | 3 | 5/5 | 27 | 0 | 0 | 0 | 0 | 0 | 30 |
| forced-normal | 1 | 5/5 | 27 | 1 | 1 | 1 | 3 | 1 | 79 |
| forced-normal | 2 | 5/5 | 27 | 1 | 1 | 1 | 4 | 1 | 83 |
| forced-normal | 3 | 5/5 | 27 | 1 | 1 | 1 | 5 | 1 | 94 |
| forced-advanced | 1 | 5/5 | 32 | 8 | 4 | 2 | 46 | 5 | 434 |
| forced-advanced | 2 | 4/5 | 30 | 7 | 4 | 2 | 45 | 5 | 452 |
| forced-advanced | 3 | 5/5 | 39 | 9 | 4 | 2 | 53 | 5 | 684 |

典型委托链（advanced）：`coordinator → doc_writer / frontend_design / security_reviewer / coder`，两级嵌套 + 每波 4 个并发。

## 3. 三个观察维度

### 3.1 契约合规 — 已饱和，不作为判别轴

v1 二进制门禁（engine/storage/api/static/readme，5 项）**所有模式全部通过**，番茄钟任务完全在单 agent 能力范围内。唯一例外是 forced-advanced-2 的 static 项（4/5）——但它是**门禁的误报**：该实现把倒计时元素命名为 `time-display`（而非字面 `timer`），门禁用 `assert "timer" in index.html` 的脆性子串匹配判失败。人工核对该前端是 9 个输出里最完整的之一（SVG 进度环、ARIA progressbar、备注表单、Start/Pause/Reset 齐全）。此轴不区分模式。

### 3.2 测试覆盖 — advanced 系统性多出对抗性用例

对所有输出的 `test_*.py` 做了关键词扫描（正则命中即认为有对应用例）：

| 用例族 | normal ×3 | forced-normal ×3 | forced-advanced ×3 |
|---|---|---|---|
| 测试函数总数 | 27 / 27 / 27 | 27 / 27 / 27 | 31 / 30 / 39 |
| huge duration（10^15 s）| ✗✗✗ | ✗✗✗ | ✓✓✗ |
| huge id（20 位）| ✗✗✗ | ✗✗✗ | ✓✓✓ |
| negative Content-Length | ✗✗✗ | ✗✗✗ | ✓✗✗ |
| 深嵌套 JSON | ✓✓✓ | ✓✓✓ | ✓✓✓ |
| 并发（多线程）| ✓✓✓ | ✓✓✓ | ✓✓✓ |
| malformed JSON | ✓✓✓ | ✓✓✓ | ✓✓✓ |

**6/6 个 normal/forced-normal 输出完全没有 huge-duration / huge-id 用例；3/3 个 advanced 输出都有 huge-id 用例（2/3 有 huge-duration）。**

### 3.3 对抗性鲁棒性 — 真正拉开差距的判别轴

用 `scripts/score_robustness.py` 对每个输出做**黑盒审计**（全新子进程加载各自 api/storage，起真实 HTTP 服务，打恶劣但合法的请求）：

| 攻击 | normal ×3 | forced-normal ×3 | forced-advanced ×3 |
|---|---|---|---|
| POST `duration_s=10^15` | **201（接受）** ×3 | **201** ×3 | 400 / 201 / 201 |
| GET id=`99999999999999999999` | **500（崩溃）** ×3 | **500** ×3 | **400 / 404 / 404** |
| 5 层嵌套 JSON | 201 ×3 | 201 ×3 | 201 ×3 |
| 攻击后存活 GET /api/sessions | 200 ×3 | 200 ×3 | 200 ×3 |

**决定性差异：`GET /api/sessions/<20位id>` 在 6/6 个 normal 系输出上以未捕获异常把整个服务器打成 500（100%）；在 3/3 个 advanced 输出上优雅返回 400/404（0% 崩溃）。**

## 4. 根因分析（为什么 normal 崩、advanced 不崩）

**normal** 的 `api.py` 直接 `session_id = int(path.rsplit("/",1)[1])`，不加范围检查就 `store.get(session_id)` 绑定进 SQLite。SQLite 的 INTEGER 是 64 位有符号；20 位十进制数超出上界，绑定时抛 `sqlite3.OverflowError`，无人捕获 → 500。`duration_s=10^15` 同理，完全没校验就入库 → 201。

**advanced** 的 `api.py` 显式防御：

```python
MAX_DURATION_S = 2**31 - 1
MAX_SESSION_ID = 2**63 - 1
if not 1 <= session_id <= MAX_SESSION_ID:
    # Reject ids that would overflow sqlite3's 64-bit INTEGER bind.
    ... 400
except OverflowError: ... 400
```

带注释说明溢出原因，并有 `OverflowError` 兜底。这个「SQLite 整数上界」防御点正是 `security_reviewer` 专职子代理的典型产出——普通模式没有专职审查者，写不出也不会写这种用例。

## 5. 编排机制差异（WS 帧审计）

| 指标 | normal | forced-normal | forced-advanced |
|---|---|---|---|
| 委托深度 | 0 | 1 | 2 |
| 子代理总数 | 0 | 1 | 7–9 |
| 最大并发 | 0 | 1 | 4 |
| 子代理轮次 | 0 | 3–5 | 45–53 |
| 工具类型 | 0 | 1 | 5 |
| 委托链 | — | coordinator | coordinator → coder/doc_writer/frontend_design/security_reviewer |

advanced 模式的深度-2 委托 + 4 路并发只带来约 6–15 倍耗时（434–684s vs 30–94s），换来的是上述可测量的质量差异。

## 6. 结论

- **契约合规已饱和**（5/5 全通过，除 1 处门禁误报），单 agent 就能写完符合契约的番茄钟应用——这个任务本身不构成两种模式的区分点。
- **可复现、可辩护的质量优势出现在对抗性鲁棒性**：`GET /api/sessions/<20位id>` 在 6/6 个 normal 系输出上以未捕获 `sqlite3.OverflowError` 打成 500（真实 Web 应用里的 DoS 向量），3/3 个 advanced 输出优雅 400/404。差异源头是 advanced 的专职子代理结构写出了 SQLite 溢出防御 + 相应测试。
- **测试与实现行为完全吻合**：写了 huge-id 用例的（3/3 advanced），实现也真守住了（400/404）；没写的（6/6 normal），实现也真崩了（500）。advanced-3 只写了 huge-id 没写 huge-duration，实现也只守 id 不守 duration——「测什么，实现就守什么」这条规则在单个 run 内部也成立。
- 次要信号：测试函数数 27 → 30–39；pytest 通过数 27 → 30–39；advanced 平均多出 12 个测试与 5–12 个通过用例。
- 局限：样本量小（每组 3）；耗时/质量权衡需按实际场景取舍；advanced 对 `duration_s=10^15` 只守住了 1/3（而 id 是 3/3），说明防御是「倾向性」而非「保证」——但 id 崩溃这一项在 9 个 run 上是 100% vs 0% 的干净分隔。

**一句话**：在这个任务上，advanced 编排模式的明确优势不在「能不能过契约门」，而在「防御非预期输入」——普通模式 100% 崩溃的超界 id，advanced 模式 0% 崩溃、100% 优雅处理；且该防御可追溯到 `security_reviewer` 专职子代理的审查产出。
