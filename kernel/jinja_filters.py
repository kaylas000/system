"""Jinja2 filters shared by skill templates and prompt templates (no dependencies on kernel packages)."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml


def to_json(value: Any, indent: int | None = 2) -> str:
    return json.dumps(value, indent=indent, ensure_ascii=False, default=str)


def to_yaml(value: Any) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False)


def slugify(value: str) -> str:
    return re.sub(r"[-\s_]+", "-", re.sub(r"[^\w\s-]", "", str(value).lower())).strip("-")


def _words(value: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [w for w in re.split(r"[^A-Za-z0-9]+", s) if w]


def pascal_case(value: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in _words(value))


def class_name(value: str) -> str:
    return pascal_case(value)


def camel_case(value: str) -> str:
    p = pascal_case(value)
    return p[:1].lower() + p[1:]


def snake_case(value: str) -> str:
    return "_".join(w.lower() for w in _words(value))


def kebab_case(value: str) -> str:
    return "-".join(w.lower() for w in _words(value))


def indent_text(text: str, spaces: int = 4, first: bool = True) -> str:
    prefix = " " * spaces
    lines = str(text).splitlines()
    return "\n".join((prefix + line if (i or first) and line else line) for i, line in enumerate(lines))


def regex_replace(text: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, str(text))


FILTERS: dict[str, Any] = {
    "to_json": to_json,
    "to_yaml": to_yaml,
    "slugify": slugify,
    "class_name": class_name,
    "pascal_case": pascal_case,
    "camel_case": camel_case,
    "snake_case": snake_case,
    "kebab_case": kebab_case,
    "indent_code": indent_text,
    "regex_replace": regex_replace,
}
