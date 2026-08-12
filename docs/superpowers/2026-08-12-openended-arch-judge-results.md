# 开放工程基准:LLM 盲评对比结果(normal / advanced / forced depth-2)

日期:2026-08-12
实验:scripts/e2e_subagents_compare_v4.py + scripts/arch_judge.py
生成模型:deepseek-v4-flash · 评分模型:deepseek-v4-flash(每份 3 采样取每维中位数)

---

## 1. 结论摘要

1. **三种模式的输出质量在统计上没有可分辨的差异。** 全报告盲评下:
   normal **54**/60(n=2)、advanced **52**/60(n=3)、depth2 **52**/60(n=3)。组间差 ~2 分,judge 自身的组内方差(49–57)足以吞没它。
2. **之前"depth2 领先"是 8k 截断造成的假象。** 截断修复(8000→30000 字符)后排名反转:normal 反而最高。原因见 §4——depth2 报告恰好更短,更多内容躲过了截断,Completeness 被虚高。
3. **强制 depth-2 的执行结构差异是真实且可复现的**:3/3 触发 coordinator→doc_writer 两级链(depth=2、并发=2、2 种 agent、2 个 run 还自发做了 web 搜索),而 normal/advanced 全部止步 depth=1。但这份结构没有换来更好的报告,只换来更长的耗时。
4. **务实建议**:benchmark 这类任务,单 agent + 一次 doc_writer 委派已经足够;强制链式分工带来的成本(墙钟 ~2×)没有质量回报。depth-2 的价值应当去「子问题可并行、需要隔离研究」的任务里找,而不是「一个 agent 从头写到尾」的报告型任务。

---

## 2. 实验设置

- **任务**(开放题,无标准答案):为"实时协作白板平台"设计系统架构——1M DAU、三区域(US+EU+APAC)、协作编辑感知延迟 <200ms、99.95% 可用性、精简预算。要求 6 个固定 section + 8 个子系统((a)–(h)),每个子系统必须给显式 trade-off。
- **三组**(每组 n=3):
  - **normal**:默认模式,无嵌套/并发开关。
  - **advanced**:`set_advanced`(允许嵌套 delegates + 并发 + budget)。
  - **depth2**:advanced + 用户提示词强制 `delegate_to_coordinator` 把整个任务交给一个无写权限的协调者,后者再把手稿交给 doc_writer。
- **盲评**:报告脱敏(不含模式标签)、模式混合随机顺序(seed=20260812)、每份 3 次独立打分取每维中位数;6 维 rubric × 10 分 + TOTAL /60。

---

## 3. 执行结构对比(WS 帧追踪)

| run | 委派数 | 波数 | 并发峰值 | 深度 | agent 类型数 | web搜索 | bash | 子agent轮数 | 墙钟(s) | 委派链 |
|-----|----|----|----|----|----|----|----|----|----|----|
| normal-1 | 1 | 1 | 1 | 1 | 1 | 0 | 4 | 3 | 112.6 | doc_writer |
| normal-2 | — | — | — | — | — | — | — | — | **TIMEOUT(900s)** | (报告已生成但被丢弃) |
| normal-3 | 2 | 1 | 1 | 1 | 1 | 0 | 10 | 15 | 236.8 | doc_writer |
| advanced-1 | 1 | 1 | 1 | 1 | 1 | 0 | 6 | 11 | 218.5 | doc_writer |
| advanced-2 | 1 | 1 | 1 | 1 | 1 | 0 | 5 | 8 | 169.8 | doc_writer |
| advanced-3 | 1 | 1 | 1 | 1 | 1 | 0 | 2 | 3 | 81.0 | doc_writer |
| depth2-1 | 2 | 1 | **2** | **2** | **2** | **3** | 8 | 16 | 421.5 | coordinator \| coordinator→doc_writer |
| depth2-2 | 2 | 1 | **2** | **2** | **2** | **2** | 3 | 10 | 251.2 | coordinator \| coordinator→doc_writer |
| depth2-3 | 2 | 1 | **2** | **2** | **2** | 0 | 4 | 10 | 204.3 | coordinator \| coordinator→doc_writer |

要点:

- **depth2 的链条结构 3/3 完全符合设计**:coordinator(只读/只搜,无写权限)→ doc_writer(写文件)。两级、并发 2、两种 agent。
- **normal 与 advanced 行为趋同**:即便开了 advanced,模型在"写一份报告"的任务上也不觉得需要嵌套分工,始终只派一次 doc_writer(depth=1)。这说明 advanced 的额外能力在此任务上没有用武之地——不是能力失效,是任务不需要。
- **唯一自发使用 web 搜索的是 depth2**:coordinator 的角色提示(只读工具+web_search)鼓励它先研究再写。normal/advanced 完全本地生成。
- **成本**:depth2 中位墙钟 ~251s(204–421s),advanced 中位 ~170s(81–218s),normal 112–237s。强制链最慢还最不稳定。
- **normal-2 超时**:17KB 报告其实已写出,但 run 在 900s 内没收尾被丢弃 → normal 组实际 n=2。属诚实记录的实验损失。

