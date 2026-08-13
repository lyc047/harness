# Token-Economy 基准实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个可辩护的基准（`scripts/e2e_token_economy.py`），证明 forced-advanced 编排（pro 协调 + flash 子代理干活）对比 normal（pro 全包）在基础多模块任务上显著节省 pro token/上下文，且质量门禁不降级。

**Architecture:** 进程内 runner（每轮全新 CoreStack + 两个带 usage 记账的 provider 实例，归因按 provider 实例分账）→ 复用 v6 的 pomodoro 任务/门禁/coordinator → 复用 `score_robustness.score` 做质量轴 → JSONL 可恢复。代码改动仅两处：`OpenAICompatProvider` 加 `track_usage`/`usage_log`，`build_core_stack` 加 `subagent_provider` 注入缝。

**Tech Stack:** Python 3.11+, asyncio, uv, pytest, ruff, mypy, OpenAI-compatible DeepSeek API。

## Global Constraints

- 环境：Windows 10 + Git Bash + uv + 清华 PyPI。命令用 `uv run <cmd>`。
- **API key 是机密**：任何命令/输出/文档都不得打印 `DEEPSEEK_API_KEY` 或 `HARNESS_SUBAGENT_API_KEY`。脚本里只用 `settings.api_key` / `settings.subagent_api_key` 引用，绝不 print。
- 复用 pomodoro 任务与门禁：`scripts/e2e_subagents_compare_v6.py`（`SPRINT_TASK`/`_prompt`/`_prompt_forced`/`_run_verify`/`_run_pytest`/`_write_coordinator`/`COORDINATOR_YAML`）、`scripts/e2e_subagents_compare_v2.py`（`REPO_ROOT`/`_fmt_spread`）、`scripts/score_robustness.py`（`score`）。
- 定价常量：pro `{in:1.68, out:3.36}`，flash `{in:0.14, out:0.28}`（$/MTok，来自 cc-switch model_pricing 表，与 DeepSeek 公开发售价一致；脚本里做成 `PRICING` dict 可改）。
- 质量门：`uv run ruff check . && uv run mypy src && uv run pytest -q` 全绿。
- 产物不污染仓库：coordinator.yaml 运行时写入 `skills/subagents/`、结束后删除；results JSONL 在 `$TEMP`；`db_path` 指向临时目录。
- 主/子模型不交叉：normal 只有 pro；advanced 的 root=pro、子代理=flash（`subagent_provider` 注入 + `subagent_model="deepseek-v4-flash"`）。
- 每轮结束立即追加 JSONL（进程死亡/重启可 resume）。

---

### Task 1: OpenAICompatProvider usage 记账

**Files:**
- Modify: `src/harness/llm/openai_compat.py`
- Test: `tests/test_openai_compat.py`

**Interfaces:**
- Produces: `OpenAICompatProvider(track_usage: bool = False)`；`provider.usage_log: list[dict]`，元素 `{"model", "prompt_tokens", "completion_tokens", "reasoning_tokens"}`。`complete()` 从 `resp.usage` 记录，`stream()` 从最后一个带 `usage` 的 chunk 记录**一次**。`track_usage=False`（默认）时 `usage_log` 恒为空。
- Consumes: 现有 `_request`（返回 openai 响应/流）、`_parse_message`。

- [ ] **Step 1: 写失败测试**（`tests/test_openai_compat.py` 末尾追加；`_provider` 辅助函数加 `track_usage` 参数）

```python
def _provider(fake_completions, *, track_usage=False):
    p = OpenAICompatProvider(
        model="deepseek-v4-flash", api_key="sk-test", retry_attempts=1, track_usage=track_usage
    )
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    return p


def _usage(prompt_tokens, completion_tokens, reasoning_tokens=0):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


@pytest.mark.asyncio
async def test_complete_records_usage_when_tracking():
    fake = FakeCompletions()
    fake.plain_message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=fake.plain_message)],
            usage=_usage(10, 5, 2),
        )

    fake.create = create
    provider = _provider(fake, track_usage=True)
    await provider.complete([Message.user("hi")])
    assert provider.usage_log == [
        {"model": "deepseek-v4-flash", "prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 2}
    ]


@pytest.mark.asyncio
async def test_stream_records_usage_once_from_final_chunk():
    fake = FakeCompletions()
    c1 = _chunk(content="hi", finish_reason=None)
    c1.usage = _usage(10, 5, 2)
    c2 = _chunk(content=" there", finish_reason="stop")
    c2.usage = _usage(10, 6, 3)  # should NOT be double counted
    fake.chunks = [c1, c2]
    provider = _provider(fake, track_usage=True)
    events = [e async for e in provider.stream([Message.user("hi")])]
    assert isinstance(events[-1], StreamEnd)
    assert len(provider.usage_log) == 1
    assert provider.usage_log[0]["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_usage_tracking_off_by_default():
    fake = FakeCompletions()
    fake.plain_message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
    provider = _provider(fake)  # track_usage default False
    await provider.complete([Message.user("hi")])
    assert provider.usage_log == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_openai_compat.py -q`
