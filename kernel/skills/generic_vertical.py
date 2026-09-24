"""
``GenericVertical`` - default ``IVertical`` for a vertical directory without custom code.

NOTE: ``specs/03_skills/registry/VERTICAL_LOADER.py`` imports ``DEFAULT_VERTICAL.GenericVertical``,
but the spec has no such file (``specs/MISSING_FILES.md``). This module is AUTHORED BY THE AGENT.

A vertical directory::

    <vertical>/
      manifest.yaml            VerticalManifest (MANIFEST.yaml also accepted)
      skills/<id>/skill.yaml   skills (+ templates/, hooks.py, assets/)
      prompts/*.j2             PLANNER / CODER / FIXER / DOCUMENTER (kernel defaults otherwise)
      verification/GATES.yaml  gates: [{id, name, command, runs_on_every_task, tags, parser, ...}]
      verification/parsers.py  optional: PARSERS = {"parse_tsc": fn, ...}
      vertical_impl.py         optional: class VerticalImpl(GenericVertical)

Gate selection for a task: gates with ``runs_on_every_task`` + gates whose ``tags``
intersect the task skill's tags + the skill's own validation gate. Project level
(``task=None``): all blocking gates.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from ..prompts.compiler import PromptCompiler
from ..protocols import GenerateRequest, IVerificationGate, VerticalManifest
from ..state import AgentState, Task
from .definition import LoadedSkill
from .executor import SkillExecutor
from .gates import CommandGate, Parser, SkillValidationGate, gates_from_config
from .registry import SkillRegistry

logger = logging.getLogger("autogen.vertical")
MANIFEST_NAMES = ("manifest.yaml", "MANIFEST.yaml")


class VerticalLoadError(RuntimeError):
    pass


def find_manifest(vertical_dir: Path) -> Path | None:
    for name in MANIFEST_NAMES:
        if (vertical_dir / name).is_file():
            return vertical_dir / name
    return None


def load_manifest(vertical_dir: Path) -> VerticalManifest:
    path = find_manifest(vertical_dir)
    if path is None:
        raise VerticalLoadError(f"no manifest.yaml in {vertical_dir}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return VerticalManifest.model_validate(data)
    except ValueError as exc:
        raise VerticalLoadError(f"{path}: {exc}") from exc


def import_module_from(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise VerticalLoadError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GenericVertical:
    def __init__(
        self,
        manifest: VerticalManifest,
        vertical_dir: Path | str,
        *,
        tool_registry: Any = None,
        skill_registry: SkillRegistry | None = None,
        parsers: dict[str, Parser] | None = None,
    ) -> None:
        self._manifest = manifest
        self.vertical_dir = Path(vertical_dir).resolve()
        self.tool_registry = tool_registry
        shared = [self.vertical_dir / "templates"]
        self.skill_registry = skill_registry or SkillRegistry(
            self.vertical_dir / manifest.skills_dir,
            vertical_id=manifest.id,
            tool_registry=tool_registry,
            shared_template_dirs=[d for d in shared if d.is_dir()],
        )
        self.prompts = PromptCompiler(self.vertical_dir / "prompts")
        self.gates: dict[str, CommandGate] = self._load_gates(parsers)

    # -- loading -------------------------------------------------------------------
    def _gates_file(self) -> Path:
        rel = (self._manifest.verification or {}).get("gates_config", "verification/GATES.yaml")
        return self.vertical_dir / str(rel)

    def _load_parsers(self) -> dict[str, Parser]:
        path = self.vertical_dir / "verification" / "parsers.py"
        if not path.is_file():
            return {}
        module = import_module_from(path, f"vertical_{self._manifest.id}_parsers")
        return dict(getattr(module, "PARSERS", {}))

    def _load_gates(self, parsers: dict[str, Parser] | None) -> dict[str, CommandGate]:
        path = self._gates_file()
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        parsers = {**self._load_parsers(), **(parsers or {})}
        try:
            return gates_from_config(data.get("gates", []), parsers)
        except ValueError as exc:
            raise VerticalLoadError(f"{path}: {exc}") from exc

    # -- IVertical -----------------------------------------------------------------
    @property
    def manifest(self) -> VerticalManifest:
        return self._manifest

    @property
    def skills(self) -> dict[str, LoadedSkill]:
        return self.skill_registry.skills

    async def initialize_state(self, request: GenerateRequest) -> dict[str, Any]:
        return {"metadata": {"project_structure": self.project_structure()}}

    def project_structure(self) -> dict[str, str]:
        return dict(getattr(self._manifest, "project_structure", None) or {})

    def prompt_context(self, state: AgentState) -> dict[str, Any]:
        return {
            "manifest": self._manifest,
            "skills": self.skills,
            "tech_stack": self._manifest.tech_stack,
            "file_structure": (state.get("metadata") or {}).get("project_structure", {}),
            "retrieved_context": (state.get("metadata") or {}).get("rag_context", ""),
        }

    def get_planner_prompt(self, state: AgentState) -> str:
        return self.prompts.render_for_state("PLANNER.j2", state, **self.prompt_context(state))

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        return self.prompts.render_for_state("CODER.j2", state, task=task, **self.prompt_context(state))

    def get_fixer_prompt(self, state: AgentState, task: Task) -> str:
        return self.prompts.render_for_state(
            "FIXER.j2",
            state,
            task=task,
            failed_gates=state.get("current_gate_results", []),
            files_to_fix=(state.get("metadata") or {}).get("files_to_fix", []),
            **self.prompt_context(state),
        )

    def get_documenter_prompt(self, state: AgentState, doc_sources: list[dict[str, str]] | None = None) -> str:
        return self.prompts.render_for_state(
            "DOCUMENTER.j2", state, doc_sources=doc_sources or [], **self.prompt_context(state)
        )

    def gate_ids_for_task(self, task: Task, skill: LoadedSkill | None) -> list[str]:
        tags = set(skill.tags if skill else [])
        return [gid for gid, gate in self.gates.items() if gate.runs_on_every_task or (tags and tags & set(gate.tags))]

    def get_verification_gates(self, state: AgentState, task: Task | None) -> list[IVerificationGate]:
        if task is None:
            return [g for g in self.gates.values() if g.config.severity == "error"]
        skill = self.skills.get(task.skill_id) if task.skill_id else None
        gates: list[IVerificationGate] = [self.gates[gid] for gid in self.gate_ids_for_task(task, skill)]
        if skill is not None and (skill.validation or skill.validate_hook is not None):
            gates.append(SkillValidationGate(skill, task.inputs, self.tool_registry))
        return gates

    def get_skill_executor(self, skill_id: str) -> SkillExecutor:
        return self.skill_registry.get_executor(skill_id)

    async def on_task_complete(self, state: AgentState, task: Task) -> None:
        return None

    async def finalize(self, state: AgentState) -> dict[str, Any]:
        return {}
