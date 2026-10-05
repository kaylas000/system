#!/usr/bin/env python3
"""
Report syntax problems in the spec files under ``specs/`` (Python / YAML / JSON).

The specs are kept verbatim (see specs/ISSUES.md), so this script only reports;
it exits 0 unless ``--strict`` is given. Used to keep ISSUES.md in sync.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "specs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit 1 if any problem is found")
    args = ap.parse_args()
    problems: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT.parent)
        try:
            if path.suffix == ".py":
                ast.parse(path.read_text(encoding="utf-8"))
            elif path.suffix in (".yaml", ".yml"):
                yaml.safe_load(path.read_text(encoding="utf-8"))
            elif path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix == ".jsonl":
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        json.loads(line)
        except SyntaxError as exc:
            problems.append(f"{rel}:{exc.lineno}: python: {exc.msg}: {(exc.text or '').strip()[:70]}")
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = mark.line + 1 if mark else 0
            problems.append(f"{rel}:{line}: yaml: {getattr(exc, 'problem', exc)}")
        except json.JSONDecodeError as exc:
            problems.append(f"{rel}:{exc.lineno}: json: {exc.msg}")
    print("\n".join(problems) or "no syntax problems")
    print(f"\n{len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