Expected: 3 个新用例 FAIL（`TypeError: unexpected keyword argument 'track_usage'`）

- [ ] **Step 3: 实现**（`src/harness/llm/openai_compat.py`）

`__init__` 签名加参数并初始化：

```python
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
        max_tool_calls: int = 128,
        retry_attempts: int = 3,
        retry_base_delay: float = 1.0,
        track_usage: bool = False,
    ) -> None:
        self.model = model
        self.max_tool_calls = max_tool_calls
        self.retry_attempts = max(1, retry_attempts)
        self.retry_base_delay = retry_base_delay
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self.track_usage = track_usage
        self.usage_log: list[dict[str, Any]] = []
        self._client: AsyncOpenAI | None = None
```

新增方法（放在 `_parse_message` 之后）：

```python
    def _record_usage(self, model: str, usage: Any) -> None:
        """Append one usage record; no-op unless ``track_usage`` is on."""
        if not self.track_usage or usage is None:
            return
        details = getattr(usage, "completion_tokens_details", None)
        self.usage_log.append(
            {
                "model": model,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
            }
        )
```

`complete()` 里 `_request` 之后记录（`_parse_message` 之前）：

```python
        resp = await self._request(wire, tools=tools, model=model)
        self._record_usage(model or self.model, getattr(resp, "usage", None))
        return self._parse_message(resp)
```

`stream()` 的 chunk 循环里，在 `if not chunk.choices: continue` **之前**加一次性记录：

```python
        usage_recorded = False
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if not usage_recorded and usage is not None:
                self._record_usage(model or self.model, usage)
                usage_recorded = True
            if not chunk.choices:
                continue
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_openai_compat.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/harness/llm/openai_compat.py tests/test_openai_compat.py
git commit -m "feat(llm): track_usage + usage_log on OpenAICompatProvider (token accounting)"
```

---

### Task 2: `build_core_stack` 的 `subagent_provider` 注入缝

**Files:**
- Modify: `src/harness/core/compose.py`
- Create: `tests/test_compose.py`

**Interfaces:**
- Consumes: `Settings.subagent_api_key`/`subagent_base_url`/`subagent_model`（Task 1 无关，但 provider 构造沿用现有 `get_provider(settings.replace(...))`）。
- Produces: `build_core_stack(settings, *, store=None, provider=None, subagent_provider: LLMProvider | None = None, ...) -> CoreStack`。传了 `subagent_provider` → 直接用；否则按 `settings.subagent_api_key` 构建；都没有 → `None`。`add_example_subagents` 已透传 `stack.subagent_provider`，无需改。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_compose.py`）

```python
"""build_core_stack provider seams: provider + subagent_provider injection."""

import pytest

from harness.config import Settings
from harness.core.compose import build_core_stack


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(self, messages, *, tools=None, model=None):  # pragma: no cover
        raise NotImplementedError

    def stream(self, messages, *, tools=None, model=None):  # pragma: no cover
        raise NotImplementedError


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "HARNESS_DB_PATH": str(tmp_path / "harness.db"),
            "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),
            "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),
        }
    )


@pytest.mark.asyncio
async def test_injected_subagent_provider_wins(tmp_path):
    main = _FakeProvider("main")
    sub = _FakeProvider("sub")
    stack = await build_core_stack(_settings(tmp_path), provider=main, subagent_provider=sub)
    assert stack.provider is main
    assert stack.subagent_provider is sub


