# 番茄钟产品冲刺 Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用「多模块、需异构技能的番茄钟微型产品冲刺」任务 + harness 自写客观 verify 门,测出 advanced 模式(深链路由 + 并行 fan-out)相对 normal 的**可复现质量差异**;同时把 `coding`/`security-review` 两个新 bundled skill 注入 subagent 生态。

**Architecture:** 三组对照(normal / forced-normal / forced-advanced)共用同一套 bundled subagents + 运行时 coordinator,唯一变量是 advanced 开关。每组每个 run 在临时 scratch 目录实现 4 模块番茄钟服务;run 结束后 harness 把 `scripts/pomodoro_verify_template.py` 复制为 `{out}/verify_impl.py` 并执行,得 `verify_pass`(0-5)为主轴,模型自己的 pytest passed 数为次级。结构指标(depth/max_concurrency/waves/chain)复用 v4 的 WS 帧追踪。skill 通过现有 `skill:` 字段机制加载。

**Tech Stack:** Python 3.11 stdlib(http.server/sqlite3/threading)、uv、uvicorn + websockets(测试服)、YAML subagent registry、WS 事件帧追踪。

## Global Constraints

(逐字取自 spec `2026-08-12-pomodoro-sprint-design.md`,每个任务都隐式包含本节)

- Scratch 实现**纯标准库**,`from __future__ import annotations`,builtin generics;scratch 目录**不要求 ruff**(无 ruff 配置,省得 subagent 空转)。
- `verify_impl.py` 由 harness 生成(run 后写入 `{out}/`),**不参与模型运行**;主轴 = 5 项 PASS 数(0-5),模型 pytest passed 为次级。
- 分组:`GROUPS = [("normal", False, False), ("forced-normal", True, False), ("forced-advanced", True, True)]`,字段 = `(forced, advanced)`;三组共用同一 `coordinator.yaml`(只读:read/glob/grep/web_search,无 write/bash),normal 组也写 coordinator.yaml。
- 服务端启动设 `HARNESS_SUBAGENTS=1`,显式设 `HARNESS_SUBAGENT_BUDGET=120`(advanced 的 coordinator + 4 个 delegate 目标吃轮次多;该非等量因素要在结果文档里如实标注)。
- 新增 skill/subagent 放 `src/harness/skills/bundled/subagents/`(随包发布,runtime `skills/subagents/` 同名覆盖);改 `coder.yaml` 只加 `skill: coding` 字段。
- 成功判据:forced-advanced 的 verify_pass 中位数 **≥** forced-normal,且深度 2、并发峰值 ≥2 在 forced-advanced 稳定出现、在 forced-normal 不出现;若打平则如实报告。
- 结果文档 `docs/superpowers/2026-08-12-pomodoro-sprint-results.md` 用**中文**,如实记录超时/失败/打平。
- 常驻安全:**绝不打印/回显 DEEPSEEK_API_KEY 或任何 API key**(.env 视为机密)。

---

### Task 1: 新增 `coding` skill 并挂到 `coder.yaml`

给 coder subagent 注入「工程纪律」方法论,benchmark 的 scratch 实现靠它扛住并发/边界/安全断言。skill body 是纯文本附加到 coder 指令后,对主 agent 零负担。

**Files:**
- Create: `src/harness/skills/bundled/subagents/coding.md`
- Modify: `src/harness/skills/bundled/subagents/coder.yaml`(加 `skill: coding`)
- Test: `tests/test_agents.py`(改 `test_subagent_skill_loads_from_bundled` 与 `test_example_subagents_include_design_and_writer`)

**Interfaces:**
- Consumes: `load_subagent_skill(name)` 从 `skills/subagents/<name>.md` → repo root → `BUNDLED_SUBAGENTS_DIR` 读 body 并 strip frontmatter(registry.py);`_with_skill` 追加为 `\n\n# Skill: coding\n\n<body>`。
- Produces: `coder` spec 的 `skill=="coding"`,`coder` 的 `instructions` 含 marker 子串 `"Coding Discipline"`。

- [ ] **Step 1: 写失败测试**(先证明 skill 尚未加载)

  在 `tests/test_agents.py` 的 `test_subagent_skill_loads_from_bundled`(第 225-233 行)加一行:

```python
def test_subagent_skill_loads_from_bundled() -> None:
    """...（原 docstring 保留）"""
    from harness.agents.registry import load_subagent_skill

    assert "Frontend Design" in load_subagent_skill("frontend-design")
    assert "Doc Co-Authoring Workflow" in load_subagent_skill("doc-coauthoring")
    assert "Coding Discipline" in load_subagent_skill("coding")
    assert "Security Review" in load_subagent_skill("security-review")
```

  同时把 `test_example_subagents_include_design_and_writer`(第 203-222 行)的 marker 字典补上两项:

```python
    for name, marker in {
        "frontend_design": "Frontend Design",
        "doc_writer": "Doc Co-Authoring Workflow",
        "coder": "Coding Discipline",
        "security_reviewer": "Security Review",
    }.items():
        assert marker in subs[name].instructions, f"{name} missing its skill"
```

  子集断言 `{"researcher","coder","frontend_design","doc_writer","search","file_handler"} <= set(subs)` 不需要改——新 subagent 只会让它更宽松。

- [ ] **Step 2: 跑测试确认失败**

  Run: `uv run pytest tests/test_agents.py::test_subagent_skill_loads_from_bundled -v`
  Expected: FAIL——`load_subagent_skill("coding")` 返回空串,`"Coding Discipline" in ""` 为 False。

- [ ] **Step 3: 写 `coding.md` skill body**

  创建 `src/harness/skills/bundled/subagents/coding.md`(格式对齐 `frontend-design.md`:frontmatter + body,body 首行标题即测试 marker):

```markdown
---
name: coding
description: Engineering discipline for implementing modules to a fixed contract — read the contract first, small verified steps, boundary/concurrency robustness, security defaults, no AI-default code.
---

# Coding Discipline

Follow these steps for every module you implement. The point is a module that
meets its contract exactly and survives hostile inputs and concurrent callers —
not code that merely works on the happy path.

## 1. Read the contract before writing anything

The task brief fixes exact class names, method signatures, return types, and
behavioral rules (e.g. "out-of-order transitions are no-ops"). Write them down
and implement TO them, not to your own idea of the API. A module that renames a
method or changes a return type fails even if it "works".

## 2. Small steps, each verified

Implement one method, then prove it with a focused test before moving on. Do not
write the whole module and check at the end. Prefer TDD: write the failing test,
implement the minimum, watch it pass.

## 3. Boundary and concurrency robustness

- Validate inputs: reject missing/non-positive/oversized values with the
  documented error, never a crash.
- Thread-safety: a module that other threads may call concurrently (state
  machines, storage) must not corrupt or raise under concurrent use. Guard
  shared state with a lock — for a shared sqlite connection set
  `check_same_thread=False` and serialize access with the lock, or use a
  connection per call.
- Persistence must survive close-and-reopen.

## 4. Security defaults

- SQL: ALWAYS parameter binding (`?` placeholders). Never build SQL by f-string
  or string concatenation.
- No eval(), no exec(), no hardcoded passwords/secrets/API keys.
- Enforce the documented request limits; reject oversized input cleanly without
  crashing the server.

## 5. No AI-default code

If you write something because it "looks standard", stop and ask what it buys.
Every non-trivial choice needs a one-line reason (in a comment or your report):
why this structure, this data layout, this boundary. Deleting a needless
abstraction is an improvement.

## 6. Verify before finishing

Run the contract's verification (the harness writes a fixed verify_impl.py into
the out dir) and the module tests. Report exactly which files you wrote and the
verification result. Never report success on an unverified module.
```

