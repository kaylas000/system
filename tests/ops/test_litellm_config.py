from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("litellm")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_litellm_config", ROOT / "scripts" / "check_litellm_config.py")
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_deploy_config_is_valid_and_routes() -> None:
    routers: list[Any] = []
    assert checker.check(ROOT / "deploy/litellm/config.yaml", routers) == []
    router = routers[0]
    assert {"router/planner", "router/coder", "text-embedding-3-small"} <= set(router.get_model_names())


async def test_router_mock_completion_through_group() -> None:
    routers: list[Any] = []
    checker.check(ROOT / "deploy/litellm/config.yaml", routers)
    resp = await routers[0].acompletion(
        model="router/coder", messages=[{"role": "user", "content": "hi"}], mock_response="ok"
    )
    assert resp.choices[0].message.content == "ok"


def test_spec_config_defects_are_detected() -> None:
    errors = checker.check(ROOT / "specs/06_ops/cost_control/MODEL_ROUTER.yaml")
    joined = "\n".join(errors)
    assert "'general'" in joined and "'cache'" in joined and "strategy" in joined
    assert "plaintext secret" in joined
