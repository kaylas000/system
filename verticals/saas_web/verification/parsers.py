"""
Gate output parsers for saas_web (written by the agent; replaces specs/.../PARSERS.py).

The spec regexes did not match real tool output (tsc prints ``file(line,col): error TSxxxx``,
``next lint`` prints the stylish format, not JSON). Each parser fills ``files_to_fix``
(workspace-relative) and ``issues`` for the fixer.
"""

from __future__ import annotations

import re
from typing import Any

from kernel.state import VerificationGateResult

_ROOTS = ("src/", "prisma/", "app/", "tests/", "public/", "scripts/")
_TSC = re.compile(r"^(?P<file>[^\s(][^(]*?)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.+)$", re.M)
_LINT_FILE = re.compile(r"^(?P<file>\.?/?[\w@./()\[\]-]+\.(?:tsx?|jsx?|mjs|cjs))$")
_LINT_ISSUE = re.compile(
    r"^\s*(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>Error|Warning):\s+(?P<msg>.+?)(?:\s{2,}(?P<rule>\S+))?$"
)
_LOC = re.compile(
    r"(?P<file>(?:\./|/)?[\w@./()\[\]-]+\.(?:tsx?|jsx?|mjs|cjs|prisma|css)):(?P<line>\d+)(?::(?P<col>\d+))?"
)


def rel(path: str) -> str | None:
    path = path.strip().removeprefix("./")
    if "node_modules/" in path or ".next/" in path:
        return None
    if path.startswith("/"):
        for root in _ROOTS:
            idx = path.find("/" + root)
            if idx >= 0:
                return path[idx + 1 :]
        return None
    return path or None


def _with(result: VerificationGateResult, issues: list[dict[str, Any]]) -> VerificationGateResult:
    files = sorted({i["file"] for i in issues if i.get("file")})
    return result.model_copy(update={"issues": issues[:200], "files_to_fix": files[:50]})


def _text(result: VerificationGateResult) -> str:
    return f"{result.stdout}\n{result.stderr}"


def parse_tsc(result: VerificationGateResult) -> VerificationGateResult:
    issues = []
    for m in _TSC.finditer(_text(result)):
        f = rel(m["file"])
        if f:
            issues.append(
                {"file": f, "line": int(m["line"]), "column": int(m["col"]), "code": m["code"], "message": m["msg"]}
            )
    return _with(result, issues)


def parse_eslint(result: VerificationGateResult) -> VerificationGateResult:
    issues: list[dict[str, Any]] = []
    current: str | None = None
    for line in _text(result).splitlines():
        fm = _LINT_FILE.match(line.strip())
        if fm:
            current = rel(fm["file"])
            continue
        im = _LINT_ISSUE.match(line)
        if im and current:
            issues.append(
                {
                    "file": current,
                    "line": int(im["line"]),
                    "column": int(im["col"]),
                    "severity": im["sev"].lower(),
                    "rule": im["rule"],
                    "message": im["msg"],
                }
            )
    return _with(result, issues)


def _locations(text: str, message_hint: str = "") -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in _LOC.finditer(line):
            f = rel(m["file"])
            if not f:
                continue
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            issues.append({"file": f, "line": int(m["line"]), "message": nxt or line.strip() or message_hint})
    return issues


def parse_nextjs_build(result: VerificationGateResult) -> VerificationGateResult:
    text = _text(result)
    issues = _locations(text)
    lint = parse_eslint(result).issues
    tsc = parse_tsc(result).issues
    seen: set[tuple[str, int]] = set()
    merged: list[dict[str, Any]] = []
    for issue in [*tsc, *lint, *issues]:
        key = (issue["file"], int(issue.get("line") or 0))
        if key not in seen:
            seen.add(key)
            merged.append(issue)
    return _with(result, merged)


def parse_vitest(result: VerificationGateResult) -> VerificationGateResult:
    text = _text(result)
    issues: list[dict[str, Any]] = []
    for m in re.finditer(r"FAIL\s+(?P<file>\S+\.(?:test|spec)\.tsx?)(?:\s+>\s+(?P<name>.+))?", text):
        f = rel(m["file"])
        if f:
            issues.append({"file": f, "message": f"failed: {m['name'] or 'suite'}"})
    issues += [i for i in _locations(text) if i["file"].startswith(("src/", "tests/"))]
    return _with(result, issues)


def parse_prisma(result: VerificationGateResult) -> VerificationGateResult:
    text = _text(result)
    issues = [
        {"file": "prisma/schema.prisma", "line": int(m["line"]), "message": m["msg"].strip()}
        for m in re.finditer(r"error: (?P<msg>.+?)\n\s*-->\s+\S*schema\.prisma:(?P<line>\d+)", text, flags=re.S)
    ]
    if not issues and result.exit_code != 0:
        issues = [{"file": "prisma/schema.prisma", "message": text.strip()[-500:]}]
    return _with(result, issues)


PARSERS = {
    "parse_tsc": parse_tsc,
    "parse_eslint": parse_eslint,
    "parse_nextjs_build": parse_nextjs_build,
    "parse_vitest": parse_vitest,
    "parse_prisma": parse_prisma,
}