- [ ] **Step 4: 给 `coder.yaml` 加 `skill: coding` 字段**

  在 `src/harness/skills/bundled/subagents/coder.yaml` 的 description 块与 `instructions:` 之间插入一行(位置对齐 `frontend_design.yaml`):

```yaml
description: >-
  Use when code needs to be inspected, written, modified, or tested. For any
  coding task, delegate to coder by default instead of writing the code
  yourself; it runs the tests and returns verification output.
skill: coding
instructions: |
```

  **注意**:coder.yaml 原指令要求写代码后跑 `uv run ruff check`。benchmark 的 `SPRINT_TASK` 明确说「不要跑 ruff」,任务 brief 优先于通用系统指令;即便 coder 跑一次 ruff 也只是多一轮工具调用,不伤 verify。不要为了 benchmark 改 coder 的常驻指令。

- [ ] **Step 5: 跑测试确认通过**

  Run: `uv run pytest tests/test_agents.py::test_subagent_skill_loads_from_bundled tests/test_agents.py::test_example_subagents_include_design_and_writer -v`
  Expected: 两个都 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/harness/skills/bundled/subagents/coding.md src/harness/skills/bundled/subagents/coder.yaml tests/test_agents.py
git commit -m "skills: add coding discipline skill for the coder subagent"
```

---

### Task 2: 新增 `security-review` skill + `security_reviewer` 只读 subagent

新 subagent 专职安全过检:只读(无 write/bash),返回结构化发现清单,由 coder 修复。这正符合「给 subagent 强、给主 agent 臃肿」原则——几百行方法论下派时才加载。

**Files:**
- Create: `src/harness/skills/bundled/subagents/security-review.md`
- Create: `src/harness/skills/bundled/subagents/security_reviewer.yaml`
- Test: `tests/test_agents.py`(新增 `test_security_reviewer_is_readonly_and_skilled`)

**Interfaces:**
- Consumes: Task 1 已在 `test_subagent_skill_loads_from_bundled` 加 `"Security Review" in load_subagent_skill("security-review")`(现在会红)。
- Produces: `example_subagents()`(auto-discover bundled `*.yaml`)自动含 `security_reviewer`;`security_reviewer` 的 `instructions` 含 marker `"Security Review"`,tools 只含 read/glob/grep/web_search。

- [ ] **Step 1: 跑 Task 1 留下的红灯测试,确认 security-review 还没加载**

  Run: `uv run pytest tests/test_agents.py::test_subagent_skill_loads_from_bundled -v`
  Expected: FAIL——`load_subagent_skill("security-review")` 返回空串。

- [ ] **Step 2: 写 `security-review.md` skill body**

  创建 `src/harness/skills/bundled/subagents/security-review.md`:

```markdown
---
name: security-review
description: Read-only security audit checklist for Python modules — input validation, injection, hardcoded credentials, request limits, error handling. Returns a structured findings list.
---

# Security Review

You are a READ-ONLY auditor: inspect the given Python files and report findings.
You do not fix code and you do not write anything.

## Checklist (audit each file against every line)

1. **Injection** — SQL built by f-string or string concatenation instead of
   `?` parameter binding; shell/command injection; HTML/JS injection via
   unescaped user input reflected into responses.
2. **Input validation** — missing/non-positive/oversized inputs accepted; the
   documented request-size limit not enforced; malformed JSON or requests
   causing a 500 with a stack trace instead of a clean 4xx.
3. **Hardcoded credentials** — password / secret / api_key literal in source;
   tokens in source files that should come from config or environment.
4. **Unsafe dynamic code** — eval(), exec(), or similar anywhere.
5. **Authorization & exposure** — endpoints that act on any id without
   ownership checks; internal server details (tracebacks, versions) leaked in
   responses.
6. **Concurrency & resource safety** — shared state mutated without a lock;
   file/db handles leaked; denial-of-service vectors (unbounded bodies).

## Output format

Return a structured findings list. For each finding:
- `file:line` — exact location
- `severity` — HIGH / MEDIUM / LOW
- `vulnerability` — what is wrong, concretely
- `fix` — the minimal change that resolves it

End with a verdict line:
- `CLEAN` if no HIGH/MEDIUM findings, or
- `MUST-FIX: <file:line, file:line, ...>` listing the blocking findings.
```

- [ ] **Step 3: 写 `security_reviewer.yaml`**

  创建 `src/harness/skills/bundled/subagents/security_reviewer.yaml`:

```yaml
name: security_reviewer
description: >-
  Use when code needs a security audit — SQL injection, input validation,
  oversized requests, hardcoded credentials, unsafe dynamic code. For ANY
  security review, delegate to security_reviewer by default instead of
  auditing yourself: it inspects the target modules read-only and returns a
  structured findings list with a CLEAN / MUST-FIX verdict.
skill: security-review
instructions: |
  You are a security review subagent. Audit the given Python modules against
  the security-review skill below. You are READ-ONLY: read_file, glob_files,
  grep_files, web_search only — you have no write_file or bash. Inspect each
  module and return a structured findings list (file:line, severity
  HIGH/MEDIUM/LOW, vulnerability, fix) and a final verdict of CLEAN or
  MUST-FIX with the blocking findings. Do not modify any code.
max_turns: 8
tools:
  - read_file
  - glob_files
  - grep_files
  - web_search
