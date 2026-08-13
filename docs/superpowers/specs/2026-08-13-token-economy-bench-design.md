# Token-Economy 质量验证基准 — 设计

> 日期：2026-08-13
> 场景：子代理省高端 LLM token + 质量不降级的可辩护验证
> 复现入口：`scripts/e2e_token_economy.py`（新，实现后）
> 前置：主智能体 = `deepseek-v4-pro`（新 key），子代理 = `deepseek-v4-flash`（旧 key，`HARNESS_SUBAGENT_API_KEY`）——上轮已配好并验证

## 1. 背景与目标

上一轮 pomodoro 冲刺基准（advanced vs normal）的结论：**契约轴饱和**（5/5 全过），质量优势只在**对抗鲁棒性**这一个维度干净分隔（超界 id 崩溃 6/6 vs 0/3）。硬冲"质量明显强"可复现性差。

本轮新增了**每子代理独立 LLM 账户**（主=pro/新 key，子=flash/旧 key）。因此本基准的主结论转向可复现、可辩护的 **token 经济性**：

> **在基础多模块任务上，forced-advanced（pro 只做协调、flash 子代理干基础活）对比 normal（pro 全包），pro token 与上下文显著下降，质量门禁不降级。**

质量作为佐证轴（鲁棒性审计 + 测试覆盖 + verify 门禁，历史显示 advanced ≥ normal）。

## 2. 成功判据

- **主 claim**：advanced 的 pro_tokens（input+output）显著低于 normal，目标 **≥50% 降幅**；advanced 的 pro_input_tokens（上下文占用）显著更小。
- **委托证据**：advanced 的 flash_tokens > 0 且 subagent_runs ≥ 1（基础活确实下放到了便宜 API）。
- **质量不降级**：verify_pass 两组均 5/5；pytest_passed 不降；robust_pass advanced ≥ normal。
- 输出含每组 n、均值、方差，可判读。

## 3. 实验设计

### 3.1 任务（复用 pomodoro 冲刺）

`SPRINT_TASK`（`scripts/e2e_subagents_compare_v6.py` 中）——engine/storage/api/static/tests/README 共 10 文件。选择理由：

- 多模块 → 天然可分派给多个专职子代理（coder / frontend_design / doc_writer / security_reviewer）。
- **flash 单独就能过 5/5 门禁**（上轮 9/9 全过）→ "质量不打折扣"的底线安全。
- normal 跑在 pro 上（≥ flash）→ 门禁也稳。
- 门禁现成：`scripts/pomodoro_verify_template.py`（5 项契约检查）+ 模型自测 pytest + `scripts/score_robustness.py`（对抗鲁棒性审计）。

### 3.2 组别与跑量

| 组 | forced prompt | advanced | 语义 |
|---|---|---|---|
| `normal` | ✗ | ✗ | 单 agent（pro），无子代理 |
| `forced-advanced` | ✓ | ✓ | root(pro) → coordinator(flash) → 嵌套+并发专职子代理(flash) |

- 每组 **3 轮**（可调：`HARNESS_COMPARE_RUNS`）。
- 可恢复：每轮结束即追加 JSONL（`$TEMP/harness-token-econ-results.jsonl`），进程死亡/重启后 resume 跳过已记录轮次（复用 v6 模式）。

### 3.3 编排语义（forced-advanced）

- root（pro）：`delegate_to_coordinator` 一次，把全任务交给 coordinator，自己不写文件。
- coordinator（flash，只读工具）：拆解任务，并行派给 coder/frontend_design/doc_writer/security_reviewer，最后只读核查。
- 嵌套深度 ≤ 2，子代理并发（`concurrent=True`），共享 `SubagentBudget`。
- coordinator YAML 由基准脚本运行时写入 `skills/subagents/coordinator.yaml`，跑完清理（与 v6 一致）。

## 4. Token 计量机制

### 4.1 为什么不能靠 trace / 流式属性

- `harness.trace.jsonl` 的 `model_call` 事件**不含 usage**。
- DeepSeek 流式响应**不填 `stream.usage`**（SDK 属性为 None）。
- 实测：DeepSeek 把 usage 放在**流式最后一个 chunk** 的 `chunk.usage` 上（OpenAI 兼容行为）。