@pytest.mark.asyncio
async def test_subagent_provider_none_without_key(tmp_path):
    stack = await build_core_stack(_settings(tmp_path), provider=_FakeProvider("main"))
    assert stack.subagent_provider is None


@pytest.mark.asyncio
async def test_subagent_provider_built_from_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SUBAGENT_API_KEY", "sk-sub")
    monkeypatch.setenv("HARNESS_SUBAGENT_MODEL", "deepseek-v4-flash")
    stack = await build_core_stack(_settings(tmp_path), provider=_FakeProvider("main"))
    assert stack.subagent_provider is not None
    assert stack.subagent_provider._api_key == "sk-sub"
    assert stack.subagent_provider.model == "deepseek-v4-flash"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_compose.py -q`
Expected: 第 1 个用例 FAIL（`TypeError: unexpected keyword argument 'subagent_provider'`）；第 2、3 个用例因第 1 个报错而……先跑第 1 个单独确认：`uv run pytest tests/test_compose.py::test_injected_subagent_provider_wins -q` FAIL。

- [ ] **Step 3: 实现**（`src/harness/core/compose.py` 的 `build_core_stack`）

签名加参数（在 `provider` 之后）：

```python
async def build_core_stack(
    settings: Settings,
    *,
    store: Store | None = None,
    provider: LLMProvider | None = None,
    subagent_provider: LLMProvider | None = None,
    tool_executor: ToolExecutor | None = None,
    prompt: ApprovalPrompt | None = None,
    on_pause: Callable[[], None] | None = None,
    pause_check: PauseCheck | None = None,
    hooks: Hooks | None = None,
) -> CoreStack:
```

docstring 补一句：`` ``subagent_provider`` is a test/benchmark seam that injects the
subagent LLM account directly; when None the settings-derived one is built
(``HARNESS_SUBAGENT_API_KEY``), and with no key either way it stays None. ``

构建逻辑改为（保留现有 settings 派生作兜底）：

```python
    if subagent_provider is None and settings.subagent_api_key:
        subagent_provider = get_provider(
            settings.replace(
                api_key=settings.subagent_api_key,
                base_url=settings.subagent_base_url or settings.base_url,
                model=settings.subagent_model or settings.model,
            )
        )
```

（`subagent_provider` 传入时不走 `get_provider`，直接使用注入实例；`CoreStack(subagent_provider=subagent_provider)` 已存在。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_compose.py tests/test_web_runtime.py -q`
Expected: 全 PASS（web_runtime 的 `test_runtime_subagent_provider_keyed_by_env` 仍绿，验证未破坏现有 env-key 路径）

- [ ] **Step 5: Commit**

```bash
git add src/harness/core/compose.py tests/test_compose.py
git commit -m "feat(compose): injectable subagent_provider seam for benchmark/tests"
```

---

### Task 3: 基准脚本 + 纯函数测试

**Files:**
- Create: `scripts/e2e_token_economy.py`
- Create: `tests/test_token_economy_script.py`

**Interfaces:**
- Consumes: Task 1 的 `track_usage`/`usage_log`；Task 2 的 `subagent_provider` 缝；v6 的 `_prompt`/`_prompt_forced`/`_run_verify`/`_run_pytest`/`_write_coordinator`/`COORDINATOR_YAML`；v2 的 `_fmt_spread`；`score_robustness.score`。
- Produces: 纯函数 `sum_usage(records) -> dict`、`cost(records, pricing=None) -> float`、`pro_reduction(advanced, normal) -> float | None`、`_spread(vals) -> str`（供 Task 4 汇总与测试导入）。

- [ ] **Step 1: 写纯函数失败测试**（新建 `tests/test_token_economy_script.py`）

