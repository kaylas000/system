"""``initialize`` node: load vertical context, create sandbox, init state."""

from __future__ import annotations

from typing import Any

from ...protocols import GenerateRequest, SandboxSpec
from ...state import AgentState, RunStatus, TokenUsage, utcnow
from ..deps import KernelDeps
from ._common import log, merged_metadata

NODE = "initialize"


def request_from_state(state: AgentState) -> GenerateRequest:
    return GenerateRequest(
        prompt=state.get("user_prompt", ""),
        vertical_id=state.get("vertical_id"),
        tech_stack_hints=state.get("tech_stack_hints", {}),
        constraints=state.get("constraints", []),
        context_files=state.get("context_files", []),
        max_budget_usd=state.get("max_budget_usd"),
    )


def sandbox_spec_for(deps: KernelDeps, env_extra: dict[str, str] | None = None) -> SandboxSpec:
    runtime = deps.vertical.manifest.runtime
    s = deps.settings.sandbox
    return SandboxSpec(
        image=str(runtime.get("docker_image") or s.default_image),
        cpu=int(runtime.get("cpu", s.cpu)),
        memory_mb=int(runtime.get("memory_mb", s.memory_mb)),
        env_vars={**{str(k): str(v) for k, v in (runtime.get("env_vars") or {}).items()}, **(env_extra or {})},
        ports=[int(p) for p in runtime.get("ports", [])],
        timeout_sec=s.timeout_sec,
    )


async def initialize_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    if not state.get("user_prompt", "").strip():
        return {
            "error": "Empty user prompt",
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, "empty prompt")],
        }

    manifest = deps.vertical.manifest
    delta = dict(await deps.vertical.initialize_state(request_from_state(state)))

    if state.get("sandbox_id"):  # resumed run: reuse sandbox
        sandbox_id = state["sandbox_id"]
    else:
        sandbox_id = await deps.sandbox.create(sandbox_spec_for(deps, delta.pop("sandbox_env", None)))

    vertical_meta = delta.pop("metadata", {}) or {}
    update: dict[str, Any] = {
        "vertical_manifest": manifest.model_dump(),
        "available_skills": {k: v.model_dump() for k, v in deps.vertical.skills.items()},
        "tech_stack_hints": {**manifest.tech_stack, **state.get("tech_stack_hints", {})},
        "workspace_path": deps.settings.sandbox.workspace_path,
        **delta,
        "sandbox_id": sandbox_id,
        "vertical_id": manifest.id,
        "status": RunStatus.PLANNING,
        "updated_at": utcnow(),
        "max_fix_retries": state.get("max_fix_retries") or deps.settings.kernel.default_max_retries,
        "fix_attempt_count": 0,
        "token_usage": state.get("token_usage") or TokenUsage(),
        "metadata": merged_metadata(state, **vertical_meta),
        "error": None,
        "logs": [log(NODE, f"vertical={manifest.id} v{manifest.version} sandbox={sandbox_id}")],
    }
    return update
