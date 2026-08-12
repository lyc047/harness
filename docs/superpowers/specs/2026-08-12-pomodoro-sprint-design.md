# 全栈微型产品冲刺(番茄钟服务)— Benchmark Design (v6)

## 1. 背景与目标

上一轮(open-ended arch-judge, v4)结论:**normal/advanced/depth2 质量无统计差异**,根因是该任务形状(单篇架构报告)用不上 advanced 的三板斧。随后用户定方向:**advanced 追求明显更高的工作质量,可放宽复杂度和 token 消耗**;手段是**注入 GitHub 大众认可度高的 skills,让 subagent 比主 agent 更强**。

本实验(场景 A 全栈产品冲刺)把两者合起来:

- **任务形状**改为「多模块、需异构技能的微型产品冲刺」——真正用上 advanced 的深链路由 + 并行 fan-out + 上下文隔离。
- **客观通过门**为主轴(不再依赖易被噪声淹没的 LLM judge):harness 自写 `verify_impl.py`,对每个模块做固定断言。
- **技能注入**为产品级永久改动:新增 `coding`(coder)与 `security-review`(新 subagent `security_reviewer`)两个 bundled skill,取法 GitHub 公认方法论(见 §5)。

**已与用户确认的决策**:
- 场景:A 全栈微型产品冲刺(协作番茄钟简化版——去掉多用户/实时,纯标准库)。
- 分组:三组含对照(normal / forced-normal / forced-advanced)。
- 技能:新增 `coding` + `security-review`;security_reviewer 为新 subagent;不需要 data_analyst/marketer/ops(YAGNI)。

---

## 2. 任务定义

在临时 scratch 目录(`tempfile.mkdtemp(prefix="harness-pomo-")`)实现**单用户番茄钟服务**,纯标准库,4 个模块 + 前端 + 测试 + README:

```
{out}/
  engine.py          — 计时状态机(可注入 clock,方便测试)
  storage.py         — sqlite 持久化
  api.py             — HTTP 服务(stdlib http.server + json)
  static/index.html  — 前端页面
  static/app.js      — 前端逻辑
  static/style.css   — 前端样式
  test_engine.py / test_storage.py / test_api.py — 模型自测
  README.md
  verify_impl.py     — harness 生成,不参与模型运行
```

**模块契约**(任务提示词里固化,verify 按此断言):

- `engine.py` — `class PomodoroEngine(work_minutes: int = 25, break_minutes: int = 5, clock: Callable[[], float] = time.monotonic)`,方法 `start()`(idle→work)、`pause()`(冻结 elapsed)、`resume()`、`reset()`(回 idle)、`state() -> str`("idle"|"work"|"break"|"paused")、`elapsed_seconds() -> float`。`clock` 可注入以便测试(测试性本身就是设计质量信号)。
- `storage.py` — `class SessionStore(path: str | Path)`,方法 `create(duration_s: int, started_at: float, note: str = "") -> int`、`get(session_id: int) -> dict | None`、`list() -> list[dict]`、`update(session_id: int, **fields: object) -> bool`、`delete(session_id: int) -> bool`。sqlite 持久化,close→reopen 数据仍在。
- `api.py` — `def create_server(store: SessionStore, static_dir: str | Path, host: str = "127.0.0.1", port: int = 0) -> http.server.HTTPServer`。端点:`GET /`(serve static/index.html)、`GET /api/sessions`(JSON 列表)、`POST /api/sessions`(JSON `{"duration_s": int, "note": str}`,201 + body 含 `id`)、`GET /api/sessions/{id}`(存在 200,缺失 404)。
- `static/` — `index.html` 引用 `style.css` + `app.js`,含计时显示元素与 start/pause/reset 控件、语义标签(`<button>` 等);`app.js` 定义 `startTimer`/`pauseTimer`/`resetTimer` 并 `fetch("/api/sessions")`;`style.css` ≥15 条规则且被引用。
- `README.md` — 含 Overview / Run / API / Tests 四节 + 启动命令示例。

