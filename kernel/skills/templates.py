"""
Jinja2 template engine for code generation (port of ``specs/03_skills/skill/TEMPLATE_ENGINE.py``).

Fixes vs. the spec (``specs/ISSUES.md`` SK-05):
* the spec added ``workspace_root`` (a path *inside the sandbox*) to the local
  loader and decided create/update with ``Path.exists()`` on the host - both are
  meaningless for a remote sandbox; existence is now checked by the executor;
* filters are also exposed as globals (the example skills call ``snake_case(x)``);
* ``env()`` global removed: templates must not read the orchestrator's environment
  (secrets leak into generated code). Pass values via inputs instead;
* rendering uses a sandboxed environment and rejects target paths that escape
  the workspace; path segments may contain ``{{ var }}``;
* ``now()`` uses timezone-aware UTC.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from jinja2 import FileSystemLoader, StrictUndefined, TemplateError, meta
from jinja2.sandbox import SandboxedEnvironment

from ..jinja_filters import (  # noqa: F401  (re-exported for hooks/templates)
    FILTERS,
    camel_case,
    class_name,
    indent_text,
    kebab_case,
    pascal_case,
    regex_replace,
    slugify,
    snake_case,
    to_json,
    to_yaml,
)
from ..state import FileChange

# -- globals ---------------------------------------------------------------------------


def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(UTC).strftime(fmt)


def uuid_short() -> str:
    return uuid_lib.uuid4().hex[:8]


class TemplateRenderError(RuntimeError):
    pass


class TemplateEngine:
    def __init__(self, template_dirs: Iterable[Path], skill_outputs: dict[str, dict[str, Any]] | None = None) -> None:
        dirs = [str(d) for d in template_dirs if Path(d).is_dir()]
        self.loader = FileSystemLoader(dirs)
        self.env = SandboxedEnvironment(
            loader=self.loader,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            autoescape=False,
        )
        outputs = dict(skill_outputs or {})
        self.env.filters.update(FILTERS)
        self.env.globals.update(FILTERS)
        self.env.globals.update(
            {
                "now": now,
                "uuid": uuid_short,
                "skill_output": lambda skill_id, key, default=None: outputs.get(skill_id, {}).get(key, default),
                "has_skill": lambda skill_id: skill_id in outputs,
            }
        )

    def render_string(self, template_str: str, context: dict[str, Any]) -> str:
        try:
            return self.env.from_string(template_str).render(**context)
        except TemplateError as exc:
            raise TemplateRenderError(f"Template render error: {exc}") from exc

    def render_file(self, name: str, context: dict[str, Any]) -> str:
        try:
            return self.env.get_template(name).render(**context)
        except TemplateError as exc:
            raise TemplateRenderError(f"Template render error in {name}: {exc}") from exc

    def list_templates(self) -> list[str]:
        return self.env.list_templates()

    def find_undeclared_variables(self, name: str) -> set[str]:
        source = self.loader.get_source(self.env, name)[0]
        return set(meta.find_undeclared_variables(self.env.parse(source)))


def safe_relative(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise TemplateRenderError(f"Template target path escapes the workspace: {path!r}")
    return p.as_posix()


def render_skill_templates(
    template_dir: Path,
    inputs: dict[str, Any],
    skill_outputs: dict[str, dict[str, Any]] | None = None,
    extra_dirs: Iterable[Path] = (),
    assets_dir: Path | None = None,
) -> list[FileChange]:
    """Render every ``*.j2`` under ``template_dir`` (``.j2`` stripped, ``{{ }}`` allowed in paths).

    Files in ``assets_dir`` are copied verbatim (text files only). Actions are all
    ``create``; the executor turns them into ``update`` for files that already exist.
    """
    changes: dict[str, FileChange] = {}
    engine = TemplateEngine([template_dir, *extra_dirs], skill_outputs)
    if assets_dir is not None and assets_dir.is_dir():
        for f in sorted(assets_dir.rglob("*")):
            if f.is_file():
                rel = safe_relative(f.relative_to(assets_dir).as_posix())
                changes[rel] = FileChange(path=rel, content=f.read_text(encoding="utf-8"), action="create")
    if not template_dir.is_dir():
        return list(changes.values())
    for j2 in sorted(template_dir.rglob("*.j2")):
        name = j2.relative_to(template_dir).as_posix()
        target_tpl = name[: -len(".j2")]
        target = engine.render_string(target_tpl, inputs) if "{{" in target_tpl or "{%" in target_tpl else target_tpl
        rel = safe_relative(target.strip())
        content = engine.render_file(name, inputs)
        changes[rel] = FileChange(path=rel, content=content, action="create")
    return list(changes.values())
