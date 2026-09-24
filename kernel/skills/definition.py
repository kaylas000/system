"""
Skill definition: ``skill.yaml`` + optional ``hooks.py`` (port of ``specs/03_skills/skill/SKILL_DEFINITION.py``).

Fixes vs. the spec (``specs/ISSUES.md`` SK-04):
* the spec declared hooks as pydantic fields named ``_pre_render`` etc. - pydantic
  treats underscore names as private attributes, so ``cls(**data, **hooks)`` silently
  dropped them and hooks never ran; here hooks are stored as private attributes;
* ``skill.yaml`` calls the input schema ``inputs`` and output templates ``outputs``;
  the spec model expected ``inputs_schema`` / ``outputs_schema`` and lost them;
* defaults from the JSON Schema are applied before validation (templates rely on them);
* hook modules get unique names and can import ``kernel.skills`` (the spec used
  relative imports to a non-package directory).
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from ..protocols import SkillDef
from ..state import FileChange, VerificationGateResult

ID_RE = re.compile(r"^[a-z0-9_-]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-z0-9.]+)?$")
CATEGORIES = {
    "scaffold",
    "config",
    "feature",
    "integration",
    "migration",
    "test",
    "ci_cd",
    "docs",
    "infra",
}  # "infra": CATALOG.md


class SkillLoadError(ValueError):
    pass


class SkillInputError(ValueError):
    pass


class HookContext(BaseModel):
    """Context passed to all hooks."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    skill_id: str
    skill_dir: Path
    inputs: dict[str, Any]
    sandbox: Any  # ISandbox
    sandbox_id: str
    workspace: str
    state: dict[str, Any]  # read-only view of AgentState
    tool_registry: Any = None  # ToolRegistry | None


PreRenderHook = Callable[[HookContext], Awaitable[dict[str, Any]]]
PostRenderHook = Callable[[HookContext, list[FileChange]], Awaitable[list[FileChange]]]
ValidationHook = Callable[[HookContext], Awaitable[list[VerificationGateResult]]]
HOOK_NAMES = ("pre_render", "post_render", "validate")


def apply_schema_defaults(schema: dict[str, Any], value: Any) -> Any:
    """Fill ``default`` values from a JSON Schema (objects and arrays of objects, recursively)."""
    if not isinstance(schema, dict):
        return value
    if schema.get("type") == "object" or "properties" in schema:
        if value is None and "default" in schema:
            value = copy.deepcopy(schema["default"])
        if isinstance(value, dict):
            out = dict(value)
            for key, sub in (schema.get("properties") or {}).items():
                if key not in out and isinstance(sub, dict) and "default" in sub:
                    out[key] = copy.deepcopy(sub["default"])
                if key in out:
                    out[key] = apply_schema_defaults(sub, out[key])
            return out
    if schema.get("type") == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [apply_schema_defaults(schema["items"], v) for v in value]
    return value


class LoadedSkill(SkillDef):
    """A validated skill ready to execute. Extends the kernel ``SkillDef``."""

    model_config = ConfigDict(extra="ignore")

    category: str = "scaffold"
    provides: list[str] = Field(default_factory=list)
    entrypoint: str = "hooks.py"
    env: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)  # values are Jinja templates over inputs
    compatible_verticals: list[str] = Field(default_factory=list)
    min_kernel_version: str = "1.0.0"
    skill_dir: str = ""

    _pre_render: PreRenderHook | None = PrivateAttr(default=None)
    _post_render: PostRenderHook | None = PrivateAttr(default=None)
    _validate: ValidationHook | None = PrivateAttr(default=None)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"invalid skill id {v!r} (allowed: a-z 0-9 _ -)")
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not SEMVER_RE.match(str(v)):
            raise ValueError(f"version {v!r} is not SemVer")
        return str(v)

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v

    # -- hooks ---------------------------------------------------------------------
    @property
    def pre_render(self) -> PreRenderHook | None:
        return self._pre_render

    @property
    def post_render(self) -> PostRenderHook | None:
        return self._post_render

    @property
    def validate_hook(self) -> ValidationHook | None:
        return self._validate

    def set_hooks(self, **hooks: Any) -> None:
        for name, fn in hooks.items():
            if name not in HOOK_NAMES:
                raise ValueError(f"unknown hook {name}")
            setattr(self, f"_{name}", fn)

    @property
    def inputs_schema(self) -> dict[str, Any]:  # name used by the spec prompts (PLANNER.j2)
        return self.inputs_json_schema

    @property
    def path(self) -> Path:
        return Path(self.skill_dir)

    @property
    def template_path(self) -> Path:
        return self.path / self.template_dir

    # -- loading -------------------------------------------------------------------
    @classmethod
    def load_from_dir(cls, skill_dir: Path) -> LoadedSkill:
        manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            raise SkillLoadError(f"skill.yaml not found in {skill_dir}")
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise SkillLoadError(f"{manifest_path}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise SkillLoadError(f"{manifest_path}: top level must be a mapping")
        data = dict(data)
        # skill.yaml field names -> model field names
        if "inputs" in data:
            data["inputs_json_schema"] = data.pop("inputs") or {}
        data.setdefault("inputs_json_schema", {})
        data["skill_dir"] = str(skill_dir.resolve())
        try:
            skill = cls.model_validate(data)
        except ValueError as exc:
            raise SkillLoadError(f"{manifest_path}: {exc}") from exc
        if skill.inputs_json_schema:
            from jsonschema import Draft202012Validator, SchemaError

            try:
                Draft202012Validator.check_schema(skill.inputs_json_schema)
            except SchemaError as exc:
                raise SkillLoadError(f"{manifest_path}: invalid inputs schema: {exc.message}") from exc
        skill._load_hooks()
        return skill

    def _load_hooks(self) -> None:
        hooks_path = self.path / self.entrypoint
        if not hooks_path.is_file():
            return
        module_name = f"kernel_skill_hooks_{self.id}_{abs(hash(str(hooks_path)))}"
        spec = importlib.util.spec_from_file_location(module_name, hooks_path)
        if spec is None or spec.loader is None:
            raise SkillLoadError(f"cannot import {hooks_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise SkillLoadError(f"{hooks_path}: {type(exc).__name__}: {exc}") from exc
        for name in HOOK_NAMES:
            fn = getattr(module, name, None)
            if fn is None:
                continue
            if not inspect.iscoroutinefunction(fn):
                raise SkillLoadError(f"{hooks_path}: hook {name} must be `async def`")
            setattr(self, f"_{name}", fn)

    # -- inputs --------------------------------------------------------------------
    def validate_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Apply schema defaults, then validate against the JSON Schema. Returns a new dict."""
        schema = self.inputs_json_schema
        if not schema:
            return dict(inputs)
        filled = apply_schema_defaults(schema, dict(inputs))
        from jsonschema import Draft202012Validator

        errors = sorted(Draft202012Validator(schema).iter_errors(filled), key=lambda e: list(e.path))
        if errors:
            msgs = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:10])
            raise SkillInputError(f"Skill '{self.id}' input validation failed: {msgs}")
        return dict(filled)

    def summary(self) -> dict[str, Any]:
        """Compact description for planner prompts / state (JSON-serializable)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description.strip(),
            "version": self.version,
            "category": self.category,
            "tags": self.tags,
            "depends_on": self.depends_on,
            "provides": self.provides,
            "inputs_schema": self.inputs_json_schema,
        }
