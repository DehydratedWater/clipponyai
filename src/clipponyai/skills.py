"""Agent Skills discovery and progressive-disclosure file loading."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import SkillsConfig, data_dir

logger = logging.getLogger(__name__)

_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SKILL_BODY_LIMIT = 16_000
_SKILL_FILE_LIMIT = 32_000


@dataclass(frozen=True)
class Skill:
    """Catalog metadata for one valid skill directory."""

    name: str
    description: str
    path: Path
    metadata: dict[str, Any]


class SkillsError(ValueError):
    """A user-visible failure while resolving or reading a skill."""


class SkillsLibrary:
    """Discover Agent Skills and load their content on demand."""

    def __init__(
        self,
        config: SkillsConfig,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self._log_fn = log_fn or logger.warning

    def scan(self) -> list[Skill]:
        """Return valid, enabled skills in deterministic first-directory-wins order."""

        if not self.config.enabled:
            return []

        skills: dict[str, Skill] = {}
        disabled = set(self.config.disabled)
        for root in self._scan_dirs():
            if not root.is_dir():
                continue
            try:
                candidates = sorted(
                    (path for path in root.iterdir() if path.is_dir()),
                    key=lambda path: path.name,
                )
            except OSError as exc:
                self._warn(f"Could not scan skills directory {root}: {exc}")
                continue

            for skill_dir in candidates:
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.is_file():
                    continue
                try:
                    skill = self._parse_skill(skill_dir, skill_file)
                except (OSError, UnicodeError, SkillsError, yaml.YAMLError) as exc:
                    self._warn(f"Skipping skill at {skill_dir}: {exc}")
                    continue
                if skill.name in disabled:
                    continue
                if skill.name in skills:
                    self._warn(
                        f"Skipping duplicate skill {skill.name!r} at {skill_dir}; "
                        f"first found at {skills[skill.name].path}"
                    )
                    continue
                skills[skill.name] = skill
        return list(skills.values())

    def catalog(self) -> str | None:
        """Render compact model-facing metadata for all available skills."""

        skills = self.scan()
        if not skills:
            return None
        lines = [
            "## Available skills",
            "Use the activate_skill tool to load a skill's full instructions when relevant.",
        ]
        lines.extend(f"- {skill.name}: {skill.description}" for skill in skills)
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """Load a skill's instruction body, excluding YAML frontmatter."""

        skill = self._find(name)
        try:
            text = (skill.path / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillsError(f"could not read skill {name!r}: {exc}") from exc
        _, body = self._split_frontmatter(text)
        body = self._truncate(body.strip(), _SKILL_BODY_LIMIT)
        return f'<skill name="{skill.name}">\n{body}\n</skill>'

    def read_file(self, name: str, relative_path: str) -> str:
        """Read a bounded text file beneath a skill directory."""

        skill = self._find(name)
        requested = Path(relative_path)
        if requested.is_absolute() or ".." in requested.parts:
            raise SkillsError("skill file path must be relative and may not contain '..'")
        if not relative_path or requested == Path("."):
            raise SkillsError("skill file path is required")

        skill_root = skill.path.resolve()
        target = (skill_root / requested).resolve()
        if not target.is_relative_to(skill_root):
            raise SkillsError("skill file path escapes the skill directory")
        if not target.is_file():
            raise SkillsError(f"skill file not found: {relative_path}")
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillsError(f"could not read skill file {relative_path!r}: {exc}") from exc
        return self._truncate(text, _SKILL_FILE_LIMIT)

    def names(self) -> list[str]:
        """Return the names currently visible in the catalog."""

        return [skill.name for skill in self.scan()]

    def _find(self, name: str) -> Skill:
        if not self.config.enabled:
            raise SkillsError("skills are disabled")
        skills = self.scan()
        for skill in skills:
            if skill.name == name:
                return skill
        available = ", ".join(skill.name for skill in skills) or "(none)"
        raise SkillsError(f"unknown skill {name}. Available: {available}")

    def _scan_dirs(self) -> list[Path]:
        roots = [
            data_dir() / "skills",
            Path("~/.agents/skills").expanduser(),
            *(Path(entry).expanduser() for entry in self.config.dirs),
        ]
        unique: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            normalized = root.resolve()
            if normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique

    @staticmethod
    def _parse_skill(skill_dir: Path, skill_file: Path) -> Skill:
        text = skill_file.read_text(encoding="utf-8")
        frontmatter, _ = SkillsLibrary._split_frontmatter(text)
        try:
            parsed = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            raise
        if not isinstance(parsed, dict):
            raise SkillsError("frontmatter must be a YAML mapping")

        name = parsed.get("name")
        description = parsed.get("description")
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise SkillsError("name must be a string between 1 and 64 characters")
        if _SKILL_NAME.fullmatch(name) is None:
            raise SkillsError(
                "name must contain lowercase letters/numbers separated by single hyphens"
            )
        if name != skill_dir.name:
            raise SkillsError(
                f"frontmatter name {name!r} must match directory {skill_dir.name!r}"
            )
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 1024
        ):
            raise SkillsError(
                "description must be a string between 1 and 1024 characters"
            )

        metadata = parsed.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SkillsError("metadata must be a YAML mapping")
        return Skill(
            name=name,
            description=description,
            path=skill_dir.resolve(),
            metadata=dict(metadata),
        )

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[str, str]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise SkillsError("SKILL.md must start with YAML frontmatter")
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "".join(lines[1:index]), "".join(lines[index + 1 :])
        raise SkillsError("SKILL.md frontmatter is missing its closing '---'")

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        notice = "\n\n[truncated: content exceeded the size limit]"
        return text[: limit - len(notice)] + notice

    def _warn(self, message: str) -> None:
        self._log_fn(message)
