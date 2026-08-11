"""Skill discovery and indexing.

A *skill* is a markdown file with a YAML-ish frontmatter block::

    ---
    name: my-skill
    description: How to do X efficiently
    ---
    <body: markdown instructions for the model>

:class:`SkillRegistry` scans a directory for these files, parses the
frontmatter (dependency-light: handles flat ``key: value`` lines), and can
render the skills back into a prompt block that gets injected into an agent's
system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Skills that ship with the package (tracked in the repo) live here. The
# runtime skills directory is the *override* layer for user-authored skills.
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "bundled"


def _parse_frontmatter(raw: str) -> dict[str, str]:
    """Parse a minimal ``key: value`` frontmatter block (quotes stripped)."""
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() not in {"name", "description"}:
            continue
        out[key.strip().lower()] = value.strip().strip("'\"")
    return out


@dataclass(frozen=True)
class Skill:
    """A discovered skill: metadata plus the markdown body."""

    name: str
    description: str
    content: str
    path: Path


def parse_skill_file(path: Path) -> Skill | None:
    """Parse a markdown file into a Skill, or None if it has no frontmatter."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    fm = _parse_frontmatter(match.group(1))
    name = fm.get("name") or path.stem
    description = fm.get("description") or name
    content = text[match.end() :].strip()
    return Skill(name=name, description=description, content=content, path=path)


class SkillRegistry:
    """Scan a directory and index the skills found in it."""

    def __init__(
        self, skills_dir: str | Path, bundled_dir: str | Path | None = None
    ) -> None:
        self._dir = Path(skills_dir)
        self._bundled_dir = Path(bundled_dir) if bundled_dir is not None else None
        self._skills: dict[str, Skill] = {}

    @property
    def directory(self) -> Path:
        """The runtime directory — where ``create_skill`` writes new files."""
        return self._dir

    def discover(self) -> list[Skill]:
        """(Re)scan and rebuild the index; returns the skills.

        Bundled skills (shipped with the package) are scanned first, then the
        runtime directory. A same-named skill in the runtime directory wins, so
        a user's edited copy overrides the shipped default.
        """
        self._skills = {}
        sources: list[Path] = []
        if self._bundled_dir is not None:
            sources.append(self._bundled_dir)
        sources.append(self._dir)
        for source in sources:
            if not source.is_dir():
                continue
            for path in sorted(source.glob("*.md")):
                skill = parse_skill_file(path)
                if skill is not None:
                    self._skills[skill.name] = skill
        return self.all()

    def refresh(self) -> list[Skill]:
        """Alias for discover(); re-scans so newly created skills take effect."""
        return self.discover()

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def to_prompt_block(self, names: list[str] | None = None) -> str:
        """Render selected (or all) skills as a prompt block for injection."""
        skills = self.all() if names is None else [s for n in names if (s := self.get(n))]
        if not skills:
            return ""
        parts: list[str] = ["## Skills"]
        for skill in skills:
            parts.append(f"### {skill.name}\n{skill.description}\n\n{skill.content}")
        return "\n\n".join(parts)

    def inject(self, instructions: str, names: list[str] | None = None) -> str:
        """Return instructions with the skill block appended (if any skills)."""
        block = self.to_prompt_block(names)
        if not block:
            return instructions
        return f"{instructions}\n\n{block}"