```

  description 含触发词「Use when…by default」(`test_delegate_tool_descriptions_carry_triggers` 要求);DELIVERY_CONTRACT 由 `to_subagent` 统一追加(`test_subagents_carry_delivery_contract` 自动覆盖到新 subagent,无需改)。

- [ ] **Step 4: 写只读性新测试**

  在 `tests/test_agents.py` 加一个新测试函数(放在 YAML registry 测试区之前):

```python
def test_security_reviewer_is_readonly_and_skilled() -> None:
    """security_reviewer ships read-only and carries the security-review skill."""
    from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry
    from harness.tools.builtin import builtin_registry

    reg = SubagentRegistry(Path(".") / "nope", bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("security_reviewer")
    assert spec is not None
    assert spec.skill == "security-review"
    assert "write_file" not in spec.tools and "bash" not in spec.tools
    sa = reg.to_subagent(spec)
    assert "Security Review" in sa.instructions
    # every declared tool name resolves to a builtin (registry tolerates unknowns,
    # so assert the allowlist actually binds)
    builtins = builtin_registry()
    for name in spec.tools:
        assert builtins.get(name) is not None, f"unknown tool {name!r}"
```

  (文件顶部已 `from pathlib import Path`,无需新增 import。)

- [ ] **Step 5: 跑全部 subagent 相关测试**

  Run: `uv run pytest tests/test_agents.py -q`
  Expected: 全 PASS,包括 `test_subagent_registry_loads_bundled_defaults`(子集断言,新 subagent 不破坏)、`test_subagents_carry_delivery_contract`、`test_delegate_tool_descriptions_carry_triggers`。

- [ ] **Step 6: Commit**

```bash
git add src/harness/skills/bundled/subagents/security-review.md src/harness/skills/bundled/subagents/security_reviewer.yaml tests/test_agents.py
git commit -m "subagents: add read-only security_reviewer with security-review skill"
```

---

### Task 3: 写客观门 `scripts/pomodoro_verify_template.py` 并用人工参考实现验证

这是 benchmark 的承重件(R5):断言设计为「合理实现必过、糊弄过不去」。写成独立文件,benchmark 把它复制进每个 `{out}/` 当 `verify_impl.py`——独立可测,不嵌字符串。

**Files:**
- Create: `scripts/pomodoro_verify_template.py`
- (验证用)临时目录 `%TEMP%/pomo-refcheck/` 里放一份人工参考实现,跑完即弃

**Interfaces:**
- Consumes: 无(纯 stdlib,不 import harness)。
- Produces: 运行后打印 5 行 `PASS/FAIL <gate>`,末行 `VERIFY_PASS N/5`;退出码 0 iff 5 项全过。benchmark(v6)按此解析。

- [ ] **Step 1: 写模板文件**

  创建 `scripts/pomodoro_verify_template.py`(完整内容如下):

```python
"""Harness-generated hard gate for the pomodoro-sprint benchmark.

NOT written or run by the model. The harness copies this file into each
{out} directory after the model's run and executes it with
`uv run python {out}/verify_impl.py`. Prints PASS/FAIL per gate and a final
`VERIFY_PASS N/5` line. The 5 gates (engine / storage / api / static / readme)
encode the fixed module contracts from the task prompt; the security static
sniffs are folded into the storage and api gates.

Exit code: 0 iff all 5 gates pass.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent
PASSED: list[str] = []


def gate(name: str):
    """Record a pass (or fail with reason) for the gate named ``name``."""

    def deco(fn):
        def wrapper():
            try:
                fn()
                PASSED.append(name)
                print(f"PASS {name}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)

        return wrapper

    return deco


class FakeClock:
    """Deterministic clock for the engine's injectable ``clock`` parameter."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _rm(path: Path) -> None:
    """Unlink a scratch db; tolerate Windows file locks in temp dirs."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---- security static sniffs (folded into the storage / api gates) ---- #


def _assert_no_eval_exec() -> None:
    for p in ("engine.py", "storage.py", "api.py"):
        src = (OUT / p).read_text(encoding="utf-8")
        assert "eval(" not in src and "exec(" not in src, f"{p} uses eval()/exec()"


def _assert_no_hardcoded_secrets() -> None:
    bad = ("password = ", "secret = ", "api_key = ", "apiKey = ")
    for p in ("engine.py", "storage.py", "api.py"):
        for line in (OUT / p).read_text(encoding="utf-8").splitlines():
            stripped = line.strip().lower()
            if any(stripped.startswith(b) for b in bad):
                raise AssertionError(f"{p} hardcodes a secret: {line.strip()!r}")


def _assert_parameterized_sql() -> None:
    src = (OUT / "storage.py").read_text(encoding="utf-8")
    assert "?" in src, "storage.py uses no SQL placeholders"
    for line in src.splitlines():
        if "execute(" in line:
            assert "f\"" not in line and "f'" not in line, f"f-string SQL: {line.strip()!r}"
            if "+" in line and '"' in line:
                raise AssertionError(f"concatenated SQL: {line.strip()!r}")


@gate("engine")
def _gate_engine() -> None:
    from engine import PomodoroEngine

    clock = FakeClock(0.0)
    e = PomodoroEngine(work_minutes=25, break_minutes=5, clock=clock)
    assert e.state() == "idle", f"initial state={e.state()!r}"
    e.start()
    assert e.state() == "work", f"after start state={e.state()!r}"
    clock.advance(25 * 60)
    assert e.state() == "break", f"after work period state={e.state()!r}"
    clock.advance(5 * 60)
    assert e.state() == "work", f"after break period state={e.state()!r}"
    # pause freezes elapsed; resume continues
    e.pause()
    frozen = e.elapsed_seconds()
    clock.advance(60)
    assert e.elapsed_seconds() == frozen, "elapsed advanced while paused"
    e.resume()
    clock.advance(60)
    assert e.elapsed_seconds() > frozen, "elapsed did not resume"
    e.reset()
    assert e.state() == "idle" and e.elapsed_seconds() == 0, "reset failed"
    # concurrency: 8 threads hammering all methods must not raise / corrupt
    errors: list[BaseException] = []
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                e.reset()
                e.start()
                e.pause()
                e.resume()
                e.state()
                e.elapsed_seconds()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"engine concurrency raised: {errors[:3]}"


@gate("storage")
def _gate_storage() -> None:
    import storage

    db = OUT / "_verify_storage.db"
    _rm(db)
    store = storage.SessionStore(db)
    sid = store.create(duration_s=1500, started_at=1000.0, note="deep work")
    assert isinstance(sid, int) and sid > 0, f"create returned {sid!r}"
    got = store.get(sid)
    assert got is not None and got["duration_s"] == 1500, f"get={got!r}"
    assert got["started_at"] == 1000.0 and got["note"] == "deep work"
    assert store.get(999_999) is None, "get missing id should be None"
    assert store.delete(999_999) is False, "delete missing id should be False"
    assert store.update(999_999, note="x") is False, "update missing id should be False"
    assert any(s["id"] == sid for s in store.list()), "list lacks created id"
    # persistence across close-and-reopen
    reopened = storage.SessionStore(db)
    assert reopened.get(sid) is not None, "data lost on reopen"
    # concurrency on its OWN db for an exact count: 8 threads x 100 = 800
    cdb = OUT / "_verify_storage_conc.db"
    _rm(cdb)
    conc = storage.SessionStore(cdb)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(100):
                conc.create(duration_s=10, started_at=0.0, note="")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"storage concurrency raised: {errors[:3]}"
    assert len(conc.list()) == 800, f"expected 800 rows, got {len(conc.list())}"
    _rm(db)
    _rm(cdb)
    # security sniff folded into the storage gate
    _assert_parameterized_sql()
    _assert_no_hardcoded_secrets()


@gate("api")
def _gate_api() -> None:
    import api
    import storage

    db = OUT / "_verify_api.db"
    _rm(db)
    store = storage.SessionStore(db)
    server = api.create_server(store, static_dir=OUT / "static", host="127.0.0.1", port=0)
    port: int = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def request(method: str, path: str, body: bytes | None = None):
        req = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (ConnectionError, OSError, TimeoutError):
            # server closed the connection early (e.g. rejected oversized body
            # without draining); survival is re-verified by the next request
            return -1, b""

    try:
        status, html = request("GET", "/")
        assert status == 200, f"GET / -> {status}"
        assert b"pomodoro" in html.lower(), "GET / does not mention pomodoro"
        body = json.dumps({"duration_s": 1500, "note": "deep work"}).encode()
        status, payload = request("POST", "/api/sessions", body)
        assert status == 201, f"POST valid -> {status}"
        sid = json.loads(payload)["id"]
        status, _ = request("POST", "/api/sessions", b"not-json")
        assert status == 400, f"POST malformed -> {status}"
        status, _ = request("POST", "/api/sessions", json.dumps({"note": "x"}).encode())
        assert status == 400, f"POST missing duration_s -> {status}"
        status, _ = request("POST", "/api/sessions", json.dumps({"duration_s": 0}).encode())
        assert status == 400, f"POST non-positive duration_s -> {status}"
        status, _ = request("GET", f"/api/sessions/{sid}")
        assert status == 200, f"GET existing id -> {status}"
        status, _ = request("GET", "/api/sessions/999999")
        assert status == 404, f"GET missing id -> {status}"
        status, payload = request("GET", "/api/sessions")
        assert status == 200, f"GET list -> {status}"
        assert any(s["id"] == sid for s in json.loads(payload)), "list lacks created id"
        # oversized body: 70 KB > 64 KB limit; server must survive
        big = json.dumps({"duration_s": 25, "note": "x" * 70_000}).encode()
        status, _ = request("POST", "/api/sessions", big)
        assert status in (400, 413, -1), f"oversized body -> {status}"
        status, _ = request("GET", "/api/sessions")
        assert status == 200, "server died after oversized body"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _rm(db)
    # security sniff folded into the api gate
    _assert_no_eval_exec()
    _assert_no_hardcoded_secrets()


@gate("static")
def _gate_static() -> None:
    idx = (OUT / "static" / "index.html").read_text(encoding="utf-8")
    js = (OUT / "static" / "app.js").read_text(encoding="utf-8")
    css = (OUT / "static" / "style.css").read_text(encoding="utf-8")
    assert "style.css" in idx and "app.js" in idx, "index.html lacks style.css/app.js refs"
    assert "timer" in idx.lower(), "index.html has no timer element"
    assert "<button" in idx.lower(), "index.html has no <button> controls"
    for fn in ("startTimer", "pauseTimer", "resetTimer"):
        assert fn in js, f"app.js missing {fn}"
    assert "fetch" in js and "/api/sessions" in js, "app.js does not fetch /api/sessions"
    assert css.count("{") >= 15, f"style.css has {css.count('{')} rules"


@gate("readme")
def _gate_readme() -> None:
    text = (OUT / "README.md").read_text(encoding="utf-8")
    for sec in ("Overview", "Run", "API", "Tests"):
        assert sec in text, f"README missing section {sec!r}"
    assert any("python" in line for line in text.splitlines()), "README has no run command line"


def main() -> int:
    for fn in (_gate_engine, _gate_storage, _gate_api, _gate_static, _gate_readme):
        fn()
    print(f"VERIFY_PASS {len(PASSED)}/5", flush=True)
    return 0 if len(PASSED) == 5 else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: ruff 检查模板**

  Run: `uv run ruff check scripts/pomodoro_verify_template.py`
  Expected: `All checks passed!`(若有违规就地修复再重跑)。

- [ ] **Step 3: 搭人工参考实现(验证门不过严)**

  在 `%TEMP%/pomo-refcheck/` 建 4 模块 + 前端 + README,每个文件完整内容如下——这是 R5 的 ground truth,刻意写成「合理但不过度工程」的实现,门必须 5/5 全过:

  `%TEMP%/pomo-refcheck/engine.py`:

```python
from __future__ import annotations

import time
from typing import Callable


class PomodoroEngine:
    """A tiny Pomodoro state machine with an injectable clock."""

    def __init__(
        self,
        work_minutes: int = 25,
        break_minutes: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.work_minutes = work_minutes
        self.break_minutes = break_minutes
        self._clock = clock
        self._state = "idle"
        self._period_start: float = 0.0
        self._frozen: float = 0.0

    def start(self) -> None:
        if self._state == "idle":
            self._state = "work"
            self._period_start = self._clock()

    def pause(self) -> None:
        if self._state in ("work", "break"):
            self._frozen = self.elapsed_seconds()
            self._state = "paused"

    def resume(self) -> None:
        if self._state == "paused":
            self._state = "work"
            self._period_start = self._clock() - self._frozen

    def reset(self) -> None:
        self._state = "idle"
        self._frozen = 0.0

    def _phase(self) -> str:
        if self._state in ("idle", "paused"):
            return self._state
        elapsed = self._clock() - self._period_start
        if self._state == "work":
            if elapsed >= self.work_minutes * 60:
                self._state = "break"
                self._period_start = self._clock()
                return "break"
            return "work"
        if elapsed >= self.break_minutes * 60:
            self._state = "work"
            self._period_start = self._clock()
            return "work"
        return "break"

    def state(self) -> str:
        return self._phase()

    def elapsed_seconds(self) -> float:
        if self._state == "paused":
            return self._frozen
        if self._state == "idle":
            return 0.0
        return self._clock() - self._period_start
```

  `%TEMP%/pomo-refcheck/storage.py`:

```python
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

_ALLOWED = ("duration_s", "started_at", "note")


class SessionStore:
    """SQLite-backed session store, safe for concurrent create() calls."""

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "duration_s INTEGER NOT NULL, "
                "started_at REAL NOT NULL, "
                "note TEXT NOT NULL DEFAULT '')"
            )
            self._conn.commit()

    def create(self, duration_s: int, started_at: float, note: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (duration_s, started_at, note) VALUES (?, ?, ?)",
                (duration_s, started_at, note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions").fetchall()
        return [dict(r) for r in rows]

    def update(self, session_id: int, **fields: object) -> bool:
        if not fields:
            return self.get(session_id) is not None
        bad = [k for k in fields if k not in _ALLOWED]
        if bad:
            raise ValueError(f"unknown fields: {bad}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE sessions SET {assignments} WHERE id = ?"
        with self._lock:
            cur = self._conn.execute(sql, (*fields.values(), session_id))
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, session_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()
            return cur.rowcount > 0
```

  `%TEMP%/pomo-refcheck/api.py`:

```python
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_BODY = 64 * 1024


def create_server(store, static_dir, host: str = "127.0.0.1", port: int = 0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep logs quiet
            pass

        def _send(self, status: int, payload: str, ctype: str = "application/json") -> None:
            data = payload.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY:
                return None
            return self.rfile.read(length)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                index = Path(static_dir) / "index.html"
                if not index.is_file():
                    self._send(404, "not found", "text/plain")
                    return
                self._send(200, index.read_text(encoding="utf-8"), "text/html")
            elif path == "/api/sessions":
                self._send(200, json.dumps(store.list()))
            elif path.startswith("/api/sessions/"):
                try:
                    sid = int(path.rsplit("/", 1)[1])
                except ValueError:
                    self._send(400, json.dumps({"error": "bad id"}))
                    return
                got = store.get(sid)
                if got is None:
                    self._send(404, json.dumps({"error": "not found"}))
                else:
                    self._send(200, json.dumps(got))
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self) -> None:
            if self.path != "/api/sessions":
                self._send(404, "not found", "text/plain")
                return
            body = self._read_body()
            if body is None:
                self._send(413, json.dumps({"error": "payload too large"}))
                return
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                self._send(400, json.dumps({"error": "malformed json"}))
                return
            duration = data.get("duration_s")
            if not isinstance(duration, int) or duration <= 0:
                self._send(400, json.dumps({"error": "duration_s required > 0"}))
                return
            note = data.get("note") or ""
            if not isinstance(note, str):
                self._send(400, json.dumps({"error": "note must be a string"}))
                return
            sid = store.create(duration_s=duration, started_at=1000.0, note=note)
            self._send(201, json.dumps({"id": sid}))

    return HTTPServer((host, port), Handler)


if __name__ == "__main__":
    import storage as _storage

    _store = _storage.SessionStore(Path("sessions.db"))
    _server = create_server(_store, static_dir="static", host="127.0.0.1", port=8000)
    _server.serve_forever()
```

  `%TEMP%/pomo-refcheck/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pomodoro Timer</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <h1>Pomodoro</h1>
    <p id="status">Ready</p>
    <div id="timer">25:00</div>
    <div class="controls">
      <button id="startBtn">Start</button>
      <button id="pauseBtn">Pause</button>
      <button id="resetBtn">Reset</button>
    </div>
    <ul id="log"></ul>
  </main>
  <script src="app.js"></script>
</body>
</html>
```

  `%TEMP%/pomo-refcheck/static/app.js`:

```js
const timer = document.getElementById("timer");
const status = document.getElementById("status");
const startBtn = document.getElementById("startBtn");
const pauseBtn = document.getElementById("pauseBtn");
const resetBtn = document.getElementById("resetBtn");
const log = document.getElementById("log");

let secondsLeft = 25 * 60;
let intervalId = null;

function render() {
  const m = String(Math.floor(secondsLeft / 60)).padStart(2, "0");
  const s = String(secondsLeft % 60).padStart(2, "0");
  timer.textContent = m + ":" + s;
}

function startTimer() {
  if (intervalId) return;
  status.textContent = "Focusing…";
  intervalId = setInterval(() => {
    if (secondsLeft > 0) {
      secondsLeft -= 1;
      render();
    } else {
      pauseTimer();
      status.textContent = "Done!";
      fetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration_s: 1500, note: "pomodoro" }),
      }).catch(() => {});
    }
  }, 1000);
}

function pauseTimer() {
  clearInterval(intervalId);
  intervalId = null;
}

function resetTimer() {
  pauseTimer();
  secondsLeft = 25 * 60;
  status.textContent = "Ready";
  render();
}

render();
```

  `%TEMP%/pomo-refcheck/static/style.css`(15 条规则):

```css
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; }
main { max-width: 480px; margin: 48px auto; text-align: center; }
h1 { font-size: 2rem; }
#timer { font-size: 4rem; font-variant-numeric: tabular-nums; }
#status { color: #555; }
.controls { display: flex; gap: 12px; justify-content: center; margin-top: 24px; }
button { padding: 10px 20px; font-size: 1rem; border-radius: 8px; border: 1px solid #ccc; cursor: pointer; }
#startBtn { background: #2e7d32; color: #fff; }
#pauseBtn { background: #f9a825; color: #111; }
#resetBtn { background: #eee; color: #111; }
button:hover { filter: brightness(0.95); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
#log { margin-top: 24px; text-align: left; }
#log li { margin: 4px 0; }
```

  `%TEMP%/pomo-refcheck/README.md`:

```markdown
# Pomodoro Timer

## Overview
A single-user Pomodoro timer service with a Python state-machine engine,
SQLite storage, and a browser UI. Standard library only.

## Run
Start the server:

    uv run python api.py

Then open http://127.0.0.1:8000 in a browser.

## API
- GET /api/sessions — list sessions
- POST /api/sessions — create a session, body {"duration_s": int, "note": str}
- GET /api/sessions/<id> — one session

## Tests
Run the test suite:

    uv run pytest
```

- [ ] **Step 4: 跑模板验证参考实现全过**

  Run:
  `cp scripts/pomodoro_verify_template.py "%TEMP%/pomo-refcheck/verify_impl.py"` 然后
  `uv run python "%TEMP%/pomo-refcheck/verify_impl.py"`
  Expected: 5 行 `PASS`,末行 `VERIFY_PASS 5/5`,退出码 0。
  **若某门 FAIL**:说明断言过严(如 sqlite 跨线程、并发行数、窗口残留),就地改模板再重跑,直到 5/5。

- [ ] **Step 5: 破坏一处,确认门抓得住糊弄**

  在 `%TEMP%/pomo-refcheck/api.py` 把 `_read_body` 里的超限分支注释掉(让它接受任意大小 body),重跑:
  `uv run python "%TEMP%/pomo-refcheck/verify_impl.py"`
  Expected: `FAIL api: ... oversized body -> 200` 且 `VERIFY_PASS 4/5`。恢复 api.py 后再跑一次确认回到 5/5。

- [ ] **Step 6: 清掉临时参考实现并 Commit**

```bash
rm -rf "$TMPDIR/pomo-refcheck" 2>/dev/null; rm -rf "$TEMP/pomo-refcheck" 2>/dev/null
git add scripts/pomodoro_verify_template.py
git commit -m "bench: add pomodoro verify gate template (stdlib, 5 gates + security sniffs)"
```

---

### Task 4: 写 benchmark runner `scripts/e2e_subagents_compare_v6.py`

三组对照 × RUNS,每 run 起 scratch、跑模型、复制 verify 门、跑模型 pytest、汇总。复用 v2 的 `_free_port/_wait_health/_fmt_spread`,v4 的 WS 链追踪(改造为可传 `prompt`/`advanced`)。

**Files:**
- Create: `scripts/e2e_subagents_compare_v6.py`

**Interfaces:**
- Consumes: Task 3 的 `scripts/pomodoro_verify_template.py`(按路径读取复制);v2 的 `REPO_ROOT/_fmt_spread/_free_port/_wait_health`;Task 1/2 的 bundled subagents + 运行时 coordinator。
- Produces: 每 run 输出 `{out}/` 目录(4 模块+前端+测试+README+verify_impl.py);stdout 每 run 一行 metrics + 末尾组间汇总表;退出码 0/1/2。

- [ ] **Step 1: 写完整脚本**

  创建 `scripts/e2e_subagents_compare_v6.py`(完整内容如下):

```python
"""Pomodoro micro-sprint benchmark: normal vs forced-normal vs forced-advanced.

Each run has the model implement a single-user Pomodoro timer in a scratch dir
(engine / storage / api / static / tests / README). After the run the harness
copies scripts/pomodoro_verify_template.py into the dir as verify_impl.py and
runs it (primary axis: verify_pass 0-5), then runs the model's own pytest
(secondary metric). WS frames track the delegation chain / concurrency.

Env:
  HARNESS_COMPARE_RUNS      runs per group (default 3)
  HARNESS_COMPARE_GROUPS    comma-separated group labels to run (default all)

Exit codes: 0 ok, 1 fail, 2 no API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from e2e_subagents_compare_v2 import (
    REPO_ROOT,
    _fmt_spread,
    _free_port,
    _wait_health,
)

RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))
DELEGATE_PREFIX = "delegate_to_"
COORDINATOR_NAME = "coordinator"
VERIFY_TEMPLATE = REPO_ROOT / "scripts" / "pomodoro_verify_template.py"