---

## 4. LLM 盲评结果与"截断修复"的教训

### 4a. 修复前(MAX_JUDGE_CHARS=8000)

| 组 | n | TOTAL中位 | Req | Arch | Trade-off | Completeness | Deploy | Risk |
|----|---|----|----|----|----|----|----|----|
| normal | 2 | 43.5 | 8.5 | 7.5 | 9.0 | **5.5** | 7.0 | **6.5** |
| advanced | 3 | 43.0 | 9.0 | 8.0 | 9.0 | **5.0** | 7.0 | **6.0** |
| depth2 | 3 | **45.0** | 9.0 | 8.0 | 9.0 | **6.0** | 6.0 | **6.0** |

表面上 depth2 最高。但复核时发现报告普遍 15–22KB,8k 截断点分别落在 (f) Observability / Search & indexing / (d),**后 3 个强制 section(Key tech choices / Risks / Evolution)对 judge 完全不可见**——而这正是 Completeness 和 Risk 的评分依据。

### 4b. 修复后(MAX_JUDGE_CHARS=30000,覆盖全部报告)

| 组 | n | TOTAL中位 | Req | Arch | Trade-off | Completeness | Deploy | Risk |
|----|---|----|----|----|----|----|----|----|
| normal | 2 | **54.0** | 9.0 | 9.0 | 10.0 | **9.0** | 8.0 | **9.0** |
| advanced | 3 | 52.0 | 9.0 | 9.0 | 9.0 | **9.0** | 8.0 | **9.0** |
| depth2 | 3 | 52.0 | 9.0 | 8.0 | 9.0 | **9.0** | 8.0 | **9.0** |

全部报告都能被完整读到了,Completeness/Risk 从 5–6 回升到 8–9(判分恢复真实)。**排名反转:normal 反超。** 每份的细节见 scripts/arch_judge_results.md。

### 4c. 关键方法论教训

- **截断点与报告长度相关,而报告长度与模式相关 → 系统性偏差。** depth2 报告更短(15–18KB,协调者写得更收敛),advanced 更长(21–22KB)。8k 一刀切截断时,短的吃亏小、长的吃亏大,depth2 的 Completeness 被系统性抬高。**这解释了"depth2 领先"为何是假象。**
- **judge 组内方差(49–57,±4 分)远大于组间差异(~2 分)。** 每份 3 采样取中位数的降噪在单份层面不够强;要分辨这么小的差异需要更多 run 和更多采样。
- **每维中位数的上限效应**:修复后 6 维里 4 维的中位数停在 9–10,顶部挤压,组间在这几维上完全失去分辨力。

---

## 5. 我的人工复核(spec 的 Validation 第 3 条)

读了最高分(advanced-1,57/60)与最低分(normal-1,50/60)全文,抽查判分:

- **判分与内容质量方向一致。** advanced-1 是量化最充分的一份:显式可用性预算拆分(每个组件的 9 个9)、每子系统 trade-off + 被否决备选方案、roadmap 带退出标准与回滚演练。normal-1 结构完整但更薄:备选方案列表更短、roadmap 细节更少——judge 给它的 Completeness=7(全场最低)是站得住的。
- **judge 的分辨力上限**:到了 9–10 区间,judge 的排序开始"挤在一起",区分度让位于随机性。它抓得住"明显更差"(advanced-3=49 也确实是最薄的一份),但分不清"差不多好"的组间差异。
- **结论可信度**:模式的排序变化(8k: depth2>normal>advanced;30k: normal>advanced=depth2)两次都落在方差内,**都不能当作"某模式更好"的证据**;能确定的只有"质量等价 + 执行结构差异真实存在 + 强制链更慢"。

---

## 6. 对 Harness 编排设计的启示

1. **报告/写作型任务:强制深链是负资产。** 它不提升质量,还让墙钟翻倍、引入一个无写权限的中间人(coordinator)与写作子 agent 之间的一次多余交接。
2. **深度链的价值场景在别处**:真正的价值是"把可并行的子问题隔离研究、最后合流"——例如多来源调研、代码库分片审计。当前 benchmark 的任务形状(一个连续文本的架构报告)天然不适合演示 depth-2 的收益。
3. **benchmark 基建本身被验证有效**:链追踪、盲评协议、截断缺陷暴露与修复——这套管线在 future work 里可以直接复用,换一个"更适合深链"的任务即可。

---

## 附:复现方式

```bash
# 生成(三模式 × 3,含链追踪 + 盲评)
HARNESS_COMPARE_RUNS=3 uv run python scripts/e2e_subagents_compare_v4.py
# 仅对已有报告重评(30k 上限),不重新生成
uv run python - <<'PY'
# 见会话中使用的重评脚本:复用 arch_judge.judge_report,samples=3,seed 20260812
PY
# 单测
uv run ruff check . && uv run mypy src && uv run pytest -q   # 255 passed
```

产物:8 份报告在 `%TEMP%/harness-arch-*/`;每份逐维分数在 scripts/arch_judge_results.md;旧版(8k,已修正)备份在同目录同名的 `arch_judge_results_8k.md`。