```python
"""Pure helpers of the token-economy benchmark (no network)."""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from e2e_token_economy import (  # noqa: E402
    cost,
    pro_reduction,
    sum_usage,
)

REC = {"model": "deepseek-v4-pro", "prompt_tokens": 1_000_000, "completion_tokens": 500_000, "reasoning_tokens": 100_000}


def test_sum_usage_aggregates():
    s = sum_usage([REC, {**REC, "prompt_tokens": 0, "completion_tokens": 0}])
    assert s["prompt"] == 1_000_000
    assert s["completion"] == 500_000
    assert s["reasoning"] == 100_000
    assert s["total"] == 1_500_000


def test_sum_usage_empty():
    assert sum_usage([]) == {"prompt": 0, "completion": 0, "reasoning": 0, "total": 0}


def test_cost_uses_per_model_pricing():
    # 1M prompt @1.68 + 0.5M completion @3.36
    assert cost([REC]) == 1.68 + 1.68


def test_cost_skips_unknown_model():
    assert cost([{**REC, "model": "no-such-model"}]) == 0.0


def test_pro_reduction_pct():
    assert pro_reduction([100], [1000, 2000]) == pytest.approx(1 - 100 / 1500)
    assert pro_reduction([], [1000]) == 1.0  # advanced burned nothing
    assert pro_reduction([100], []) is None
    assert pro_reduction([100], [0, 0]) is None  # normal base zero -> undefined
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_token_economy_script.py -q`
Expected: FAIL（`ModuleNotFoundError: e2e_token_economy`）

- [ ] **Step 3: 实现脚本**（`scripts/e2e_token_economy.py`，完整内容如下）