# (label, forced, advanced) — only `advanced` varies between groups.
GROUPS_ALL: list[tuple[str, bool, bool]] = [
    ("normal", False, False),
    ("forced-normal", True, False),
    ("forced-advanced", True, True),
]
_filter = [s for s in os.environ.get("HARNESS_COMPARE_GROUPS", "").split(",") if s]
GROUPS = [g for g in GROUPS_ALL if not _filter or g[0] in _filter]

SPRINT_TASK = """\
Implement a complete single-user Pomodoro timer service in the directory {out}.
Use ONLY the Python standard library — no third-party imports. Do NOT run ruff
(this scratch dir has no ruff config). Write every file to {out} exactly.

You must produce these files, with these EXACT module contracts:

{out}/engine.py
  class PomodoroEngine(work_minutes: int = 25, break_minutes: int = 5,
                       clock: Callable[[], float] = time.monotonic)
    start() -> None           # idle -> work
    pause() -> None           # work|break -> paused, freezes elapsed_seconds
    resume() -> None          # paused -> work|break
    reset() -> None           # any -> idle, elapsed_seconds resets to 0
    state() -> str            # "idle" | "work" | "break" | "paused"
    elapsed_seconds() -> float
  `clock` must be injectable (default time.monotonic) so a test can fake time.
  A transition that does not apply to the current state is a NO-OP and must
  never raise (e.g. pause() while idle). After `work_minutes` of accumulated
  work the state becomes "break"; after `break_minutes` of break it returns to
  "work". elapsed_seconds() is frozen while paused.

{out}/storage.py
  class SessionStore(path: str | Path)
    create(duration_s: int, started_at: float, note: str = "") -> int   # new id
    get(session_id: int) -> dict | None
    list() -> list[dict]
    update(session_id: int, **fields: object) -> bool   # False if id missing
    delete(session_id: int) -> bool                      # False if id missing
  SQLite-backed and persistent across close-and-reopen. ALL SQL must use
  parameter binding (`?` placeholders) — never build SQL by f-string or string
  concatenation. Must be safe under concurrent create() calls from multiple
  threads (a lock + check_same_thread=False, or a connection per call).

{out}/api.py
  def create_server(store, static_dir, host="127.0.0.1", port=0) -> HTTPServer
  Endpoints:
    GET  /                  -> serve static/index.html from static_dir
    GET  /api/sessions      -> 200 + JSON list
    POST /api/sessions      -> body {"duration_s": int, "note": str}
                                201 + {"id": N}; 400 on malformed JSON or a
                                missing/non-positive duration_s
    GET  /api/sessions/<id> -> 200 + session dict; 404 if missing
  Reject request bodies over 64 KB with 413 or 400 WITHOUT crashing the server.
  Never return a 500 with a stack trace for malformed input.

{out}/static/index.html, {out}/static/app.js, {out}/static/style.css
  A working Pomodoro timer page. index.html references style.css and app.js,
  shows a timer display, and has Start / Pause / Reset <button> controls.
  app.js defines functions startTimer, pauseTimer, resetTimer and calls
  fetch("/api/sessions") (e.g. to POST a completed session). style.css has at
  least 15 rules and is linked from index.html.

{out}/test_engine.py, {out}/test_storage.py, {out}/test_api.py
  Your own pytest tests for the three modules. They must pass with:
  `uv run pytest -q {out}`

{out}/README.md
  Sections: Overview, Run, API, Tests. Include the command to start the server.

Security requirements: no eval()/exec() anywhere; no hardcoded passwords,
secrets, or API keys in any file.

When done, report the files you wrote and the pytest result.
"""

