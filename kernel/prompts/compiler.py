"""
Prompt compiler: renders a vertical's Jinja2 prompt templates.

NOTE: ``prompts/compiler.py`` is imported by ``specs/05_vertical_saas_web/VERTICAL_IMPL.py``
(``from .prompts.compiler import PromptCompiler``) but has no content in the spec
(``specs/MISSING_FILES.md``). This module is AUTHORED BY THE AGENT.

Design:
* search path: the vertical's ``prompts/`` first, then the kernel defaults
  (``kernel/prompts/defaults/``) so a vertical may omit e.g. ``FIXER.j2``;
* prompts are prose, not code: missing variables render as empty strings
  (``ChainableUndefined``) instead of aborting a run; ``undeclared()`` lets tests
  check which variables a template expects;
* the context is ``{**state, **extra}`` so templates can use ``user_prompt``,
  ``constraints`` etc. directly (as the spec templates do);
* ``<system>...</system>`` / ``<user>...</user>`` blocks (spec template format) can
  be split into chat messages with ``split_messages``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jinja2 import ChainableUndefined, FileSystemLoader, TemplateNotFound, meta
from jinja2.sandbox import SandboxedEnvironment

from ..jinja_filters import FILTERS

DEFAULTS_DIR = Path(__file__).parent / "defaults"
_BLOCK_RE = re.compile(r"<(system|user)>\s*(.*?)\s*</\1>", re.DOTALL)


class PromptNotFoundError(LookupError):
    pass


class PromptCompiler:
    def __init__(self, prompt_dirs: Path | str | Iterable[Path | str] | None = None, *, use_defaults: bool = True):
        if prompt_dirs is None:
            dirs: list[Path] = []
        elif isinstance(prompt_dirs, str | Path):
            dirs = [Path(prompt_dirs)]
        else:
            dirs = [Path(d) for d in prompt_dirs]
        if use_defaults:
            dirs.append(DEFAULTS_DIR)
        self.dirs = [d for d in dirs if d.is_dir()]
        self.env = SandboxedEnvironment(
            loader=FileSystemLoader([str(d) for d in self.dirs]),
            undefined=ChainableUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
            autoescape=False,
        )
        self.env.filters.update(FILTERS)
        self.env.globals.update(FILTERS)

    def has(self, name: str) -> bool:
        try:
            self.env.get_template(name)
        except TemplateNotFound:
            return False
        return True

    def render(self, template_name: str, context: Mapping[str, Any] | None = None, /, **extra: Any) -> str:
        try:
            template = self.env.get_template(template_name)
        except TemplateNotFound as exc:
            raise PromptNotFoundError(f"prompt template {template_name!r} not found in {self.dirs}") from exc
        ctx = {**dict(context or {}), **extra}
        return template.render(**ctx).strip() + "\n"

    def render_for_state(self, template_name: str, state: Mapping[str, Any], /, **extra: Any) -> str:
        """Render with ``state`` keys at top level plus ``state`` itself and ``extra``."""
        return self.render(template_name, {**dict(state), "state": state, **extra})

    def undeclared(self, name: str) -> set[str]:
        source = self.env.loader.get_source(self.env, name)[0]  # type: ignore[union-attr]
        return set(meta.find_undeclared_variables(self.env.parse(source)))


def split_messages(rendered: str) -> tuple[str, str]:
    """Split ``<system>``/``<user>`` blocks. Returns (system, user); no tags -> (all, "")."""
    blocks = {kind: body for kind, body in _BLOCK_RE.findall(rendered)}
    if not blocks:
        return rendered.strip(), ""
    return blocks.get("system", "").strip(), blocks.get("user", "").strip()
