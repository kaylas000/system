"""
Validate a LiteLLM proxy config without API keys or Redis (written by the agent):

* YAML structure: only known top-level sections, every model has ``model_name`` + ``litellm_params.model``;
* every model group used by the platform exists; fallbacks point to existing groups;
* ``routing_strategy`` is one of LiteLLM's strategies;
* ``litellm.Router`` accepts the model list and fallbacks (env references replaced by dummies);
* no plaintext secrets (``api_key`` / ``master_key`` must be ``os.environ/...``).

    python scripts/check_litellm_config.py deploy/litellm/config.yaml
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED_GROUPS = ["router/planner", "router/coder", "router/enricher", "router/reranker", "router/classifier"]
TOP_LEVEL = {"model_list", "router_settings", "litellm_settings", "general_settings", "environment_variables"}
STRATEGIES = {
    "simple-shuffle",
    "least-busy",
    "usage-based-routing",
    "usage-based-routing-v2",
    "latency-based-routing",
    "cost-based-routing",
}
SECRET_KEYS = {"api_key", "master_key", "password", "redis_password", "aws_secret_access_key"}


def _walk_secrets(node: Any, path: str, errors: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if k in SECRET_KEYS and isinstance(v, str) and not v.startswith("os.environ/"):
                errors.append(f"{p}: plaintext secret (use os.environ/VAR)")
            _walk_secrets(v, p, errors)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_secrets(v, f"{path}[{i}]", errors)


def _dummy_env(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _dummy_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_dummy_env(v) for v in node]
    if isinstance(node, str) and node.startswith("os.environ/"):
        return "dummy"
    return node


def check(path: Path, router_out: list[Any] | None = None) -> list[str]:
    errors: list[str] = []
    cfg = yaml.safe_load(path.read_text())
    if not isinstance(cfg, dict):
        return ["config is not a mapping"]
    for key in cfg:
        if key not in TOP_LEVEL:
            errors.append(f"unknown top-level section {key!r} (LiteLLM ignores it)")
    models = cfg.get("model_list") or []
    groups: set[str] = set()
    for i, m in enumerate(models):
        if not m.get("model_name") or not (m.get("litellm_params") or {}).get("model"):
            errors.append(f"model_list[{i}]: model_name and litellm_params.model are required")
            continue
        groups.add(m["model_name"])
    errors += [f"missing model group {g!r}" for g in REQUIRED_GROUPS if g not in groups]
    rs = cfg.get("router_settings") or {}
    if "strategy" in rs:
        errors.append("router_settings.strategy is not a LiteLLM key (routing_strategy)")
    strategy = rs.get("routing_strategy", "simple-shuffle")
    if strategy not in STRATEGIES:
        errors.append(f"router_settings.routing_strategy {strategy!r} is not a LiteLLM strategy {sorted(STRATEGIES)}")
    for kind in ("fallbacks", "context_window_fallbacks"):
        for entry in rs.get(kind) or []:
            for src, dests in entry.items():
                for g in [src, *dests]:
                    if g not in groups:
                        errors.append(f"router_settings.{kind}: unknown group {g!r}")
    _walk_secrets(cfg, "", errors)
    if errors:
        return errors

    import litellm

    router = litellm.Router(
        model_list=_dummy_env(copy.deepcopy(models)),
        fallbacks=rs.get("fallbacks") or [],
        context_window_fallbacks=rs.get("context_window_fallbacks") or [],
        routing_strategy=strategy,
        num_retries=rs.get("num_retries", 0),
    )
    if router_out is not None:
        router_out.append(router)
    missing = [g for g in REQUIRED_GROUPS if g not in router.get_model_names()]
    errors += [f"router does not expose {g!r}" for g in missing]
    return errors


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "deploy/litellm/config.yaml")
    errors = check(path)
    for e in errors:
        print(f"ERROR {e}")
    print(f"{path}: {'OK' if not errors else f'{len(errors)} error(s)'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