SPRINT_TASK_FORCED = (
    "DELEGATE THE ENTIRE TASK to a single subagent — call the "
    f"delegate_to_{COORDINATOR_NAME} tool once and hand it the FULL task below "
    "plus the target directory {out}. Do NOT scaffold, write, read, search, or "
    "implement anything yourself — the coordinator owns the whole job.\n\n"
    "NOTE: the coordinator cannot write files or run bash. It is expected to "
    "split the work and hand each piece to the matching subagents — coder for "
    "engine.py/storage.py/api.py and their tests, frontend_design for static/, "
    "security_reviewer for a read-only audit of storage.py and api.py, "
    "doc_writer for README.md — via its delegate_to_* tools, several in "
    "parallel where possible, then verify the result. Wait for the coordinator's "
    "summary and report back.\n\n"
    + SPRINT_TASK
)

COORDINATOR_INSTRUCTIONS = """\
You are an implementation coordinator. You own the whole sprint end to end.

YOUR TOOLS: read_file, glob_files, grep_files, web_search. You CANNOT write
files and CANNOT run bash — you have no write_file or bash tool at all.

Your job is to decompose the pomodoro task into its modules and hand each one
to the best-fit subagent via your delegate_to_* tools:
  - delegate_to_coder: engine.py, storage.py, api.py and their test files
  - delegate_to_frontend_design: static/index.html, app.js, style.css
  - delegate_to_security_reviewer: a read-only audit of storage.py and api.py
    (SQL injection, input validation, oversized bodies, hardcoded credentials)
  - delegate_to_doc_writer: README.md
Run independent pieces in parallel (issue several delegate calls in one turn).
Have the coder fix anything security_reviewer finds, then do a final read-only
pass yourself over the files that exist to confirm the contract is met.

When you finish, return a short summary: which subagent wrote which file, the
audit outcome, and the exact path of the finished project.
"""