### 4.2 provider 记账（核心改动）

`OpenAICompatProvider` 新增（生产级 token 计账，不改流式行为）：

```python
def __init__(self, ..., track_usage: bool = False):
    ...
    self.track_usage = track_usage
    self.usage_log: list[dict[str, Any]] = []  # {model, prompt_tokens, completion_tokens, reasoning_tokens}

def _record_usage(self, model: str, usage: Any) -> None:
    if not self.track_usage:
        return
    self.usage_log.append({
        "model": model,
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "reasoning_tokens": (getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0),
    })
```

- `complete()`：`resp = await self._request(...)` 后 `self._record_usage(model or self.model, getattr(resp, "usage", None))`。
- `stream()`：chunk 循环内若 `chunk.usage` 非空且本流未记录过 → 记录一次（用局部 flag 防重复）。

### 4.3 进程内 runner 与归因

- 每轮全新 `CoreStack`（全新 provider 实例）→ 每轮 usage_log 天然独立，**归因按 provider 实例**（不按 model 字符串，防子代理意外继承 pro provider 时混账）。
- `build_core_stack` 需加 **`subagent_provider` 注入缝**（对称于现有 `provider` 缝），否则无法传入 flash 记账实例。

## 5. 改动点

### 5.1 `src/harness/llm/openai_compat.py` — provider 记账

见 §4.2。加 `track_usage` 参数 + `usage_log` + `_record_usage`，`complete()`/`stream()` 各记一次。

### 5.2 `src/harness/core/compose.py` — subagent_provider 缝

```python
async def build_core_stack(
    settings, *, store=None, provider=None, subagent_provider: LLMProvider | None = None, ...
) -> CoreStack:
    ...
    if subagent_provider is None and settings.subagent_api_key:
        subagent_provider = get_provider(settings.replace(...))  # 现有逻辑兜底
    ...
```

`CoreStack.subagent_provider` 字段已存在（上轮加了），只补注入缝。

### 5.3 `scripts/e2e_token_economy.py`（新）

- **入口**：`load_dotenv(.env)`；缺 `DEEPSEEK_API_KEY` → exit 2。
- **构建两个 provider**（均 `track_usage=True`）：
  - pro：`OpenAICompatProvider(model="deepseek-v4-pro", api_key=<DEEPSEEK_API_KEY>)`
  - flash：`OpenAICompatProvider(model=<settings.subagent_model or "deepseek-v4-flash">, api_key=<settings.subagent_api_key>)`
- **每轮流程**：
  1. 写 coordinator.yaml（复用 v6 的 `COORDINATOR_YAML_TEXT`）。
  2. `out_dir = tmp/<label>-<i>`。
  3. 建栈：`build_core_stack(settings, provider=pro, subagent_provider=flash, prompt=_auto_approve)`。
  4. advanced 时：`add_example_subagents(stack, advanced=True, subagent_model="", subagent_provider=stack.subagent_provider, on_event=_count_subagent)`。
  5. `await runner.run(stack.agent, prompt)`（forced 提示复用 v6 的 `_prompt_forced` / `_prompt`）。
  6. 归集：`pro.usage_log` / `flash.usage_log` 求和 → 各类 token；`_count_subagent` 数 `SubagentRunStart` → `subagent_runs`；`wall = 实测秒`。
  7. 质量：复制 verify 模板跑 `verify_impl.py`（`VERIFY_PASS (\d)/5`）；跑 `pytest -q out_dir`；调 `score_robustness.score(label, i, out_dir)`。
  8. 追加 JSONL 记录。
- **超时/失败兜底**：单轮超时或 API 失败 → 按已有文件 salvage（verify/pytest），usage 记 0，继续跑（复用 v6 的 `_salvage_run` 精神）。
- **汇总**：每组输出均值±方差：pro in/out、flash in/out、cost_pro/cost_flash/total、wall、subagent_runs、verify_pass、pytest、robust_pass；主 claim 输出 pro-token 降幅 %。
- `_auto_approve`：`async def _auto_approve(tc) -> str: return "y"`（进程内审批全放行）。

