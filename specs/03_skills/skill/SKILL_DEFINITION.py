# specs/03_skills/skill/SKILL_DEFINITION.py
"""
Pydantic Models for Skill Definition.
Loaded from skill.yaml + hooks.py discovery.
"""

from __future__ import annotations
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Awaitable, Type
from pydantic import BaseModel, Field, ConfigDict
from kernel.state import FileChange, VerificationGateResult

# --- Input/Output Schemas ---


class SkillInputSchema(BaseModel):
    """Dynamic schema generated from skill.yaml 'inputs'."""

    # We use a generic dict validated against JSON Schema at runtime
    model_config = ConfigDict(extra="allow")


class SkillOutput(BaseModel):
    """Structured output returned to Kernel State."""

    file_changes: List[FileChange] = []
    outputs: Dict[str, Any] = {}  # e.g. {"nextjs_version": "14"}
    verification_results: List[VerificationGateResult] = []
    metadata: Dict[str, Any] = {}  # Arbitrary data for next skills


# --- Hook Types ---


class HookContext(BaseModel):
    """Context passed to all hooks."""

    skill_id: str
    skill_dir: Path
    inputs: Dict[str, Any]
    sandbox: Any  # ISandbox instance
    sandbox_id: str
    workspace: str
    state: Dict[str, Any]  # Full AgentState (read-only for pre-hooks)
    tool_registry: Any  # ToolRegistry instance


# Hook Signatures
PreRenderHook = Callable[[HookContext], Awaitable[Dict[str, Any]]]  # Returns modified inputs
PostRenderHook = Callable[[HookContext, List[FileChange]], Awaitable[List[FileChange]]]  # Can modify FileChanges
ValidationHook = Callable[[HookContext], Awaitable[List[VerificationGateResult]]]  # Custom validation

# --- Main Skill Definition ---


class SkillDef(BaseModel):
    """Loaded, validated, ready-to-execute Skill."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # From skill.yaml
    id: str
    name: str
    version: str
    description: str
    category: str = "scaffold"
    tags: List[str] = []
    inputs_schema: Dict[str, Any] = Field(default_factory=dict)  # Raw JSON Schema
    depends_on: List[str] = []
    provides: List[str] = []
    template_dir: str = "templates"
    entrypoint: str = "hooks.py"
    post_scripts: List[str] = []
    env: Dict[str, str] = {}
    validation: List[str] = []
    outputs_schema: Dict[str, Any] = {}
    compatible_verticals: List[str] = []
    min_kernel_version: str = "1.0.0"

    # Runtime (Loaded from hooks.py)
    _pre_render: Optional[PreRenderHook] = Field(default=None, exclude=True)
    _post_render: Optional[PostRenderHook] = Field(default=None, exclude=True)
    _validate: Optional[ValidationHook] = Field(default=None, exclude=True)

    @classmethod
    def load_from_dir(cls, skill_dir: Path) -> "SkillDef":
        """Load skill.yaml, discover hooks, validate structure."""
        manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"skill.yaml not found in {skill_dir}")

        import yaml

        data = yaml.safe_load(manifest_path.read_text())

        # Load Hooks
        hooks_path = skill_dir / data.get("entrypoint", "hooks.py")
        hooks = {}
        if hooks_path.exists():
            spec = importlib.util.spec_from_file_location(f"skill_hooks_{data['id']}", hooks_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Discover hooks by name convention
            for name in ["pre_render", "post_render", "validate"]:
                if hasattr(module, name) and inspect.iscoroutinefunction(getattr(module, name)):
                    hooks[f"_{name}"] = getattr(module, name)

        return cls(**data, **hooks)

    def validate_inputs(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Validate inputs against JSON Schema (using jsonschema library)."""
        from jsonschema import validate, ValidationError

        try:
            validate(instance=inputs, schema=self.inputs_schema)
            return inputs
        except ValidationError as e:
            raise ValueError(f"Skill '{self.id}' input validation failed: {e.message}") from e