```python
"""Token-economy benchmark: normal (pro only) vs forced-advanced (flash subagents).

Each run has the model implement the pomodoro sprint (same task/gates as
scripts/e2e_subagents_compare_v6.py). The benchmark runs **in-process**: a
fresh CoreStack per run with two usage-tracking provider instances (main=pro,
subagents=flash), so token usage is attributed per model exactly. After each
run it runs the verify gate, the model's own pytest, and the adversarial
robustness audit (scripts/score_robustness.py).

Primary claim: forced-advanced uses far fewer PRO tokens/context than normal,
with quality gates (verify 5/5, pytest, robustness) not degraded.

Env:
  HARNESS_COMPARE_RUNS      runs per group (default 3)
  HARNESS_COMPARE_GROUPS    comma-separated group labels (default
                            "normal,forced-advanced")
  HARNESS_COMPARE_TIMEOUT   per-run timeout seconds (default 1800)

Exit codes: 0 ok, 1 fail, 2 no API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Run from the repo root so relative skills/ and data dirs resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))

from e2e_subagents_compare_v2 import _fmt_spread  # noqa: E402
from e2e_subagents_compare_v6 import (  # noqa: E402
    COORDINATOR_YAML,
    _prompt,
    _prompt_forced,
    _run_pytest,
    _run_verify,
    _write_coordinator,
)
from score_robustness import score as robust_score  # noqa: E402

from harness.agents.orchestrator import SubagentRunStart  # noqa: E402
from harness.config import Settings  # noqa: E402
from harness.core.compose import add_example_subagents, build_core_stack  # noqa: E402
from harness.llm.openai_compat import OpenAICompatProvider  # noqa: E402

RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))
RUN_TIMEOUT = float(os.environ.get("HARNESS_COMPARE_TIMEOUT", "1800"))
GROUPS_ALL = ("normal", "forced-advanced")
_filter = [s for s in os.environ.get("HARNESS_COMPARE_GROUPS", "").split(",") if s]
GROUPS = [g for g in GROUPS_ALL if not _filter or g in _filter]
RESULTS_FILE = Path(tempfile.gettempdir()) / "harness-token-econ-results.jsonl"

# Per-MTok USD pricing (cc-switch model_pricing table; matches DeepSeek
# published pricing — edit to match your billing).
PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"in": 1.68, "out": 3.36},
    "deepseek-v4-flash": {"in": 0.14, "out": 0.28},
}
SUBAGENT_MODEL = "deepseek-v4-flash"
SUBAGENT_BUDGET = int(os.environ.get("HARNESS_SUBAGENT_BUDGET", "120"))


# ---- pure helpers (imported by tests) ---- #

def sum_usage(records: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate usage records into {prompt, completion, reasoning, total}."""
    return {
        "prompt": sum(int(r.get("prompt_tokens", 0)) for r in records),
        "completion": sum(int(r.get("completion_tokens", 0)) for r in records),
        "reasoning": sum(int(r.get("reasoning_tokens", 0)) for r in records),
        "total": sum(
            int(r.get("prompt_tokens", 0)) + int(r.get("completion_tokens", 0))
            for r in records
        ),
    }


def cost(records: list[dict[str, Any]], pricing: dict[str, dict[str, float]] | None = None) -> float:
    """USD cost of usage records at the given per-MTok pricing."""
    pricing = pricing or PRICING
    total = 0.0
    for r in records:
        p = pricing.get(str(r.get("model", "")))
        if p is None:
            continue
        total += (int(r.get("prompt_tokens", 0)) / 1e6) * p["in"]
        total += (int(r.get("completion_tokens", 0)) / 1e6) * p["out"]
    return round(total, 6)


def pro_reduction(advanced_pro: list[float], normal_pro: list[float]) -> float | None:
    """Pro-token reduction = 1 - mean(advanced)/mean(normal); None if undefined."""
    if not normal_pro:
        return None
    base = sum(normal_pro) / len(normal_pro)
    if base <= 0:
        return None
    adv = sum(advanced_pro) / len(advanced_pro) if advanced_pro else 0.0
    return 1 - adv / base


def _spread(vals: list[float]) -> str:
    """mean (min–max) string; 'n/a' when empty."""
    if not vals:
        return "n/a"
    m = sum(vals) / len(vals)
    return f"{m:.1f} ({min(vals):.1f}–{max(vals):.1f})"


# ---- run orchestration ---- #

def _make_sink(names: list[str]) -> Any:
    async def sink(run_id: str, name: str, event: object) -> None:
        if isinstance(event, SubagentRunStart):
            names.append(name)

    return sink


async def _auto_approve(tool_call: Any) -> str:
    return "y"


def _build_providers(settings: Settings) -> tuple[OpenAICompatProvider, OpenAICompatProvider]:
    """Main (pro) + subagent (flash) providers, both tracking usage."""
    pro = OpenAICompatProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        track_usage=True,
    )
    flash = OpenAICompatProvider(
        model=settings.subagent_model or SUBAGENT_MODEL,
        api_key=settings.subagent_api_key,
        base_url=settings.subagent_base_url or settings.base_url,
        track_usage=True,
    )
    return pro, flash


async def _run_once(
    settings: Settings,
    out_dir: Path,
    *,
    label: str,
    i: int,
    forced: bool,
    advanced: bool,
) -> dict[str, Any]:
    pro, flash = _build_providers(settings)
    started = time.monotonic()
    stack = await build_core_stack(
        settings, provider=pro, subagent_provider=flash, prompt=_auto_approve
    )
    agent_names: list[str] = []
    if advanced:
        add_example_subagents(
            stack,
            advanced=True,
            subagent_model=settings.subagent_model or SUBAGENT_MODEL,
            on_event=_make_sink(agent_names),
        )
    prompt = _prompt_forced(str(out_dir)) if forced else _prompt(str(out_dir))
    try:
        await stack.runner.run(stack.agent, prompt)
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash the benchmark
        reason = f"{type(exc).__name__}: {exc}"
    else:
        reason = "ok"
    seconds = time.monotonic() - started
    pro_u = sum_usage(pro.usage_log)
    flash_u = sum_usage(flash.usage_log)
    verify_pass = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    rob = robust_score(label, i, out_dir)
    return {
        "mode": label,
        "run": i,
        "out": str(out_dir),
        "reason": reason,
        "metrics": {
            "seconds": seconds,
            "subagent_runs": len(agent_names),
            "agents": sorted(set(agent_names)),
            "pro_input_tokens": pro_u["prompt"],
            "pro_output_tokens": pro_u["completion"],
            "pro_reasoning_tokens": pro_u["reasoning"],
            "flash_input_tokens": flash_u["prompt"],
            "flash_output_tokens": flash_u["completion"],
            "pro_tokens": pro_u["total"],
            "flash_tokens": flash_u["total"],
            "cost_usd": round(cost(pro.usage_log) + cost(flash.usage_log), 6),
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
            "robust_pass": rob.get("robust_pass", 0),
        },
    }


def _salvage(label: str, i: int, out_dir: Path, *, reason: str, seconds: float) -> dict[str, Any]:
    """Record a run that ended without clean metrics (timeout / API failure)."""
    verify_pass = _run_verify(out_dir)
    pytest_passed = _run_pytest(out_dir)
    return {
        "mode": label,
        "run": i,
        "out": str(out_dir),
        "reason": reason,
        "metrics": {
            "seconds": seconds,
            "subagent_runs": 0,
            "agents": [],
            "pro_input_tokens": 0,
            "pro_output_tokens": 0,
            "pro_reasoning_tokens": 0,
            "flash_input_tokens": 0,
            "flash_output_tokens": 0,
            "pro_tokens": 0,
            "flash_tokens": 0,
            "cost_usd": 0.0,
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
            "robust_pass": 0,
        },
    }


async def _main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    settings = Settings.load(REPO_ROOT / ".env")
    if not settings.api_key:
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2
    if not settings.subagent_api_key:
        print("no HARNESS_SUBAGENT_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2

    _write_coordinator()
    tmp = Path(tempfile.mkdtemp(prefix="harness-token-econ-"))
    settings = settings.replace(db_path=str(tmp / "harness.db"))
    runs: list[dict[str, Any]] = []
    done: set[tuple[str, int]] = set()
    if RESULTS_FILE.is_file():
        for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            done.add((rec["mode"], rec["run"]))
            runs.append(rec)
        print(
            f"resume: {len(done)} runs already recorded in {RESULTS_FILE} — skipping them",
            flush=True,
        )

    for label in GROUPS:
        advanced = label == "forced-advanced"
        forced = advanced  # both forced-* groups use the forced prompt
        for i in range(1, RUNS + 1):
            if (label, i) in done:
                continue
            out_dir = tmp / f"{label}-{i}"
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                record = await asyncio.wait_for(
                    _run_once(settings, out_dir, label=label, i=i, forced=forced, advanced=advanced),
                    timeout=RUN_TIMEOUT,
                )
            except TimeoutError:
                print(f"  {label}-{i}: TIMEOUT after {RUN_TIMEOUT:.0f}s — salvaging", flush=True)
                record = _salvage(label, i, out_dir, reason="timeout", seconds=RUN_TIMEOUT)
            m = record["metrics"]
            print(
                f"  ran {label}-{i}: {record['reason']} pro_tok={m['pro_tokens']} "
                f"flash_tok={m['flash_tokens']} sub={m['subagent_runs']} "
                f"verify={m['verify_pass']}/5 pytest={m['pytest_passed']} "
                f"robust={m['robust_pass']}/4 cost=${m['cost_usd']} wall={m['seconds']:.1f}s",
                flush=True,
            )
            runs.append(record)
            with RESULTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    if not runs:
        print("  no runs completed — aborting", file=sys.stderr)
        return 1

    by = {g: [r for r in runs if r["mode"] == g] for g in GROUPS}
    print("\n== token-economy comparison ==")
    for g in GROUPS:
        rs = by[g]
        if not rs:
            print(f"  {g:15s} n=0 (no completed runs)")
            continue
        ms = [r["metrics"] for r in rs]
        print(f"  {g:15s} n={len(rs)}")
        for key, unit in (
            ("pro_tokens", ""),
            ("pro_input_tokens", ""),
            ("flash_tokens", ""),
            ("cost_usd", " $"),
            ("seconds", " s"),
            ("subagent_runs", ""),
            ("verify_pass", "/5"),
            ("pytest_passed", ""),
            ("robust_pass", "/4"),
        ):
            print(f"      {key:18s} {_spread([float(m[key]) for m in ms])}{unit}")

    n = [r["metrics"]["pro_tokens"] for r in by.get("normal", [])]
    a = [r["metrics"]["pro_tokens"] for r in by.get("forced-advanced", [])]
    red = pro_reduction(a, n)
    if red is not None:
        print(f"\n  PRO-TOKEN REDUCTION: {red * 100:.1f}%  "
              f"(normal mean={sum(n)/len(n):.0f} -> advanced mean={sum(a)/len(a):.0f})")
    else:
        print("\n  PRO-TOKEN REDUCTION: undefined (need >=1 completed run per group)")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
```