**工程要求**:纯标准库;`from __future__ import annotations`;builtin generics;每模块自己的 pytest 测试文件;最终 `uv run python {out}/verify_impl.py` 全过;`uv run pytest -q {out}` 全绿。**不要求 ruff**(scratch 目录无 ruff 配置,省得 subagent 空转)。

---

## 3. 客观通过门(防糊弄)

模型自测不可信,硬门由 harness 自带的 `verify_impl.py` 承担,harness 在 run 结束后把源码写进 `{out}/` 再执行。逐模块固定断言,产出 **5 项 PASS 数(0-5)** 为主轴:

| 模块 | 关键断言(合理实现必过、糊弄过不去) |
|------|-------------------------------------|
| engine | 初始 `state()=="idle"`;`start()`→work;注入假 clock 推进 work_minutes→break,推进 break_minutes→work(周期闭环);pause 冻结 elapsed、resume 续走;reset 回 idle;elapsed 单调;8 线程×500 混合 `start/pause/resume/state/reset` 并发调用不崩、state 始终合法 |
| storage | create→get 往返字段一致;非法 id `get→None`/`delete→False`/`update→False` 不崩;list 含新建;close→reopen 数据仍在;8 线程×100 `create` → list 共 800 |
| api | 起 server 于 `port=0`(随机端口),POST 合法→201 + body 含 id;非法 JSON→400;缺 `duration_s` / 非正→400;GET 缺失 id→404;GET `/` 返回含 "pomodoro" 标记的 HTML;请求体超限(>64KB)→413/400 且 server 仍存活 |
| static | `index.html` 引用 `style.css`+`app.js`、含计时元素与 start/pause/reset 控件、有 `<button>`;`app.js` 含三函数 + fetch `/api/sessions`;`style.css` ≥15 个 `{` |
| README | 含 "Overview"/"Run"/"API"/"Tests" 四个节头 + 启动命令示例行 |

**安全静态嗅探**(并入 storage/api 两门的断言,不单独计数,作为硬门一部分):
- 全目录无 `eval(` / `exec(`。
- 无硬编码 secret 字面量(`password = "` / `secret = "` / `api_key = "` + 非空串)。
- `storage.py` 的 SQL 用参数绑定(`?` 占位),无 f-string/字符串拼接拼 SQL。

**通过率 = verify 的 5 项 PASS 数(0-5)**,组间对比主轴。

---

## 4. 分组与因果设计

三组共用同一套 bundled subagents + 运行时 coordinator,唯一变量是 `advanced` 开关:

| 组 | 提示词 | advanced | 预期 coordinator 行为 |
|----|--------|----------|------------------------|
| **normal** | `SPRINT_TASK`(无委派指令) | False | root 自己实现(可能串行写 5 文件),或偶尔 depth-1 委派一次 |
| **forced-normal** | `SPRINT_TASK_FORCED`(强制 delegate_to_coordinator) | False | coordinator **无 delegate 工具**→ 空转返回;root 兜底串行。展示"结构上做不到分工" |
| **forced-advanced** | `SPRINT_TASK_FORCED`(同) | True | coordinator **拆模块→并行派给 coder(engine/storage/api)/frontend_design(static)/security_reviewer(安全过检)/doc_writer(README)**→ 合流验证;root 只见摘要 |

`SPRINT_TASK_FORCED` 措辞:要求先 delegate 给 coordinator,coordinator 负责把各模块派发出去,root 不自己写任何东西。

三组共用同一 coordinator.yaml(只读:read/glob/grep/web_search,无 write/bash)。**normal 组也写 coordinator.yaml** 但不引导使用(变量只有 advanced)。

---

## 5. Subagent 与技能注入(产品级永久改动)

**核心原则**:新技能放进 `src/harness/skills/bundled/subagents/`(随包发布,runtime `skills/subagents/` 同名文件覆盖),走现有 `skill:` 字段机制。

