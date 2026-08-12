"""Prompt/description sharpening for the bundled subagents and the advanced protocol."""

from __future__ import annotations

import yaml

from harness.agents.registry import BUNDLED_SUBAGENTS_DIR


def _bundled(name: str) -> dict:
    text = (BUNDLED_SUBAGENTS_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_researcher_points_at_web_search_and_doc_handoff() -> None:
    cfg = _bundled("researcher")
    assert "web_search" in cfg["description"]
    assert "web_search" in cfg["instructions"]
    assert "file:line" in cfg["instructions"]  # source attribution
    assert "doc_writer" in cfg["instructions"]  # handoff seed
    assert "under 200" in cfg["instructions"]  # length cap kept


def test_search_distinguishes_glob_vs_grep() -> None:
    cfg = _bundled("search")
    assert "glob_files" in cfg["instructions"]
    assert "grep_files" in cfg["instructions"]
    assert "path pattern" in cfg["instructions"]
    assert "content" in cfg["instructions"]
    # still workspace-only: search must not advertise web search
    assert "web_search" not in cfg["instructions"]
    assert "web_search" not in cfg["description"]


def test_coder_can_consult_web_search() -> None:
    cfg = _bundled("coder")
    assert "web_search" in cfg["instructions"]
    assert "cite the URL" in cfg["instructions"]


def test_writer_descriptions_sharpen_ownership() -> None:
    for name, marker in {
        "doc_writer": "not the parent's",
        "frontend_design": "not the parent's",
    }.items():
        assert marker in _bundled(name)["description"]


def test_file_handler_boundary_keeps_code_out() -> None:
    cfg = _bundled("file_handler")
    assert "NOT code" in cfg["description"]
    assert "coder" in cfg["description"]


def test_advanced_protocol_nudges_chaining() -> None:
    from harness.agents.orchestrator import DELEGATION_PROTOCOL_ADVANCED

    assert "RECOMMENDED NEXT STEP names a subagent" in DELEGATION_PROTOCOL_ADVANCED
    assert "router" in DELEGATION_PROTOCOL_ADVANCED
    assert "chaining best-fit subagents" in DELEGATION_PROTOCOL_ADVANCED


def test_advanced_hint_allows_nested_handoff() -> None:
    from harness.agents.orchestrator import DELEGATION_HINT

    assert "better-fit subagent" in DELEGATION_HINT
