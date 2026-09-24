"""Skills framework: definitions, template engine, executor, gates, registry, vertical loader."""

from .definition import HookContext, LoadedSkill, SkillInputError, SkillLoadError, apply_schema_defaults
from .executor import SkillExecutionError, SkillExecutor
from .gates import CommandGate, GateConfig, SkillValidationGate, gates_from_config
from .generic_vertical import GenericVertical, VerticalLoadError
from .loader import VerticalLoader
from .registry import SkillRegistry
from .templates import TemplateEngine, TemplateRenderError, render_skill_templates

__all__ = [
    "CommandGate",
    "GateConfig",
    "GenericVertical",
    "HookContext",
    "LoadedSkill",
    "SkillExecutionError",
    "SkillExecutor",
    "SkillInputError",
    "SkillLoadError",
    "SkillRegistry",
    "SkillValidationGate",
    "TemplateEngine",
    "TemplateRenderError",
    "VerticalLoadError",
    "VerticalLoader",
    "apply_schema_defaults",
    "gates_from_config",
    "render_skill_templates",
]
