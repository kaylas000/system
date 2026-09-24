#!/usr/bin/env bash
# Seed the knowledge base (specs/06_ops/scripts/SEED_KNOWLEDGE.sh; written by the agent).
#
#   scripts/ops/seed_knowledge.sh                 # all knowledge_sources of every vertical
#   scripts/ops/seed_knowledge.sh saas_web        # one vertical
#   EXTRA_REPOS="https://github.com/org/repo.git" scripts/ops/seed_knowledge.sh
#
# Uses AUTOGEN_VECTOR_DB__QDRANT_URL / AUTOGEN_KNOWLEDGE__* from the environment.
# Without LLM keys: AUTOGEN_KNOWLEDGE__EMBEDDER=hashing and --no-enrich are applied automatically.
set -euo pipefail
cd "$(dirname "$0")/../.."

args=()
if [ -z "${AUTOGEN_LLM__API_KEY:-}${OPENAI_API_KEY:-}" ]; then
  echo "no LLM key: offline hashing embedder, no enrichment" >&2
  export AUTOGEN_KNOWLEDGE__EMBEDDER=hashing
  args+=(--no-enrich)
fi

verticals=("$@")
if [ ${#verticals[@]} -eq 0 ]; then
  mapfile -t verticals < <(python - <<'PY'
from pathlib import Path
from kernel.skills import VerticalLoader
from kernel.config import get_settings
print("\n".join(sorted(VerticalLoader(Path(get_settings().kernel.verticals_dir)).discover())))
PY
)
fi

for v in "${verticals[@]}"; do
  echo "==> vertical $v"
  autogen-knowledge reindex --vertical "$v" "${args[@]}" || echo "  skipped: $v (no knowledge_sources or ingest error)" >&2
done

for repo in ${EXTRA_REPOS:-}; do
  echo "==> repo $repo"
  autogen-knowledge ingest "$repo" "${args[@]}"
done

autogen-knowledge stats
