"""Tests for skills (discovery, runtime creation, injection) and preferences."""

from __future__ import annotations

from pathlib import Path

from harness.memory.preferences import PreferenceStore, make_remember_preference_tool
from harness.skills.loader import create_skill_file, make_create_skill_tool
from harness.skills.registry import (
    BUNDLED_SKILLS_DIR,
    SkillRegistry,
    parse_skill_file,
)

SKILL_MD = """---
name: code-review
description: Review Python code for correctness and style
---
1. Read the file.
2. Check typing, edge cases, and side effects.
3. Suggest fixes.
"""


def _write_skill(root: Path, filename: str, text: str) -> Path:
    path = root / filename
    path.write_text(text, encoding="utf-8")
    return path


# -- parsing --

def test_parse_skill_file_frontmatter(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "code-review.md", SKILL_MD)
    skill = parse_skill_file(path)
    assert skill is not None
    assert skill.name == "code-review"
    assert skill.description == "Review Python code for correctness and style"
    assert "1. Read the file." in skill.content
    assert skill.name not in skill.content  # frontmatter stripped


def test_parse_skill_file_no_frontmatter(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "plain.md", "# Just a heading\nno frontmatter\n")
    assert parse_skill_file(path) is None


def test_parse_skill_file_name_falls_back_to_stem(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "unnamed.md", "---\ndescription: x\n---\nbody\n")
    skill = parse_skill_file(path)
    assert skill is not None
    assert skill.name == "unnamed"


# -- registry --

def test_registry_discover_and_index(tmp_path: Path) -> None:
    _write_skill(tmp_path, "a.md", SKILL_MD)
    _write_skill(tmp_path, "b.md", "---\nname: b\n---\nbody\n")
    registry = SkillRegistry(tmp_path)
    skills = registry.discover()
    assert sorted(s.name for s in skills) == ["b", "code-review"]
    assert registry.get("code-review") is not None
    assert registry.names() == ["code-review", "b"]


def test_registry_ignores_non_md(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text(SKILL_MD, encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    assert registry.discover() == []


def test_registry_merges_bundled_with_runtime(tmp_path: Path) -> None:
    """Bundled skills (shipped) merge with the runtime dir; a same-named skill
    in the runtime dir wins, so a user's edited copy overrides the shipped
    default."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    _write_skill(bundled, "alpha.md", "---\nname: alpha\n---\nBUNDLED alpha\n")
    _write_skill(bundled, "shared.md", "---\nname: shared\n---\nBUNDLED shared\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _write_skill(runtime, "shared.md", "---\nname: shared\n---\nRUNTIME shared\n")

    registry = SkillRegistry(runtime, bundled_dir=bundled)
    assert sorted(s.name for s in registry.discover()) == ["alpha", "shared"]
    shared = registry.get("shared")
    assert shared is not None and "RUNTIME shared" in shared.content  # runtime wins


def test_shipped_bundled_skills_found_without_runtime(tmp_path: Path) -> None:
    """A fresh clone (no runtime skills dir yet) still gets the shipped skills;
    subagent-only skills must not leak into the main registry."""
    registry = SkillRegistry(tmp_path / "no-skills", bundled_dir=BUNDLED_SKILLS_DIR)
    names = {s.name for s in registry.discover()}
    assert "skill-creator" in names
    assert "frontend-design" not in names
    assert "doc-coauthoring" not in names


def test_inject_appends_skill_block(tmp_path: Path) -> None:
    _write_skill(tmp_path, "code-review.md", SKILL_MD)
    registry = SkillRegistry(tmp_path)
    registry.discover()
    instructions = "You are an assistant."
    out = registry.inject(instructions)
    assert out.startswith(instructions)
    assert "code-review" in out
    assert "Review Python code" in out


def test_to_prompt_block_filtered_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, "a.md", "---\nname: a\n---\nA body\n")
    _write_skill(tmp_path, "b.md", "---\nname: b\n---\nB body\n")
    registry = SkillRegistry(tmp_path)
    registry.discover()
    block = registry.to_prompt_block(names=["b"])
    assert "B body" in block
    assert "A body" not in block


# -- runtime creation --

def test_create_skill_file_and_refresh(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)
    registry.discover()
    assert registry.get("new-skill") is None

    create_skill_file(tmp_path, "new-skill", "Does X", "Step 1\nStep 2")
    registry.refresh()
    skill = registry.get("new-skill")
    assert skill is not None
    assert skill.description == "Does X"
    assert "Step 2" in skill.content


async def test_create_skill_tool_invokes(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)
    registry.discover()
    tool = make_create_skill_tool(registry)  # type: ignore[assignment]
    result = await tool.invoke(
        name="new-skill", description="Does X", content="Step 1\nStep 2"  # type: ignore[arg-type]
    )
    assert "Created skill" in result.content
    assert registry.get("new-skill") is not None  # refreshed immediately
    assert (tmp_path / "new-skill.md").exists()


async def test_create_skill_tool_duplicate_errors(tmp_path: Path) -> None:
    create_skill_file(tmp_path, "dup", "desc", "body")
    registry = SkillRegistry(tmp_path)
    registry.discover()
    tool = make_create_skill_tool(registry)  # type: ignore[assignment]
    result = await tool.invoke(name="dup", description="desc", content="body")  # type: ignore[arg-type]
    assert "already exists" in result.content


# -- runtime creation -> next-turn loading --

def test_runtime_skill_loads_next_turn(tmp_path: Path) -> None:
    """A skill created at runtime is discovered and injected into instructions."""
    registry = SkillRegistry(tmp_path)
    registry.discover()
    base = "You are an assistant."

    # agent creates a skill at runtime
    create_skill_file(tmp_path, "summarize", "Summarize long docs", "Return 3 bullets.")
    registry.refresh()

    # next turn: instructions rebuilt with skills now include it
    instructions = registry.inject(base)
    assert "summarize" in instructions
    assert "Return 3 bullets." in instructions


# -- preferences --

async def test_preference_store_roundtrip(tmp_path: Path) -> None:
    store = PreferenceStore(str(tmp_path / "prefs.db"))
    await store.initialize()
    try:
        assert await store.get("language") is None
        await store.set("language", "zh")
        await store.set("verbosity", "concise")
        assert await store.get("language") == "zh"
        assert await store.get_all() == {"language": "zh", "verbosity": "concise"}
        await store.set("language", "en")  # upsert
        assert await store.get("language") == "en"
        await store.delete("verbosity")
        assert await store.get("verbosity") is None
    finally:
        await store.close()


async def test_remember_preference_tool(tmp_path: Path) -> None:
    store = PreferenceStore(str(tmp_path / "prefs.db"))
    await store.initialize()
    try:
        tool = make_remember_preference_tool(store)  # type: ignore[assignment]
        result = await tool.invoke(key="language", value="zh")  # type: ignore[arg-type]
        assert "Saved preference" in result.content
        assert await store.get("language") == "zh"
    finally:
        await store.close()


async def test_preferences_survive_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "prefs.db")
    store = PreferenceStore(db)
    await store.initialize()
    await store.set("theme", "dark")
    await store.close()

    reopened = PreferenceStore(db)
    await reopened.initialize()
    try:
        assert await reopened.get("theme") == "dark"
    finally:
        await reopened.close()