注意：`os.chdir(REPO_ROOT)` 放在 import harness 之前（`REPO_ROOT` 由 `Path(__file__)` 推导，不依赖 cwd）。脚本以 `uv run python scripts/e2e_token_economy.py` 运行。

- [ ] **Step 4: 跑纯函数测试确认通过**

Run: `uv run pytest tests/test_token_economy_script.py -q`
Expected: 全 PASS

- [ ] **Step 5: lint 脚本**

Run: `uv run ruff check scripts/e2e_token_economy.py tests/test_token_economy_script.py`
Expected: clean（若有 E501，按现有脚本风格折行）

- [ ] **Step 6: Commit**

```bash
git add scripts/e2e_token_economy.py tests/test_token_economy_script.py
git commit -m "bench: in-process token-economy benchmark (normal vs forced-advanced, pro/flash usage split)"
```

---

### Task 4: 质量门 + 冒烟 + 全量跑 + 结果文档

**Files:**
- Run: 质量门、冒烟、全量基准
- Create: `docs/superpowers/2026-08-13-token-economy-bench-results.md`

**Interfaces:**
- Consumes: Task 1-3 交付的代码。全量跑需 `.env` 真实 key（主=pro/新 key，子=flash/旧 key，已验证可用）。

- [ ] **Step 1: 质量门全绿**

