#!/usr/bin/env bash
# Regenerate pinned requirements for the Docker images (needs `uv`: pip install uv).
set -euo pipefail
cd "$(dirname "$0")/.."
common=(--python-version 3.11 --python-platform x86_64-manylinux_2_28 --no-header --annotation-style line -q)
uv pip compile pyproject.toml --extra postgres --extra llm --extra e2b --extra ops --extra knowledge \
  "${common[@]}" -o deploy/docker/requirements-kernel.lock
uv pip compile pyproject.toml --extra knowledge --extra llm "${common[@]}" -o deploy/docker/requirements-knowledge.lock
echo "locks updated"
