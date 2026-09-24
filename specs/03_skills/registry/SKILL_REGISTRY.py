# specs/03_skills/registry/SKILL_REGISTRY.py
"""
Skill Registry: Loads, Caches, Resolves Dependencies, Executes Skills.
"""

from __future__ import annotations
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Set
from collections import defaultdict

from kernel.protocols import IVertical, SkillDef, ISkillExecutor
from kernel.state import AgentState
from ..skill.SKILL_DEFINITION import SkillDef as SkillDefModel
from ..skill.SKILL_EXECUTOR import SkillExecutor


class SkillRegistry:
    def __init__(self, vertical: IVertical, tool_registry: "ToolRegistry"):
        self.vertical = vertical
        self.tool_registry = tool_registry
        self._skills: Dict[str, SkillDefModel] = {}
        self._executors: Dict[str, SkillExecutor] = {}
        self._load_all()

    def _load_all(self):
        """Scan vertical skills directory."""
        skills_dir = Path(self.vertical.manifest.skills_dir)  # e.g. verticals/saas_web/skills
        if not skills_dir.exists():
            return

        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "skill.yaml").exists():
                try:
                    skill_def = SkillDefModel.load_from_dir(skill_dir)
                    # Check vertical compatibility
                    if (
                        skill_def.compatible_verticals
                        and self.vertical.manifest.id not in skill_def.compatible_verticals
                    ):
                        continue
                    self._skills[skill_def.id] = skill_def
                    self._executors[skill_def.id] = SkillExecutor(skill_def, self.tool_registry)
                except Exception as e:
                    print(f"[SkillRegistry] Failed to load skill {skill_dir.name}: {e}")

    def get_skill(self, skill_id: str) -> Optional[SkillDefModel]:
        return self._skills.get(skill_id)

    def get_executor(self, skill_id: str) -> Optional[ISkillExecutor]:
        return self._executors.get(skill_id)

    def list_skills(self, category: str = None, tags: List[str] = None) -> List[SkillDefModel]:
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.category == category]
        if tags:
            skills = [s for s in skills if any(t in s.tags for t in tags)]
        return skills

    def resolve_dependencies(self, skill_ids: List[str]) -> List[str]:
        """Topological sort of skill IDs based on depends_on."""
        # Kahn's algorithm
        graph = {sid: set(self._skills[sid].depends_on) for sid in skill_ids if sid in self._skills}
        # Add transitive deps
        all_nodes = set(skill_ids)
        for sid in skill_ids:
            self._collect_deps(sid, all_nodes, graph)

        # Rebuild graph for all_nodes
        full_graph = {sid: set(self._skills[sid].depends_on) & all_nodes for sid in all_nodes}

        # Topo sort
        indegree = defaultdict(int)
        for u in full_graph:
            for v in full_graph[u]:
                indegree[v] += 1

        queue = [u for u in full_graph if indegree[u] == 0]
        result = []
        while queue:
            u = queue.pop(0)
            result.append(u)
            for v in full_graph[u]:
                indegree[v] -= 1
                if indegree[v] == 0:
                    queue.append(v)

        if len(result) != len(full_graph):
            raise ValueError("Circular dependency detected in skills")
        return result

    def _collect_deps(self, skill_id: str, all_nodes: Set, graph: Dict):
        if skill_id not in self._skills:
            return
        for dep in self._skills[skill_id].depends_on:
            if dep not in all_nodes:
                all_nodes.add(dep)
                self._collect_deps(dep, all_nodes, graph)
