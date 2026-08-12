# Open-Ended Architecture-Design Judge Benchmark

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `scripts/e2e_subagents_compare_v4.py` — a real-world, open-ended engineering-problem benchmark (a multi-region realtime whiteboard platform architecture design) with **no single correct answer**, graded by a **blind LLM judge** (6-dimension rubric, 3 samples/run, median + variance) instead of the deterministic rubric used by v2/v3. Run all three modes — normal, advanced, and forced depth-2 — to surface differences in *problem-solving capability*, not factual recall.

**Architecture:** Reuse the proven v3 WS-driver (`_run_mode` with per-run delegation-chain tracking, `_wait_health`, `_free_port`, coordinator override for the forced-depth-2 group). New pieces are: the open-ended `TASK_PROMPT` (fixed scene + 6 mandated report sections), the `JUDGE_RUBRIC` (6 dimensions × 1-10), `_blind_render` (strips mode/run labels from the report before judging), `_judge_one` (blind call to the same DeepSeek model, 3 samples, per-dimension median + variance), and a three-group runner that dumps both raw reports and a deidentified `judge_results.md`.

**Tech Stack:** Python 3.11, existing harness WS frames (`ready`/`set_advanced`/`message`/`run_done`/`subagent_start`/`subagent_event`), `openai.AsyncOpenAI` (base_url `https://api.deepseek.com`, key from `.env`), stdlib only otherwise.

## Global Constraints

- **Never print, log, or echo any API key** (DEEPSEEK / TAVILY / any). The judge reads the key from `.env` inside the script; nothing key-like ever appears in output, logs, or the report.
- The judge input is **deidentified**: it receives the fixed task + the report content ONLY. No mode label, no run number, no script path. Three groups are interleaved at random before judging.
- Judge uses the **same DeepSeek model as generation** (fairness: both groups are generated and judged by the same model). Report this honestly; never present the score as absolute truth.
- Report is passed to the judge truncated to a hard cap (default `MAX_JUDGE_CHARS = 8000`) to bound token cost across 3 samples × N runs.
- Two-phase execution (cheapest-first, as before): smoke `HARNESS_COMPARE_RUNS=1` per group → confirm task completes and judge returns sane per-dimension scores → then `HARNESS_COMPARE_RUNS=3`.
- Quality gate on the script: `uv run ruff check scripts/e2e_subagents_compare_v4.py && uv run python -m py_compile scripts/e2e_subagents_compare_v4.py`. (`scripts/` is outside `mypy src`.)
- No changes to `src/harness/**`. This is a benchmark script only.

---

## Task definition (`TASK_PROMPT`, fixed, given verbatim to every run)

Scene: *"Design the system architecture for a realtime collaborative whiteboard platform for teams — think a browser-based infinite canvas that many users edit together live. Target: 1M daily active users, multi-region (US + EU + APAC), <200 ms perceived latency for collaborative edits, 99.95% availability, a lean startup budget. There is no single correct answer — we judge the quality of your reasoning and trade-offs, not whether you guessed a specific stack."*

Mandated report sections (same 6 for every run, in this order — this is what makes outputs comparable):

1. **Requirements & constraint analysis** — non-functional requirements (scale, latency, availability, budget, team size) and how they bind the design.
2. **Architecture overview** — components and data flow; a short ASCII diagram is welcome.
3. **Subsystem designs** — one subsection each, with an explicit trade-off per subsystem: (a) storage & consistency, (b) realtime sync engine (WebSocket / CRDT-vs-OT choice), (c) message fan-out & presence, (d) search & indexing, (e) auth & tenant isolation, (f) observability, (g) multi-region & failover, (h) cost control.
4. **Key technology choices & rationale** — concrete candidate technologies named, with why/why-not.
5. **Risks & failure scenarios** — top bottlenecks, single points of failure, and their mitigations.
6. **Evolution roadmap** — how the design scales from day 1 to target, and what is deliberately deferred.

Write the report to `{out}/report.md`.

## Judge protocol (`JUDGE_RUBRIC`)

Blind judge call — system prompt:

```
You are a principal systems architect grading a peer's architecture-design report.
Grade ONLY what is in the report. You do not know the author, the tool, or the mode.
Score each dimension 1-10 (integer) and give one short sentence of justification per
dimension, then a total (sum, max 60). Use exactly this format, one line per dimension:
<dim>: <score>/10 — <one-sentence justification>
TOTAL: <sum>/60
```

Dimensions (each defined in the judge prompt):

| # | Dimension | What it rewards |
|---|---|---|
| 1 | Requirements understanding | non-functional constraints named and correctly bound to design choices |
| 2 | Architectural soundness | coherent component split, clean data flow, internal self-consistency |
| 3 | Trade-off awareness | explicit pros/cons per decision; avoids over-engineering |
| 4 | Completeness | all 8 required subsystems covered; all 6 sections present |
| 5 | Deployability | concrete, implementable choices (named tech, interfaces, ops) |
| 6 | Risk & evolution | bottlenecks/SPOFs identified; a believable scaling path |

Sampling: each report is judged **3 times** (independent calls, interleaved order across all runs). Per-dimension median + the 3-sample spread are reported. TOTAL = sum of per-dimension medians.

## Script structure

```
scripts/e2e_subagents_compare_v4.py
├── TASK_PROMPT / JUDGE_RUBRIC / MAX_JUDGE_CHARS = 8000
├── COORDINATOR_YAML_TEXT   # adapted from v3: no write/bash, must hand off to doc_writer
├── _run_mode(port, out, mode)  # from v3, incl. chain tracking; normal/advanced toggle
├── _blind_render(out) -> str   # read report.md, truncate to MAX_JUDGE_CHARS, NO labels
├── _judge_one(rendered) -> dict[str, Any]  # 3 blind samples -> per-dim median/spread/total
└── main()  # per group: RUNS runs -> reports -> judge -> dump judge_results.md + summary table
```

Mode enum for the three groups: `normal`, `advanced`, `depth2` (depth2 = advanced + the v3 coordinator override so the parent is forced to chain through a non-writing coordinator).

## Validation

1. Smoke (`HARNESS_COMPARE_RUNS=1`, all three groups): every run produces a `report.md` with all 6 sections; judge returns 6 scores + total for every run; per-run spread across 3 samples is reported.
2. Full (`HARNESS_COMPARE_RUNS=3`): 3 runs per group × 3 groups, all judged. Outputs a per-group table (per-dimension median, total median, variance) and a `judge_results.md`.
3. My human review pass: I read the highest- and lowest-delta output pairs and sanity-check the judge's verdicts against the actual reports, reported in the final summary as "judge variance + my review".

## Risks

- **R1 judge variance:** 3 samples damp but do not eliminate it — the spread is printed per run and the final delta is reported with its uncertainty.
- **R2 cost:** open-ended generation (3-6 min/run) × 3 groups × N, plus 3 judge calls/run at ~8k chars each. Smoke keeps the cost bounded before committing to n=3.
- **R3 judge ceiling:** same-model self-judging is fair but bounded by the model's own standards; scores are treated as *relative* signal between modes, not absolute quality.
- **R4 task width:** an over-broad scene yields incomparable reports — the 6 mandated sections + 8 required subsystem subsections exist precisely to keep outputs structurally comparable.