COORDINATOR_YAML_TEXT = (
    "name: " + COORDINATOR_NAME + "\n"
    "description: Use when a whole multi-module implementation sprint should "
    "run as one coordinated job — it decomposes the task and hands each module "
    "to the matching subagent.\n"
    "instructions: |\n"
    + "".join("  " + line + "\n" for line in COORDINATOR_INSTRUCTIONS.splitlines())
    + 'model: ""\n'
    "max_turns: 12\n"
    "tools:\n"
    "  - read_file\n"
    "  - glob_files\n"
    "  - grep_files\n"
    "  - web_search\n"
)

COORDINATOR_YAML = REPO_ROOT / "skills" / "subagents" / f"{COORDINATOR_NAME}.yaml"


def _write_coordinator() -> None:
    COORDINATOR_YAML.parent.mkdir(parents=True, exist_ok=True)
    COORDINATOR_YAML.write_text(COORDINATOR_YAML_TEXT, encoding="utf-8")


def _prompt(out: str) -> str:
    return "Perform the following task.\n\n" + SPRINT_TASK.format(out=out)


def _prompt_forced(out: str) -> str:
    return "Perform the following task.\n\n" + SPRINT_TASK_FORCED.format(out=out)


def _run_verify(out_dir: Path) -> int:
    """Copy the gate into {out} and run it; return verify_pass (0-5)."""
    (out_dir / "verify_impl.py").write_text(
        VERIFY_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    try:
        proc = subprocess.run(
            ["uv", "run", "python", str(out_dir / "verify_impl.py")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        print("    verify: TIMEOUT", flush=True)
        return 0
    for line in proc.stdout.splitlines():
        print("    " + line, flush=True)
    m = re.search(r"VERIFY_PASS (\d)/5", proc.stdout)
    return int(m.group(1)) if m else 0


def _run_pytest(out_dir: Path) -> int:
    """Run the model's own tests; return passed count (secondary metric)."""
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", "-q", str(out_dir)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("    pytest: TIMEOUT", flush=True)
        return 0
    tail = proc.stdout.strip().splitlines()
    if tail:
        print(f"    pytest: {tail[-1]}", flush=True)
    m = re.search(r"(\d+) passed", proc.stdout + proc.stderr)
    return int(m.group(1)) if m else 0


async def _run_mode(port: int, out: str, *, prompt: str, advanced: bool) -> dict[str, object]:
    """Run one pomodoro run; track delegation chain + tool use + verify/pytest."""
    import websockets

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws",
        max_size=2**24,
        ping_interval=30,
        ping_timeout=120,
    ) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        if advanced:
            await ws.send(json.dumps({"type": "set_advanced", "advanced": True}))
            assert json.loads(await ws.recv())["type"] == "advanced_changed"
        await ws.send(json.dumps({"type": "message", "content": prompt}))

        started = time.monotonic()
        active = 0
        max_concurrency = 0
        depth_by_run: dict[str, int] = {}
        agent_by_run: dict[str, str] = {}
        parent_by_run: dict[str, str | None] = {}
        pending_delegator: str | None = None
        types: set[str] = set()
        sub_turns = 0
        waves = 0
        last_was_delegate_call = False
        tool_uses: dict[str, int] = {}

        def _count(name: str) -> None:
            tool_uses[name] = tool_uses.get(name, 0) + 1

        while True:
            frame = json.loads(await ws.recv())
            t = frame["type"]
            if t == "tool_call":  # parent's own tool call
                name = frame["tool_call"]["name"]
                if name.startswith(DELEGATE_PREFIX):
                    if not last_was_delegate_call:
                        waves += 1
                    last_was_delegate_call = True
                    pending_delegator = "root"
                else:
                    last_was_delegate_call = False
                    _count(name)
            elif t == "subagent_event":
                ev = frame["event"]
                if ev.get("type") == "tool_call":
                    ev_name = ev["tool_call"]["name"]
                    if ev_name.startswith(DELEGATE_PREFIX):
                        pending_delegator = frame["run_id"]
                    else:
                        _count(ev_name)
                last_was_delegate_call = False
            elif t == "subagent_start":
                types.add(frame["agent"])
                agent_by_run[frame["run_id"]] = frame["agent"]
                parent = (
                    None
                    if pending_delegator is None or pending_delegator == "root"
                    else pending_delegator
                )
                parent_by_run[frame["run_id"]] = parent
                base = (
                    0
                    if pending_delegator is None or pending_delegator == "root"
                    else depth_by_run.get(pending_delegator, 0)
                )
                depth_by_run[frame["run_id"]] = base + 1
                pending_delegator = None
                active += 1
                max_concurrency = max(max_concurrency, active)
                last_was_delegate_call = False
            elif t == "subagent_end":
                active -= 1
                sub_turns += int(frame.get("turns", 0))
                last_was_delegate_call = False
            elif t == "approval_required":
                await ws.send(
                    json.dumps(
                        {
                            "type": "approval",
                            "tool_call_id": frame["tool_call"]["id"],
                            "decision": "y",
                        }
                    )
                )
                last_was_delegate_call = False
            elif t == "run_done":
                break
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")
            else:
                last_was_delegate_call = False

        def _path(run_id: str) -> list[str]:
            path: list[str] = []
            cur: str | None = run_id
            while cur is not None:
                path.append(agent_by_run[cur])
                cur = parent_by_run.get(cur)
            return path[::-1]

        chains = sorted(
            {tuple(_path(rid)) for rid in depth_by_run},
            key=lambda p: (len(p), p),
        )
        out_dir = Path(out)
        verify_pass = _run_verify(out_dir)
        pytest_passed = _run_pytest(out_dir)
        return {
            "seconds": time.monotonic() - started,
            "delegations": len(depth_by_run),
            "waves": waves,
            "max_concurrency": max_concurrency,
            "depth": max(depth_by_run.values(), default=0),
            "types": len(types),
            "sub_turns": sub_turns,
            "web_searches": tool_uses.get("web_search", 0),
            "greps": tool_uses.get("grep_files", 0) + tool_uses.get("glob_files", 0),
            "writes": tool_uses.get("write_file", 0),
            "bash": tool_uses.get("bash", 0),
            "chain": [list(c) for c in chains],
            "verify_pass": verify_pass,
            "pytest_passed": pytest_passed,
        }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping (exit 2)", file=sys.stderr)
        return 2
    if not VERIFY_TEMPLATE.is_file():
        print(f"missing {VERIFY_TEMPLATE} — run Task 3 first", file=sys.stderr)
        return 1

    _write_coordinator()
    port = _free_port()
    env = {
        **os.environ,
        "HARNESS_SUBAGENTS": "1",
        "HARNESS_SUBAGENT_BUDGET": "120",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "harness.web.server:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    server_log: deque[str] = deque(maxlen=60)

    def _drain_server() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            server_log.append(raw.decode("utf-8", errors="replace").rstrip())

    threading.Thread(target=_drain_server, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="harness-pomo-"))
    runs: list[dict[str, Any]] = []
    try:
        _wait_health(port)
        for label, forced, advanced in GROUPS:
            for i in range(1, RUNS + 1):
                out_dir = tmp / f"{label}-{i}"
                out_dir.mkdir(parents=True, exist_ok=True)
                prompt = _prompt_forced(str(out_dir)) if forced else _prompt(str(out_dir))
                try:
                    metrics = asyncio.run(
                        asyncio.wait_for(
                            _run_mode(port, str(out_dir), prompt=prompt, advanced=advanced),
                            timeout=900.0,
                        )
                    )
                except TimeoutError:
                    print(f"  {label}-{i}: TIMEOUT after 900s — skipped", flush=True)
                    continue
                runs.append({"mode": label, "out": str(out_dir), "metrics": metrics, "run": i})
                chains = [" -> ".join(c) for c in cast(list[list[str]], metrics["chain"])]
                print(
                    f"  ran {label}-{i}: verify={metrics['verify_pass']}/5 "
                    f"pytest={metrics['pytest_passed']} deleg={metrics['delegations']} "
                    f"waves={metrics['waves']} conc={metrics['max_concurrency']} "
                    f"depth={metrics['depth']} types={metrics['types']} "
                    f"web={metrics['web_searches']} bash={metrics['bash']} "
                    f"sub_turns={metrics['sub_turns']} wall={metrics['seconds']:.1f}s",
                    flush=True,
                )
                print(
                    f"    chains: {' | '.join(chains) if chains else '(none)'}",
                    flush=True,
                )
        if not runs:
            print("  no runs completed — aborting", file=sys.stderr)
            return 1

        by_mode = {g[0]: [r for r in runs if r["mode"] == g[0]] for g in GROUPS}
        print("\n== pomodoro-sprint comparison (verify_pass 0-5 = primary) ==")
        for label, _forced, _adv in GROUPS:
            rs = by_mode[label]
            if not rs:
                print(f"  {label:15s} n=0 (no completed runs)")
                continue
            vps = [float(r["metrics"]["verify_pass"]) for r in rs]
            pps = [float(r["metrics"]["pytest_passed"]) for r in rs]
            walls = [float(r["metrics"]["seconds"]) for r in rs]
            depths = [float(r["metrics"]["depth"]) for r in rs]
            concs = [float(r["metrics"]["max_concurrency"]) for r in rs]
            print(
                f"  {label:15s} n={len(rs)}  verify {_fmt_spread(vps)}/5  "
                f"pytest {_fmt_spread(pps)}"
            )
            print(
                f"      wall {_fmt_spread(walls)}s  depth {_fmt_spread(depths)}  "
                f"conc {_fmt_spread(concs)}"
            )
        print("\n== delegation chains (deduped per group) ==")
        for label, _forced, _adv in GROUPS:
            chains: set[str] = set()
            for r in by_mode[label]:
                for c in r["metrics"]["chain"]:
                    chains.add(" -> ".join(c))
            print(
                f"  {label:15s} "
                + (" | ".join(sorted(chains)) if chains else "(none)")
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"POMO BENCH FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("--- last server log lines ---", file=sys.stderr)
        for line in server_log:
            print(line, file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        COORDINATOR_YAML.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 静态检查脚本**

  Run: `uv run ruff check scripts/e2e_subagents_compare_v6.py`
  Expected: `All checks passed!`。脚本不进 mypy 范围(`mypy src`),只查 src。
  **注意**:不要在本任务真跑 benchmark——9 个 run 要 2-4 小时,留给 Task 5 之后的完整实验。

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e_subagents_compare_v6.py
git commit -m "bench: pomodoro-sprint compare runner (v6, objective verify gate + WS tracking)"
```

---

### Task 5: 质量门 + 真实 WS 冒烟

**Files:** 无新文件(只跑命令)。

- [ ] **Step 1: 全量质量门**

  Run: `uv run ruff check . && uv run mypy src && uv run pytest -q`
  Expected: ruff `All checks passed!`、mypy 无错、pytest 全绿(之前 255 passed,新增测试后 ≥257)。若有失败就地修。

- [ ] **Step 2: 真实 WS 冒烟(1 个 forced-advanced run)**

  Run:
  `HARNESS_COMPARE_GROUPS=forced-advanced HARNESS_COMPARE_RUNS=1 uv run python scripts/e2e_subagents_compare_v6.py`
  Expected(对照 spec §8 第 2 条):
  - 链出现 `root -> coordinator -> coder` 之类深度 2 的路径;
  - `conc`(并发峰值)≥ 2;
  - `verify` ≥ 3/5;
  - 输出里 5 个 PASS/FAIL 行 + `VERIFY_PASS N/5`。
  若深度只有 1 或 verify=0:先读 server log 定位(coordinator 没拿到 delegate 工具 / 模型没写 `{out}` / 门断言过严),修复后重跑,别直接进全量。

- [ ] **Step 3: Commit 收尾**

  冒烟通过后,本计划的任务即完成。提交任何修复改动,然后写结果文档是**实验阶段**(非本计划范围)的工作。

---

## Self-Review(计划自审)

**1. Spec coverage** — 逐节核对:
- §2 任务契约:SPRINT_TASK 固化(engine/storage/api/static/README 逐项)→ Task 4。
- §3 客观门 5 项 + 安全嗅探并入 storage/api → Task 3。
- §4 三组 + coordinator 只读 + normal 也写 coordinator.yaml → Task 4。
- §5 skill 注入(coding.md / security-review.md / security_reviewer.yaml / coder.yaml skill 字段)→ Task 1+2。
- §6 指标(verify_pass 主轴、pytest 次级、结构、成本)→ Task 4。
- §7 脚本结构(复用 v2 helpers、v4 追踪、budget=120)→ Task 4。
- §8 验证方式(质量门 + WS 冒烟 + 完整 9 run)→ Task 5(完整 9 run 是实验阶段)。
- §9 风险 R3(forced-normal 超时 skip)/ R5(参考实现验证门)/ R7(budget=120)→ 已在对应任务落地。
- §10 交付物:两个 skill + 新 subagent + coder.yaml 修改 + v6 脚本 → Task 1/2/4;结果文档是实验阶段。
- §11 成功判据 → 结果文档阶段判定,非本计划代码范围。

**2. Placeholder scan** — 无 TBD/TODO;每个任务代码块完整可复制;Task 3 参考实现是刻意「不过度工程」的完整实现。

**3. Type consistency** — 关键接口链核对:
- `load_subagent_skill(name) -> str`(Task 1 测试断言 ↔ registry.py 实现)。
- `PomodoroEngine(work_minutes, break_minutes, clock)` / `SessionStore(path)` / `create_server(store, static_dir, host, port)` 在 SPRINT_TASK、参考实现、verify 模板三处**逐字一致**(类名、方法名、签名、返回类型)。
- verify 模板 5 个 gate 名(engine/storage/api/static/readme)与 v6 解析 `VERIFY_PASS (\d)/5` 一致。
- `_run_mode(port, out, *, prompt, advanced)` 在 Task 4 定义与调用一致;metrics 键名(verify_pass/pytest_passed/depth/max_concurrency/chain)在收集与汇总两处一致。
- `HARNESS_SUBAGENT_BUDGET=120` 在 v6 的 env 字典里,spec §7 逐字一致。

**4. 已知取舍(如实记录)**
- coder.yaml 的常驻 ruff 指令与 benchmark 的「不跑 ruff」brief 冲突:task brief 优先;最坏多一轮工具调用。不为此改常驻指令(spec §5 只加 skill 字段)。
- verify 的 SQL 嗅探是启发式(f-string/`+` 拼 SQL):reference 用白名单列名 + 先拼 sql 再 execute,不在 execute 行内触发,已验证不误伤。
- 并发计数用独立 db 文件保证精确 800(spec 说「共 800」,基线行不混入)。
