#!/usr/bin/env bash
# Create / upgrade the LangGraph checkpoint tables (specs/06_ops/scripts/MIGRATE_DB.sh; written by the agent).
#   AUTOGEN_DATABASE__POSTGRES_DSN=postgresql://... scripts/ops/migrate_db.sh
# The kernel also runs this on start-up; the script is for deploy pipelines (run before rollout).
set -euo pipefail
: "${AUTOGEN_DATABASE__POSTGRES_DSN:?set AUTOGEN_DATABASE__POSTGRES_DSN}"
exec python -m kernel.main migrate