### 5.4 定价（成本轴）

来自 cc-switch `model_pricing` 表（与 DeepSeek 公开发布价一致，做成脚本常量 + 注释可改）：

| 模型 | 输入 $/MTok | 输出 $/MTok |
|---|---|---|
| deepseek-v4-pro | 1.68 | 3.36 |
| deepseek-v4-flash | 0.14 | 0.28 |

价差恰好 **12×**。成本 = in×in_price/1e6 + out×out_price/1e6。

### 5.5 测试（质量门）

- `tests/test_openai_compat.py`（现有文件，新增用例）：`OpenAICompatProvider(track_usage=True)` 用 fake client——
  - `complete()` 记录 usage（fake resp 带 `.usage`）；
  - `stream()` 从带 `.usage` 的最后一个 chunk 记录一次（fake 流 chunk），且不重复记录；
  - `track_usage=False`（默认）不记录。
- `tests/test_compose.py`（新）：`build_core_stack` 传 `subagent_provider` 时使用传入实例；不传时按 `HARNESS_SUBAGENT_API_KEY` 构建；都不满足时 None（`CoreStack.subagent_provider` 字段上轮已存在）。
- `tests/test_token_economy_script.py`（新）：脚本的纯函数（usage 求和、成本计算、汇总表格式化、pro-token 降幅计算）无网络跑。
- 质量门：`uv run ruff check . && uv run mypy src && uv run pytest -q`。

## 6. 指标与报告

### 6.1 每轮指标（JSONL record）

```json
{"mode": "normal|forced-advanced", "run": 1,
 "out": "<dir>",
 "metrics": {
   "seconds": 123.4,
   "subagent_runs": 0,
   "pro_input_tokens": 12345, "pro_output_tokens": 678,
   "flash_input_tokens": 0,   "flash_output_tokens": 0,
   "pro_tokens": 13023, "flash_tokens": 0,
   "cost_usd": 0.0,
   "verify_pass": 5, "pytest_passed": 27, "robust_pass": 4,
   "chains_hint": ["coordinator -> coder"]  // 可选：on_event 记录的 agent 名序列
 },
 "reason": "ok"}
```

### 6.2 汇总输出

- 每组 n=3：`pro_tokens` 均值±SD、`flash_tokens`、`cost_usd`、`wall`、`verify_pass`、`pytest_passed`、`robust_pass`。
- 主 claim 行：`pro-token 降幅 = 1 - mean(advanced.pro_tokens)/mean(normal.pro_tokens)`。
- 结果文档：`docs/superpowers/2026-08-13-token-economy-bench-results.md`（中文，表格 + 结论 + 局限）。

## 7. 验证方式

1. 质量门全绿（ruff + mypy + pytest）。
2. 单轮冒烟：`HARNESS_COMPARE_RUNS=1` 跑 normal 一轮 + forced-advanced 一轮，检查：JSONL 记录含正数 pro/flash token、verify 5/5、robustness 正常。
3. 全量：`HARNESS_COMPARE_RUNS=3`，约 1-1.5h，完成后自动出汇总。
4. 若 pro-token 降幅 < 50% 或质量降级 → 检查原因（委托是否发生 / flash 是否拖垮质量），如实写入结果文档，不伪造结论。

## 8. 风险与注意点

- **R1 委托不触发**：forced prompt 已在上轮证明能稳定触发；若某轮 root 不委托，flash_tokens≈0 → 该轮按"委托失败"标注并在汇总中如实呈现。
- **R2 归因**：按 provider 实例分账，绝不按 model 字符串；两 provider 的 model 名不同（pro/flash）作为交叉校验。
- **R3 stream 无 usage 的异常调用**：若某流末 chunk 无 usage（罕见），该调用跳过（不记），成本/token 略低估；汇总注明。
- **R4 成本**：价格为常量假设，附来源注释；脚本暴露 `PRICING` dict 可改。
- **R5 时长**：2 组 × 3 轮 ≈ 1-1.5h；可恢复 JSONL 防进程死亡丢数据。
- **R6 不污染仓库**：coordinator.yaml 与产物都在 temp/运行时目录，结束后清理；results JSONL 在 $TEMP。
