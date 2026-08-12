"""Per-subagent tool allowlists (SubagentSpec.tools + Subagent.mcp_allowlist)."""

from __future__ import annotations

from harness.agents.registry import BUNDLED_SUBAGENTS_DIR, SubagentRegistry
from harness.tools.builtin import builtin_registry


def test_bundled_subagents_resolve_exact_allowlists(tmp_path) -> None:
    """Each bundled subagent's tools == its allowlist ∩ currently-registered
    builtins. `web_search` lands in Task 3; until then it is simply skipped,
    so this assertion stays correct across both commits."""
    reg = SubagentRegistry(tmp_path / "empty", bundled_dir=BUNDLED_SUBAGENTS_DIR)
    specs = {s.name: s for s in reg.discover()}
    expect = {
        "search": ["glob_files", "grep_files", "bash"],
        "researcher": ["read_file", "glob_files", "grep_files", "bash", "web_search"],
        "coder": ["read_file", "write_file", "glob_files", "grep_files", "bash", "web_search"],
        "doc_writer": ["read_file", "write_file", "bash"],
        "file_handler": ["read_file", "write_file", "bash"],
        "frontend_design": ["read_file", "write_file", "bash"],
    }
    registered = set(builtin_registry().names())
    for name, want in expect.items():
        sa = reg.to_subagent(specs[name])
        assert sa.tools.names() == sorted(set(want) & registered), name
        # mcp_* patterns are carried separately, not registered as builtins
        assert sa.mcp_allowlist == (), name


def test_subagent_without_tools_keeps_full_builtin_set(tmp_path) -> None:
    """Absent `tools:` field (backwards compatible) => every builtin."""
    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "plain.yaml").write_text(
        "name: plain\n"
        "description: Use when delegating; delegate by default.\n"
        "instructions: Do things.\n",
        encoding="utf-8",
    )
    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("plain")
    assert spec is not None
    assert spec.tools == ()
    sa = reg.to_subagent(spec)
    assert sa.tools.names() == sorted(builtin_registry().names())


def test_mcp_patterns_carried_not_registered(tmp_path) -> None:
    """`mcp_*` entries are carried on the Subagent for Task-3 propagation and
    never resolve into builtin tool registrations."""
    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "mcpuser.yaml").write_text(
        "name: mcpuser\n"
        "description: Use when delegating; delegate by default.\n"
        "instructions: Use mcp.\n"
        "tools:\n"
        "  - read_file\n"
        "  - mcp_*\n",
        encoding="utf-8",
    )
    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("mcpuser")
    assert spec is not None
    sa = reg.to_subagent(spec)
    assert sa.tools.names() == ["read_file"]
    assert sa.mcp_allowlist == ("mcp_*",)


def test_unknown_tool_names_are_skipped(tmp_path) -> None:
    """A typo in `tools:` resolves to nothing and is skipped, not fatal."""
    runtime = tmp_path / "skills" / "subagents"
    runtime.mkdir(parents=True)
    (runtime / "sloppy.yaml").write_text(
        "name: sloppy\n"
        "description: Use when delegating; delegate by default.\n"
        "instructions: x.\n"
        "tools:\n"
        "  - read_file\n"
        "  - write_fil\n",
        encoding="utf-8",
    )
    reg = SubagentRegistry(runtime, bundled_dir=BUNDLED_SUBAGENTS_DIR)
    spec = reg.get("sloppy")
    assert spec is not None
    sa = reg.to_subagent(spec)
    assert sa.tools.names() == ["read_file"]
