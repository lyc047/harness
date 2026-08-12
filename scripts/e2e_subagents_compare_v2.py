"""Targeted capability comparison v2: external-dependency audit vs advanced mode.

Same harness as e2e_subagents_compare.py (ONE web server, RUNS real-model runs
per mode), but the task is deliberately engineered to *exercise* the three
subagent optimizations the old benchmark was blind to:

  * Task 2 (per-subagent tool allowlists)  — the task routes three phases to
    three differently-scoped subagents: search (grep/glob ONLY, no write),
    researcher (web_search + read, no write), doc_writer (read + write). The
    report CANNOT be produced by a single all-tools subagent run.
  * Task 3 (web_search builtin)            — the task requires citing CURRENT
    official doc URLs for the external services the repo talks to; fabricating
    one (reserved-TLD domain) or omitting URLs is scored down. web_search call
    counts are also surfaced directly in the verdict.
  * Task 1 (delegation chaining nudge)     — the phases are structured as a
    handoff chain; depth-2 is still inductive, but the WS tracker records it
    when it happens.

Scores: A orchestration structure (25), B resource efficiency (30), C outcome
quality (45). C is fully deterministic — no LLM calls:

  gate           report exists + all required section headings
  call_coverage  real out-of-process touchpoint FILES mentioned in report /
                 total transport files (locating them requires grep/glob)
  url_citation   distinct real-looking https URLs >= MIN_URLS (requires the
                 researcher's web_search; guessing produces traps)
  clean          cited `path:line` locations that don't exist (hallucinated),
                 plus RFC-2606 reserved-TLD URLs (.example/.invalid/.test)

Per-mode aggregates are medians with min-max spread. Exit codes:
    0  PASS (both modes completed)
    1  FAIL (a mode errored or scoring could not run)
    2  no API key configured
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "harness"
sys.path.insert(0, str(REPO_ROOT))

# ---- The task's gold set: every out-of-process / network touchpoint file ----
# Deterministically scanned from the repo, not hand-picked. Covers outbound
# HTTP (urlopen, AsyncOpenAI), SSH (paramiko), MCP stdio, the inbound WebSocket
# server, and local subprocess launches — everything a thorough audit finds.
TRANSPORT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("http", re.compile(r"urlopen\s*\(")),
    ("ssh", re.compile(r"paramiko\.SSHClient")),
    ("mcp_stdio", re.compile(r"ClientSession\s*\(")),
    ("websocket", re.compile(r"websocket")),
    ("openai", re.compile(r"AsyncOpenAI")),
    ("subprocess", re.compile(
        r"create_subprocess_exec|create_subprocess_shell|subprocess\.(run|Popen)"
    )),
    ("server_bind", re.compile(r"uvicorn\.run")),
]

RUNS = int(os.environ.get("HARNESS_COMPARE_RUNS", "3"))
DELEGATE_PREFIX = "delegate_to_"

# C rubric constants
HEADINGS = ["## Network touchpoints", "## External services", "## Sources"]
MIN_REPORT_CHARS = 800
MIN_URLS = 4                      # distinct real https URLs required for full marks

# RFC 2606 reserved namespaces — cannot be a real service; any URL here is
# fabricated: reserved TLDs (.example/.invalid/.test/.localhost) and the IANA
# documentation second-levels (example.com/.net/.org, and any subdomain of them).
RESERVED_SUFFIXES = (
    ".example.com", ".example.net", ".example.org",
    ".example", ".invalid", ".test", ".localhost",
)


def _host(url: str) -> str:
    """Lowercased hostname of a URL (strips scheme and any path/port)."""
    host = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return host.split(":", 1)[0].lower()


def _scan_transports() -> dict[str, list[int]]:
    """relpath -> transport-line numbers, scanned from src/harness at runtime."""
    hits: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = path.relative_to(REPO_ROOT).as_posix()
        for i, line in enumerate(lines, start=1):
            if any(pat.search(line) for _, pat in TRANSPORT_PATTERNS):
                hits.setdefault(rel, []).append(i)
    return hits


def _prompt(out: str) -> str:
    headings = ", ".join(f"'{h}'" for h in HEADINGS)
    return (
        "Audit the external / out-of-process I/O touchpoints of this codebase "
        "and write a markdown report.\n"
        f"SCOPE: scan src/harness/ (every .py under it) for all places that "
        "communicate with anything OUTSIDE the local Python interpreter — "
        "outbound HTTP (urllib/urlopen, OpenAI-compatible HTTP clients), SSH, "
        "WebSocket server endpoints, MCP stdio transports, and local subprocess "
        "launches. For each, record the exact `path:line`, the transport, and "
        "the peer/service it talks to.\n"
        "THIS IS A THREE-PHASE TASK. Delegate each phase to the matching "
        "subagent so they run as separate specialized agents:\n"
        "  PHASE 1 (locate) — use the delegate_to_search tool. It greps for "
        "transport primitives (urlopen, paramiko.SSHClient, ClientSession, "
        "websocket) and returns the exact `path:line` of every hit. It CANNOT "
        "write files — it only locates.\n"
        "  PHASE 2 (verify) — use the delegate_to_researcher tool for the "
        "external services those touchpoints talk to (Bing / DuckDuckGo / "
        "Tavily web search, paramiko/SSH, the MCP protocol, the WebSocket/ASGI "
        "server). It MUST call the web_search tool for each service and return "
        "its CURRENT official documentation URL — a real https URL from the "
        "search results, never an invented one. Use AT MOST ONE web_search call "
        "per service: if it returns a usable URL, take it and move on; if it "
        "returns nothing, record that and move on — do not retry or rephrase "
        "the same query. It CANNOT write files either.\n"
        "  PHASE 3 (write) — use the delegate_to_doc_writer tool, handing it "
        "both subagents' findings. It has write access and writes the report "
        f"to {out}/report.md.\n"
        f"REPORT (written by doc_writer) must contain exactly these sections: "
        f"{headings}.\n"
        "  '## Network touchpoints' — a table of every call site: `path:line`, "
        "transport, peer/service.\n"
        "  '## External services' — one entry per service with its current "
        "official doc URL.\n"
        "  '## Sources' — the file paths inspected.\n"
        "Be exact: every cited `path:line` must match where the primitive "
        "actually appears, and every service URL must be a REAL current URL — "
        "fabricating one (e.g. any *.example.com or *.invalid domain) is a "
        "failure. Make each section at least a short paragraph."
    )


def _structure_score(out_dir: Path) -> dict[str, int]:
    """Deterministic completion gate: report exists / headings / report size."""
    report = out_dir / "report.md"
    if not report.exists():
        return {"report": 0, "sections": 0, "size": 0, "total": 0}
    text = report.read_text(encoding="utf-8", errors="replace")

    present = sum(1 for h in HEADINGS if h in text)
    size_ok = 1 if len(text) >= MIN_REPORT_CHARS else 0
    return {
        "report": 20 if report.exists() else 0,
        "sections": present,
        "size": size_ok,
        "total": 20 + 15 * present + 10 * size_ok,
    }


def _resolve_cited_file(cite: str, root: Path) -> Path | None:
    """Map a cited path fragment onto a real file under the repo."""
    cite = cite.replace("\\", "/")
    tail = cite.split("src/harness/", 1)[-1]
    candidates = [
        root / tail,
        root / "src" / tail,
        root / "src" / "harness" / tail,
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _factual_score(text: str) -> dict[str, float]:
    """Deterministic outcome checks: call coverage / URL citation / cleanliness."""
    transports = _scan_transports()
    total_files = len(transports)
    if total_files == 0:
        raise AssertionError("transport scan found no files — rubric is broken")

    # call_coverage: real touchpoint files mentioned anywhere in the report
    mentioned = 0
    for rel in transports:
        name = rel.rsplit("/", 1)[-1]
        if re.search(re.escape(rel) + "|" + re.escape(name), text):
            mentioned += 1
    coverage = mentioned / total_files

    # url_citation: distinct real-looking https URLs vs the floor
    urls = set(re.findall(r"https?://[^\s)\"'`<>]+", text))
    citation = min(len(urls) / MIN_URLS, 1.0)

    # clean: cited path:line must resolve to a real file with a real line;
    # reserved-TLD URLs are fabrication. No near-transport tolerance — a good
    # report legitimately cites helper/construction lines our coarse scan
    # doesn't flag, and penalising those rewards shallower audits.
    bad = 0
    total_cites = 0
    for m in re.finditer(r"([\w/\\:.\-]+\.py):(\d+)", text):
        total_cites += 1
        f, ln = m.group(1), m.group(2)
        resolved = _resolve_cited_file(f, REPO_ROOT)
        if resolved is None:
            bad += 1
            continue
        try:
            lineno = int(ln)
        except ValueError:
            bad += 1
            continue
        nlines = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
        if lineno < 1 or lineno > nlines:
            bad += 1

    reserved = sum(
        1
        for u in urls
        if any(_host(u).endswith(t) for t in RESERVED_SUFFIXES)
    )
    clean = max(
        0.0,
        1.0 - (bad / max(total_cites, 1)) * 0.7 - min(reserved / MIN_URLS, 1.0) * 0.3,
    )

    return {
        "coverage": coverage,
        "citation": citation,
        "clean": clean,
        "urls": len(urls),
        "files": mentioned,
        "total_files": total_files,
        "bad_cites": bad,
        "total_cites": total_cites,
        "reserved_urls": reserved,
    }


async def _run_mode(port: int, out: str, advanced: bool) -> dict[str, float]:
    """Run one mode against the real model; collect orchestration + tool metrics.

    Tool-use counts (web_search / grep / write) are gathered from both the
    parent's own tool calls and subagent event streams, so the verdict shows
    whether the optimizations actually fired inside the delegation graph.
    """
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
        await ws.send(json.dumps({"type": "message", "content": _prompt(out)}))

        started = time.monotonic()
        active = 0
        max_concurrency = 0
        depth_by_run: dict[str, int] = {}
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
                        waves += 1  # one burst of delegates per parent response
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
                }
            elif t == "run_error":
                raise AssertionError(f"run_error: {frame}")
            else:
                last_was_delegate_call = False


def _score_run(r: dict[str, object], best: dict[str, float]) -> dict[str, float]:
    """Weighted composite for one run: A (25) + B (30) + C (45)."""
    m = r["metrics"]
    assert isinstance(m, dict)
    out_dir = r["out"]
    assert isinstance(out_dir, Path)

    # A — orchestration structure (same weights as v1)
    a1 = min(float(m["max_concurrency"]), 4.0) / 4.0 * 10.0
    depth = int(m["depth"])
    a2 = 8.0 if depth >= 2 else (4.0 if depth == 1 else 0.0)
    ntypes = int(m["types"])
    a3 = 7.0 if ntypes >= 2 else (3.5 if ntypes == 1 else 0.0)
    A = a1 + a2 + a3

    # B — resource efficiency (relative to the best run across ALL runs)
    b1 = best["seconds"] / float(m["seconds"]) * 15.0
    sub = float(m["sub_turns"])
    b2 = best["sub_turns"] / sub * 8.0 if sub > 0 else 0.0
    wav = float(m["waves"])
    b3 = best["waves"] / wav * 7.0 if wav > 0 else 0.0
    B = b1 + b2 + b3

    # C — outcome quality (gate: full completion, else C = 0)
    struct = _structure_score(out_dir)
    text = ""
    report = out_dir / "report.md"
    if report.exists():
        text = report.read_text(encoding="utf-8", errors="replace")
    gate = struct["total"] >= 20 + 15 * len(HEADINGS)
    if gate:
        f = _factual_score(text)
        C = f["coverage"] * 18.0 + f["citation"] * 18.0 + f["clean"] * 9.0
    else:
        f = {
            "coverage": 0.0,
            "citation": 0.0,
            "clean": 0.0,
            "urls": 0,
            "files": 0,
            "total_files": 0,
            "bad_cites": 0,
            "total_cites": 0,
            "reserved_urls": 0,
        }
        C = 0.0

    return {
        "A": A,
        "B": B,
        "C": C,
        "total": A + B + C,
        "gate": 1.0 if gate else 0.0,
        "facts": f,  # type: ignore[dict-item]
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _wait_health(port: int, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError("web server did not become healthy in time")


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _fmt_spread(vals: list[float]) -> str:
    return f"{_median(vals):.1f} ({min(vals):.1f}–{max(vals):.1f})"


def main() -> int:
    if "--scan" in sys.argv:
        for rel, lines in _scan_transports().items():
            print(f"{rel}: {', '.join(str(n) for n in lines)}")
        return 0

    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("no DEEPSEEK_API_KEY configured; skipping compare (exit 2)", file=sys.stderr)
        return 2

    port = _free_port()
    env = {**os.environ, "HARNESS_SUBAGENTS": "1"}
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

    # Drain the server's stdout/stderr continuously — uvicorn blocks forever if
    # its pipe fills, and without this a log-heavy run freezes the whole bench.
    server_log: deque[str] = deque(maxlen=60)

    def _drain_server() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            server_log.append(raw.decode("utf-8", errors="replace").rstrip())

    threading.Thread(target=_drain_server, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="harness-compare-v2-"))
    runs: list[dict[str, Any]] = []
    try:
        _wait_health(port)
        for mode, advanced in (("normal", False), ("advanced", True)):
            for i in range(1, RUNS + 1):
                out_dir = tmp / f"{mode}-{i}"
                try:
                    metrics = asyncio.run(
                        asyncio.wait_for(_run_mode(port, str(out_dir), advanced), timeout=600.0)
                    )
                except TimeoutError:
                    print(f"  {mode}-{i}: TIMEOUT after 600s — skipped", flush=True)
                    continue
                runs.append(
                    {"mode": mode, "out": out_dir, "metrics": metrics, "run": i}
                )
                struct = _structure_score(out_dir)
                print(
                    f"  ran {mode}-{i}: struct={struct['total']}/"
                    f"{20 + 15 * len(HEADINGS)}  "
                    f"deleg={metrics['delegations']} waves={metrics['waves']} "
                    f"conc={metrics['max_concurrency']} depth={metrics['depth']} "
                    f"types={metrics['types']} web={metrics['web_searches']} "
                    f"grep={metrics['greps']} wr={metrics['writes']} "
                    f"bash={metrics['bash']} sub_turns={metrics['sub_turns']} "
                    f"wall={metrics['seconds']:.1f}s",
                    flush=True,
                )

        if not runs:
            print("  no runs completed in either mode — aborting", file=sys.stderr)
            return 1

        best = {
            "seconds": min(float(r["metrics"]["seconds"]) for r in runs),
            "sub_turns": min(float(r["metrics"]["sub_turns"]) for r in runs),
            "waves": min(float(r["metrics"]["waves"]) for r in runs),
        }
        for r in runs:
            r.update(_score_run(r, best))

        for mode in ("normal", "advanced"):
            rs = [r for r in runs if r["mode"] == mode]
            if not rs:
                print(f"\n== {mode} (n=0 — all runs timed out; skipped) ==")
                continue
            print(f"\n== {mode} (n={len(rs)}) ==")
            print(f"  A structure  {_fmt_spread([r['A'] for r in rs])} /25")
            print(f"  B resource   {_fmt_spread([r['B'] for r in rs])} /30")
            print(f"  C outcome    {_fmt_spread([r['C'] for r in rs])} /45")
            print(f"  total        {_fmt_spread([r['total'] for r in rs])} /100")
            facts = [r["facts"] for r in rs]
            if facts:
                cov = [f["coverage"] * 100 for f in facts]
                cit = [f["citation"] * 100 for f in facts]
                clean = [f["clean"] * 100 for f in facts]
                print(
                    f"    coverage {_fmt_spread(cov)}%  citation {_fmt_spread(cit)}%  "
                    f"clean {_fmt_spread(clean)}%"
                )
                ws = [float(r["metrics"]["web_searches"]) for r in rs]
                gp = [float(r["metrics"]["greps"]) for r in rs]
                wr = [float(r["metrics"]["writes"]) for r in rs]
                bs = [float(r["metrics"]["bash"]) for r in rs]
                tp = [float(r["metrics"]["types"]) for r in rs]
                dp = [float(r["metrics"]["depth"]) for r in rs]
                print(
                    f"    tools: web_search {_fmt_spread(ws)}  grep/glob {_fmt_spread(gp)}  "
                    f"write_file {_fmt_spread(wr)}  bash {_fmt_spread(bs)}  "
                    f"types {_fmt_spread(tp)}  depth {_fmt_spread(dp)}"
                )

        def _med(key: str, mode: str) -> float | None:
            rs = [r[key] for r in runs if r["mode"] == mode]
            return _median(rs) if rs else None

        nt, at = _med("total", "normal"), _med("total", "advanced")
        if nt is None or at is None:
            print(f"\nverdict: incomplete — normal={nt}  advanced={at}")
            return 0
        print(f"\nverdict: total median  normal={nt:.1f}  advanced={at:.1f}  delta={at - nt:+.1f}")
        for dim, key in (("A structure", "A"), ("B resource", "B"), ("C outcome", "C")):
            nv, av = _med(key, "normal"), _med(key, "advanced")
            nstr = f"{nv:.1f}" if nv is not None else "n/a"
            astr = f"{av:.1f}" if av is not None else "n/a"
            print(f"          {dim:11s} normal={nstr:>5s} advanced={astr:>5s}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"E2E SUBAGENTS COMPARE V2 FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
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


if __name__ == "__main__":
    sys.exit(main())