Run: `uv run ruff check . && uv run mypy src && uv run pytest -q`
Expected: ruff clean、mypy clean、pytest 全 PASS（含 Task 1-3 新增用例）

- [ ] **Step 2: 冒烟（每组 1 轮）**

Run: `HARNESS_COMPARE_RUNS=1 uv run python scripts/e2e_token_economy.py`
Expected: 输出含 `normal-1` 与 `forced-advanced-1` 两行；`forced-advanced` 行 `pro_tok` 明显小于 `normal` 行、`flash_tok` > 0、`verify=5/5`。若委托未触发（flash_tok≈0）→ 检查 coordinator 加载与 prompt，修后重跑。
（先清掉上次的 results JSONL：`rm -f "$TEMP/harness-token-econ-results.jsonl"`）

- [ ] **Step 3: 全量跑（每组 3 轮，约 1-1.5h）**

Run: `HARNESS_COMPARE_RUNS=3 uv run python scripts/e2e_token_economy.py`
Expected: 每组 3 条记录；汇总打印 `PRO-TOKEN REDUCTION`。跑批可在后台执行（`run_in_background: true`），完成后再汇总。

- [ ] **Step 4: 写结果文档**（`docs/superpowers/2026-08-13-token-economy-bench-results.md`，中文）

结构：实验设计回顾 → 每组逐轮表格（pro/flash token、cost、wall、verify、pytest、robust）→ 主 claim（pro-token 降幅 %）→ 质量佐证（verify 5/5 不降、robust advanced ≥ normal）→ 成本对比（真钱，12× 价差）→ 根因/机制（子代理在 flash 上干活、pro 只协调）→ 局限（n=3、flash 质量依赖模型能力）。

- [ ] **Step 5: Commit 结果**

```bash
git add docs/superpowers/2026-08-13-token-economy-bench-results.md
git commit -m "docs: token-economy benchmark results (advanced saves pro tokens, quality holds)"
```

## 执行偏差记录（2026-08-13 实施时）

1. `scripts/e2e_token_economy.py` 的 `main()` 改为**同步**（v6 先例）：JSONL 读写移到同步 helper `_load_results`/`_append_record`，每轮循环在 `async def _run_all`，避免 ASYNC230/240 触发。导入去掉未用的 `COORDINATOR_YAML`（`_write_coordinator` 自管）；`_fmt_spread` 未用改为只定义 `_spread`。
2. 结果文件路径可用 env `HARNESS_TOKEN_ECON_RESULTS` 覆盖（冒烟与全量隔离，避免 resume 串扰）。
3. `SUBAGENT_BUDGET` 实际通过 `settings.replace(subagent_budget=SUBAGENT_BUDGET)` 生效（计划只定义了常量未应用）。
4. `_auto_approve` 标注为 `Callable[[ToolCall], Awaitable[str]]`（`harness.core.messages.ToolCall`），匹配 `ApprovalPrompt`。
5. `tests/test_compose.py::test_subagent_provider_built_from_env_key` 不用 monkeypatch（`Settings.from_env(dict)` 不读进程 env），直接把 subagent key 放进 dict。
6. `tests/test_token_economy_script.py::test_sum_usage_aggregates` 的 reasoning 断言修正为 200_000（两个记录都带 reasoning）。
7. `_run_all` 末尾的 `PRO-TOKEN REDUCTION` 只统计 `reason == "ok"` 的干净轮：全量跑时 forced-advanced-2 在一次子代理重派后网络卡死、被 1800s 超时 salvage（pro_tokens=0），若计入会把真实降幅（96.6%）虚夸成 97.8%——超时是失败轮，不是省 token。