| subagent | 工具 | skill(注入) | 职责 | 来源 |
|----------|------|-------------|------|------|
| `coder` | read/write/glob/grep/bash/web_search | **`coding`(新增)** | engine/storage/api 三个模块 + 各自测试 | 从 [obra/superpowers](https://github.com/obra/superpowers) TDD + [wshobson/agents](https://github.com/wshobson/agents) code-review 提炼 |
| `frontend_design` | read/write/bash | frontend-design(已有) | static/ 三件套 | [anthropics/skills](https://github.com/anthropics/skills) |
| `security_reviewer`(**新 subagent**) | read/glob/grep/web_search(**只读,无 write/bash**) | **`security-review`(新增)** | 对 api.py/storage.py 做安全过检(注入/越权/敏感信息),返回发现清单 | [unitoneai OWASP ASVS](https://www.npmjs.com/package/@unitoneai/skills) + [r03-anthropics-skills-security](https://github.com/TeamArtisanThrive/r03-anthropics-skills-security) 提炼成可勾选短清单 |
| `doc_writer` | read/write/bash | doc-coauthoring(已有) | README | [anthropics/skills](https://github.com/anthropics/skills) |
| `coordinator` | read/glob/grep/web_search(只读) | 无 | 拆模块 → 派发 → 合流验证(advanced 下才有 delegate 工具) | 运行时写,同 v4 |

**为什么 security_reviewer 只读**:它是审计者不是写者——专职过检、返回发现清单,由 coder 修复(或 coordinator 决定是否修复)。这正是「给 subagent 强、给主 agent 臃肿」的典型:几百行方法论,主 agent 常年挂着是负担,下派时才加载。

**新增/修改文件**:
- 新增 `src/harness/skills/bundled/subagents/coding.md` — 方法论:先读契约后写、小步实现+每步验证、边界与并发健壮性、参数化 SQL、禁 eval/exec、防「AI 默认代码」(每处取舍给理由)。
- 新增 `src/harness/skills/bundled/subagents/security-review.md` — 可勾选短清单:输入校验/注入/硬编码凭据/越权/请求超限,只读审计,输出结构化发现。
- 新增 `src/harness/skills/bundled/subagents/security_reviewer.yaml` — `skill: security-review`,tools 只读四项,max_turns 8,description 含触发词("Use when code security needs auditing; delegate by default.")。
- 修改 `src/harness/skills/bundled/subagents/coder.yaml` — 加 `skill: coding` 字段。

**不新增**:data_analyst / marketer / ops——本任务用不上,YAGNI(未来可随场景加)。

---

## 6. 指标

每组 × RUNS(=3,env `HARNESS_COMPARE_RUNS`):

**客观通过门**(主轴):
- `verify_pass`:verify_impl.py 的 5 项 PASS 数(0-5)。
- `pytest_passed`:模型 pytest 的 passed 数(次级)。

**结构**(WS 帧追踪,复用 v4 链追踪逻辑):
- `depth`、`max_concurrency`、`waves`、`types`、`delegations`、`sub_turns`、`chain`、`web_searches`、`bash`。

**成本**(advanced 允许更贵,记录不作成功判据):
- `seconds`、`turns`。

---

## 7. 脚本结构

**新建 `scripts/e2e_subagents_compare_v6.py`**:

- 复用 v2 的 `REPO_ROOT/_fmt_spread/_free_port/_wait_health`,v4 的 WS 追踪(`_run_mode` 改造为可传 `prompt`/`advanced`)。
- `SPRINT_TASK`(任务提示词,含 `{out}` 占位)、`SPRINT_TASK_FORCED`(强制 coordinator 版)。
- `VERIFY_SOURCE`(verify_impl.py 源码字符串,harness 在 run 后写入 `{out}/` 并执行)。
- `COORDINATOR_YAML_TEXT`(v6 版,提示 coordinator 拆模块派发)+ `_write_coordinator()`。
- `_run_mode(port, out, *, prompt, advanced)` → metrics(结构 + 成本)。
- 三组循环:`GROUPS = [("normal", False, False), ("forced-normal", True, False), ("forced-advanced", True, True)]`,字段 = `(forced, advanced)`。
- 每 run 后:写 `verify_impl.py` → `uv run python {out}/verify_impl.py` 收集 `verify_pass`(解析 PASS/FAIL);`uv run pytest -q {out}` 收集 `pytest_passed`。
- 汇总表:每组 median verify_pass(0-5)、median wall、median depth/concurrency、chain 去重样例。
- finally:terminate server + unlink coordinator.yaml。

**注意**:服务端启动时设 `HARNESS_SUBAGENTS=1`(同 v4);advanced 组的 coordinator + 4 个 delegate 目标(coder/frontend_design/security_reviewer/doc_writer,其中 coder 可能被拆成多次委派)会吃掉较多 subagent 轮次,启动环境里显式设 `HARNESS_SUBAGENT_BUDGET` 调高(如 120)以免预算耗尽被误判为「结构上做不到」,并在文档里如实标注这个非等量因素。

---

## 8. 验证方式

1. 质量门:`uv run ruff check . && uv run mypy src && uv run pytest -q` 全绿(注意新增 bundled 文件会改变 `example_subagents()` 集合,现有测试是子集断言不会挂,但确认新增 security_reviewer 通过 delivery-contract 测试)。
2. 真实 WS 冒烟:1 个 forced-advanced run,断言形成 `coordinator -> coder` 深链 + 并发峰值 ≥2 + verify_pass ≥3(先手工核对 verify_impl.py 契约与 4 个模块可测)。
3. 完整 n=3 × 3 组 ≈ 9 个 run,后台跑(每 run 可能 300–900s,预计 2–4 小时),如实记录超时/失败。

---

## 9. 风险与对策

- **R1 模型不写 `{out}` 而写别处** → verify 失败,如实记录;提示词里把 `{out}` 路径写死。
- **R2 模块 API 名与契约不符** → verify 按契约断言,FAIL 计入通过率(规范遵从度本身是测点)。
- **R3 forced-normal 超时**:coordinator 空转 → root 兜底,可能慢。timeout 900s,超时 skip(如实记录 n)。
- **R4 并发 4+ 个子 agent 同时打 LLM API** → 可能慢或限流。这是 advanced 的核心机制,如实记录;不因慢而失败,只看 verify 与结构。
- **R5 verify 断言过严/过松** → 断言设计为「合理实现必过、糊弄过不去」(如并发 smoke、容量驱逐、持久化、请求超限、安全静态嗅探),先手工验证一份人工实现能全过。
- **R6 前端门过弱** → static 只做结构断言(引用/函数/控件存在),不评美感;设计质量留作可选次级 judge,不卡通过率。
- **R7 subagent 预算耗尽** → 启动设 `HARNESS_SUBAGENT_BUDGET=120`,并把该非等量因素如实写进结果文档。

---

## 10. 交付物

- 新 bundled skill:`src/harness/skills/bundled/subagents/coding.md`、`security-review.md`
- 新 subagent:`src/harness/skills/bundled/subagents/security_reviewer.yaml`
- 修改:`src/harness/skills/bundled/subagents/coder.yaml`(+`skill: coding`)
- `scripts/e2e_subagents_compare_v6.py`(生成 + 客观门 + 汇总)
- 结果文档 `docs/superpowers/2026-08-12-pomodoro-sprint-results.md`(中文)
- 9 份 `{out}` 目录(含 4 模块 + 前端 + 测试 + README + verify_impl.py)

---

## 11. 成功判据

- **客观可分辨**:forced-advanced 的 verify_pass 中位数 **≥** forced-normal,且链结构(深度 2、并发峰值 ≥2)在 forced-advanced 稳定出现、在 forced-normal 不出现。
- 若 forced-advanced 与 forced-normal 的 verify_pass 也持平 → 如实报告「此规模下无差异」,不强行包装(consistent with 上一轮结论)。
