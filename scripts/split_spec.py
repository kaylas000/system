#!/usr/bin/env python3
"""Split TECHNICAL_SPECIFICATION.md into the specs/ directory tree.

Every section of the form ``## N. `specs/<path>` `` is followed by a fenced
code block; the content of that block is written verbatim to ``<path>``.
For every part (``# Ответ №N``) a README.md with the part's introduction,
developer instruction and Definition of Done is written to its specs folder.

The source document is never modified. Re-running the script is idempotent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "TECHNICAL_SPECIFICATION.md"

SECTION_RE = re.compile(r"^## \d+\. `(specs/[^`]+)`")
FENCE_RE = re.compile(r"^ {0,3}(`{3,})(.*)$")
PART_RE = re.compile(r"^# Ответ №(\d+)")
PART_DIRS = {
    1: "specs/01_kernel",
    2: "specs/02_infra",
    3: "specs/03_skills",
    4: "specs/04_knowledge",
    5: "specs/05_vertical_saas_web",
    6: "specs/06_ops",
    7: "specs/07_gateway",
}


def extract_block(lines: list[str], start: int) -> tuple[str, int]:
    """Return content of the first fenced block at/after ``start``."""
    i = start
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if m:
            break
        i += 1
    else:
        raise ValueError(f"no code block after line {start + 1}")
    ticks = len(m.group(1))
    body: list[str] = []
    j = i + 1
    while j < len(lines):
        m2 = FENCE_RE.match(lines[j])
        if m2 and len(m2.group(1)) >= ticks and m2.group(2).strip() == "":
            return "\n".join(body) + "\n", j
        body.append(lines[j])
        j += 1
    raise ValueError(f"unclosed code block starting at line {i + 1}")


def main() -> int:
    lines = SRC.read_text(encoding="utf-8").split("\n")

    # --- 1. Code / document sections ---
    written = 0
    for idx, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if not m:
            continue
        target = ROOT / m.group(1)
        content, _ = extract_block(lines, idx + 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1
        print(f"  wrote {target.relative_to(ROOT)}")

    # --- 2. Part README files (intro + instruction + DoD) ---
    part_starts = [(int(m.group(1)), i) for i, line in enumerate(lines) if (m := PART_RE.match(line))]
    for n, (part, start) in enumerate(part_starts):
        end = part_starts[n + 1][1] if n + 1 < len(part_starts) else len(lines)
        chunk = lines[start:end]
        first_section = next(i for i, line in enumerate(chunk) if SECTION_RE.match(line))
        tail_start = next((i for i, line in enumerate(chunk) if line.startswith("### 🎯")), len(chunk))
        readme = [
            *chunk[:first_section],
            "",
            "<!-- ... файлы спецификации лежат в этой папке ... -->",
            "",
            *chunk[tail_start:],
        ]
        path = ROOT / PART_DIRS[part] / "README.md"
        path.write_text("\n".join(readme).rstrip() + "\n", encoding="utf-8")
        print(f"  wrote {path.relative_to(ROOT)}")

    print(f"{written} spec files + {len(part_starts)} README files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
