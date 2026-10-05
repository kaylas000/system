# specs/03_skills/skill/TEMPLATE_ENGINE.py
"""
Jinja2 Template Engine with Custom Filters & Globals for Code Gen.
Features:
- Strict Undefined (catch typos in templates)
- Custom Filters: to_json, to_yaml, slugify, class_name, snake_case, indent
- Globals: `now()`, `uuid()`, `env()`, `skill_output(skill_id)`
- Template Inheritance & Includes
- Streaming for large files (optional)
"""

from __future__ import annotations
import json
import yaml
import re
import uuid as uuid_lib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from jinja2 import Environment, FileSystemLoader, StrictUndefined, meta, TemplateError
from jinja2.filters import FILTERS as JINJA_DEFAULT_FILTERS

from kernel.state import FileChange

# --- Custom Filters ---


def to_json(value: Any, indent: int = 2) -> str:
    return json.dumps(value, indent=indent, ensure_ascii=False, default=str)


def to_yaml(value: Any) -> str:
    return yaml.dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False)


def slugify(value: str) -> str:
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", value.lower())).strip("-")


def class_name(value: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[-_\s]+", value) if w)


def snake_case(value: str) -> str:
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def pascal_case(value: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[-_\s]+", value) if w)


def indent(text: str, spaces: int = 4) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())


def regex_replace(text: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, text)


def file_exists(path: str, base_dir: Path) -> bool:
    return (base_dir / path).exists()


# --- Globals ---


def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.utcnow().strftime(fmt)


def uuid_short() -> str:
    return uuid_lib.uuid4().hex[:8]


def env(var: str, default: str = "") -> str:
    import os

    return os.getenv(var, default)


# --- Engine ---


class TemplateEngine:
    def __init__(self, template_dirs: List[Path], skill_outputs: Dict[str, Dict]):
        """
        template_dirs: List of directories to search (Skill dir, Vertical dir, Global dir).
        skill_outputs: Outputs from previously executed skills in this run {skill_id: outputs_dict}.
        """
        self.loader = FileSystemLoader([str(d) for d in template_dirs])
        self.env = Environment(
            loader=self.loader,
            undefined=StrictUndefined,  # Fail fast on missing vars
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            autoescape=False,  # Code gen usually doesn't need HTML escaping
        )
        self._register_filters()
        self._register_globals(skill_outputs)

    def _register_filters(self):
        self.env.filters.update(
            {
                "to_json": to_json,
                "to_yaml": to_yaml,
                "slugify": slugify,
                "class_name": class_name,
                "snake_case": snake_case,
                "pascal_case": pascal_case,
                "indent": indent,
                "regex_replace": regex_replace,
            }
        )
        # Keep default jinja filters too
        for k, v in JINJA_DEFAULT_FILTERS.items():
            if k not in self.env.filters:
                self.env.filters[k] = v

    def _register_globals(self, skill_outputs: Dict[str, Dict]):
        self.env.globals.update(
            {
                "now": now,
                "uuid": uuid_short,
                "env": env,
                "skill_output": lambda skill_id, key, default=None: skill_outputs.get(skill_id, {}).get(key, default),
                "has_skill": lambda skill_id: skill_id in skill_outputs,
            }
        )

    def render_string(self, template_str: str, context: Dict[str, Any]) -> str:
        try:
            template = self.env.from_string(template_str)
            return template.render(**context)
        except TemplateError as e:
            raise RuntimeError(f"Template render error: {e}") from e

    def render_file(self, template_path: Path, context: Dict[str, Any]) -> str:
        try:
            template = self.env.get_template(str(template_path))
            return template.render(**context)
        except TemplateError as e:
            raise RuntimeError(f"Template render error in {template_path}: {e}") from e

    def list_templates(self) -> List[str]:
        return self.env.list_templates()

    def get_template_ast(self, template_path: Path):
        """For static analysis / variable extraction."""
        source = self.loader.get_source(self.env, str(template_path))[0]
        return self.env.parse(source)

    def find_undeclared_variables(self, template_path: Path) -> set:
        ast = self.get_template_ast(template_path)
        return meta.find_undeclared_variables(ast)


# --- High Level Render Function ---


async def render_skill_templates(
    skill_dir: Path, inputs: Dict[str, Any], skill_outputs: Dict[str, Dict], workspace_root: Path
) -> List[FileChange]:
    """
    Renders all *.j2 files in skill_dir/template_dir preserving directory structure.
    Strips .j2 extension.
    """
    template_dir = skill_dir / "templates"
    if not template_dir.exists():
        return []

    engine = TemplateEngine(
        template_dirs=[template_dir, workspace_root],  # Allow inheriting from workspace
        skill_outputs=skill_outputs,
    )

    context = {**inputs, "skill_dir": str(skill_dir), "workspace_root": str(workspace_root)}

    changes = []
    for j2_path in template_dir.rglob("*.j2"):
        relative_path = j2_path.relative_to(template_dir)
        # Remove .j2 extension
        target_path = relative_path.with_suffix("")

        content = engine.render_file(j2_path, context)

        # Determine action: create vs update (check if exists in workspace)
        action = "create"
        if (workspace_root / target_path).exists():
            action = "update"

        changes.append(FileChange(path=str(target_path), content=content, action=action))
    return changes
