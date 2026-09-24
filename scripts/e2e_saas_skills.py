#!/usr/bin/env python3
"""
End-to-end check of the saas_web skills WITHOUT an LLM (written by the agent).

Runs the standard skeleton plan (Todo app with auth) the way the graph does:
skill executor -> task gates (every-task + tag gates + skill:<id>) -> next task, and at the end
the project-level verification (all blocking gates incl. `next build`).

Requires node >= 20 and network access (npm registry). Uses LocalSandbox (no isolation).

    python scripts/e2e_saas_skills.py --pm npm --keep /tmp/saas-e2e
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kernel.protocols import SandboxSpec
from kernel.sandbox.local import LocalSandbox
from kernel.skills import VerticalLoader
from kernel.state import Task, VerificationGateStatus

WS = "/workspace"


def plan(pm: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("init_nextjs_app_router", {"project_name": "todo-saas", "title": "Todo SaaS", "package_manager": pm}),
        ("init_prisma_postgres", {}),
        ("init_trpc_setup", {}),
        ("add_nextauth_credentials", {"github_oauth": True}),
        ("add_shadcn_ui", {}),
        (
            "add_prisma_model",
            {
                "model_name": "Todo",
                "fields": [
                    {"name": "title", "type": "String"},
                    {"name": "notes", "type": "String", "optional": True, "text": True},
                    {"name": "done", "type": "Boolean", "default": "false"},
                    {"name": "priority", "type": "Enum", "enum": "Priority", "default": "MEDIUM"},
                    {"name": "estimate", "type": "Int", "optional": True},
                    {"name": "dueDate", "type": "DateTime", "optional": True},
                ],
                "enums": [{"name": "Priority", "values": ["LOW", "MEDIUM", "HIGH"]}],
            },
        ),
        ("add_trpc_router", {"router_name": "todo", "model_name": "Todo"}),
        ("add_crud_page", {"router_name": "todo", "model_name": "Todo"}),
        ("add_dockerfile_prod", {}),
        ("add_github_actions_ci", {}),
    ]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pm", choices=["npm", "pnpm"], default="npm")
    ap.add_argument("--keep", help="base dir for the sandbox (kept after the run)")
    ap.add_argument(
        "--fake-prisma-engines",
        action="store_true",
        help="for networks without binaries.prisma.sh: point Prisma at dummy engine files "
        "(validate/format/generate work via WASM; the app cannot query a DB)",
    )
    ap.add_argument("--skip-final", action="store_true", help="skip project-level verification (next build)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    vertical = VerticalLoader(root / "verticals").load_vertical("saas_web")
    sandbox = LocalSandbox(args.keep)
    env = {"NEXT_TELEMETRY_DISABLED": "1", "CI": "1"}
    if args.fake_prisma_engines:
        fake = Path(tempfile.mkdtemp(prefix="fake-prisma-"))
        (fake / "libquery_engine.so.node").touch()
        (fake / "schema-engine").touch(mode=0o755)
        env |= {
            "PRISMA_QUERY_ENGINE_LIBRARY": str(fake / "libquery_engine.so.node"),
            "PRISMA_SCHEMA_ENGINE_BINARY": str(fake / "schema-engine"),
            "PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING": "1",
        }
    sid = await sandbox.create(SandboxSpec(image="local", env_vars=env, timeout_sec=900))
    print(f"workspace: {sandbox.host_path(sid, WS)}", flush=True)
    state: dict[str, Any] = {"skill_outputs": {}, "sandbox_id": sid, "workspace_path": WS}
    failed = 0
    for i, (skill_id, inputs) in enumerate(plan(args.pm), 1):
        task = Task(id=f"t{i}", name=skill_id, description=skill_id, skill_id=skill_id, inputs=inputs)
        state["task_graph"] = [task]
        state["current_task_id"] = task.id
        t0 = time.perf_counter()
        result = await vertical.get_skill_executor(skill_id).execute(sandbox, sid, WS, inputs, state)  # type: ignore[arg-type]
        state["skill_outputs"][skill_id] = dict(result.outputs)
        gates = vertical.get_verification_gates(state, task)  # type: ignore[arg-type]
        results = await asyncio.gather(
            *(g.execute(sandbox, sid, WS, state) for g in gates if not getattr(g, "exclusive", False))
        )  # type: ignore[arg-type]
        for g in gates:
            if getattr(g, "exclusive", False):
                results.append(await g.execute(sandbox, sid, WS, state))  # type: ignore[arg-type]
        bad = [r for r in results if r.status != VerificationGateStatus.PASSED]
        status = "OK " if not bad else "FAIL"
        print(
            f"[{status}] {task.id} {skill_id}: {len(result.file_changes)} files, "
            f"gates {', '.join(r.gate_id for r in results)} ({time.perf_counter() - t0:.0f}s)",
            flush=True,
        )
        for r in bad:
            failed += 1
            print(f"   {r.gate_id} exit={r.exit_code} files={r.files_to_fix}\n{(r.stdout + r.stderr)[-3000:]}")
        if bad:
            return 1
    if not args.skip_final:
        t0 = time.perf_counter()
        for gate in vertical.get_verification_gates(state, None):  # type: ignore[arg-type]
            r = await gate.execute(sandbox, sid, WS, state)  # type: ignore[arg-type]
            print(f"[final] {r.gate_id}: {r.status.value} ({r.duration_ms / 1000:.0f}s)", flush=True)
            if r.status != VerificationGateStatus.PASSED:
                failed += 1
                print((r.stdout + r.stderr)[-4000:])
        print(f"final verification {time.perf_counter() - t0:.0f}s")
    if not args.keep:
        await sandbox.close(sid)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
