"""Runtime skill creation and loading.

The agent can create its own skills through the :func:`create_skill` tool,
which writes a markdown file (frontmatter + body) into the skills directory and
refreshes the registry so the skill is picked up immediately. Loading a skill
means injecting it into the agent's instructions.
"""

from __future__ import annotations

from pathlib import Path

from harness.observability.logging import get_logger
from harness.skills.registry import SkillRegistry
from harness.tools.base import Tool, tool

logger = get_logger("skills")


def create_skill_file(
    skills_dir: str | Path,
    name: str,
    description: str,
    content: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Write a skill markdown file with frontmatter; returns its path."""
    directory = Path(skills_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    if path.exists() and not overwrite:
        raise FileExistsError(f"skill {name!r} already exists at {path}")
    body = (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"{content.strip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    logger.info("created skill %r at %s", name, path)
    return path


def make_create_skill_tool(registry: SkillRegistry) -> Tool:
    """A tool the agent can call to author a new skill at runtime."""

    @tool(
        name="create_skill",
        description=(
            "Create a reusable skill: write a markdown file with frontmatter "
            "(name/description) plus instructional content into the skills "
            "directory. The skill becomes available immediately. Use for "
            "repeatable procedures, code patterns, or domain knowledge."
        ),
    )
    def create_skill(name: str, description: str, content: str) -> str:
        try:
            path = create_skill_file(
                registry.directory, name, description, content, overwrite=False
            )
        except FileExistsError as exc:
            return f"Error: {exc}"
        registry.refresh()
        return f"Created skill '{name}' at {path} ({len(content)} chars). Now loaded."

    return create_skill
