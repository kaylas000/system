"""
Skill registry (port of ``specs/03_skills/registry/SKILL_REGISTRY.py``).

Fixes vs. the spec (``specs/ISSUES.md`` SK-07):
* ``resolve_dependencies`` counted in-degree on the dependency instead of the
  dependent, so Kahn's algorithm returned dependents *before* their dependencies;
* unknown dependencies raised ``KeyError``; now they are reported explicitly;
* load errors were ``print``-ed and lost; now logged and kept in ``load_errors``;
* the registry takes a directory (not a whole vertical) so it is usable standalone;
* duplicate skill ids are an error instead of silently overwriting.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .definition import LoadedSkill, SkillLoadError
from .executor import SkillExecutor

logger = logging.getLogger("autogen.skills")


class SkillRegistry:
    def __init__(
        self,
        skills_dir: Path | str | None,
        *,
        vertical_id: str | None = None,
        tool_registry: Any = None,
        shared_template_dirs: list[Path] | None = None,
        strict: bool = False,
    ) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else None
        self.vertical_id = vertical_id
        self.tool_registry = tool_registry
        self.shared_template_dirs = shared_template_dirs or []
        self._skills: dict[str, LoadedSkill] = {}
        self._executors: dict[str, SkillExecutor] = {}
        self.load_errors: dict[str, str] = {}
        self._load_all()
        if strict and self.load_errors:
            raise SkillLoadError("; ".join(f"{k}: {v}" for k, v in self.load_errors.items()))

    def _load_all(self) -> None:
        if self.skills_dir is None or not self.skills_dir.is_dir():
            return
        for skill_dir in sorted(p for p in self.skills_dir.iterdir() if p.is_dir()):
            if not (skill_dir / "skill.yaml").exists():
                continue
            try:
                skill = LoadedSkill.load_from_dir(skill_dir)
            except SkillLoadError as exc:
                self.load_errors[skill_dir.name] = str(exc)
                logger.error("Failed to load skill %s: %s", skill_dir.name, exc)
                continue
            if skill.compatible_verticals and self.vertical_id and self.vertical_id not in skill.compatible_verticals:
                logger.info("Skill %s not compatible with vertical %s", skill.id, self.vertical_id)
                continue
            self.add(skill)

    def add(self, skill: LoadedSkill) -> None:
        if skill.id in self._skills:
            raise SkillLoadError(f"duplicate skill id {skill.id!r}")
        self._skills[skill.id] = skill
        self._executors[skill.id] = SkillExecutor(skill, self.tool_registry, self.shared_template_dirs)

    # -- queries -------------------------------------------------------------------
    @property
    def skills(self) -> dict[str, LoadedSkill]:
        return dict(self._skills)

    def get_skill(self, skill_id: str) -> LoadedSkill | None:
        return self._skills.get(skill_id)

    def get_executor(self, skill_id: str) -> SkillExecutor:
        try:
            return self._executors[skill_id]
        except KeyError:
            raise KeyError(f"unknown skill {skill_id!r}") from None

    def list_skills(self, category: str | None = None, tags: list[str] | None = None) -> list[LoadedSkill]:
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.category == category]
        if tags:
            skills = [s for s in skills if any(t in s.tags for t in tags)]
        return skills

    def providers_of(self, capability: str) -> list[str]:
        return [s.id for s in self._skills.values() if capability in s.provides]

    def resolve_dependencies(self, skill_ids: list[str]) -> list[str]:
        """Skill ids plus their transitive ``depends_on``, dependencies first (stable order)."""
        needed: list[str] = []
        seen: set[str] = set()
        stack = list(skill_ids)
        while stack:
            sid = stack.pop(0)
            if sid in seen:
                continue
            if sid not in self._skills:
                raise KeyError(f"unknown skill {sid!r}")
            seen.add(sid)
            needed.append(sid)
            stack.extend(self._skills[sid].depends_on)

        deps = {sid: [d for d in self._skills[sid].depends_on if d in seen] for sid in needed}
        indegree = {sid: len(d) for sid, d in deps.items()}
        dependents: dict[str, list[str]] = {sid: [] for sid in needed}
        for sid, ds in deps.items():
            for d in ds:
                dependents[d].append(sid)
        queue = [sid for sid in needed if indegree[sid] == 0]
        order: list[str] = []
        while queue:
            u = queue.pop(0)
            order.append(u)
            for v in dependents[u]:
                indegree[v] -= 1
                if indegree[v] == 0:
                    queue.append(v)
        if len(order) != len(needed):
            cycle = sorted(sid for sid, n in indegree.items() if n > 0)
            raise ValueError(f"Circular dependency among skills: {cycle}")
        return order
