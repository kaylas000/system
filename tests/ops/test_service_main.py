from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from kernel import main as kernel_main
from kernel.config import BudgetSection, KernelSection, SandboxSection, Settings

ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kernel=KernelSection(verticals_dir=str(ROOT / "verticals")),
        sandbox=SandboxSection(provider="local"),
        budget=BudgetSection(usage_db_path=str(tmp_path / "db" / "usage.sqlite"), max_cost_usd_per_day=5.0),
    )


def test_build_app_serves_health_and_metrics(tmp_path: Path) -> None:
    app = kernel_main.build_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "active_runs": 0}
        assert client.get("/metrics").status_code == 200
        assert client.get("/runs/unknown").status_code == 404
        manager = app.state.run_manager
        assert manager.resolve_vertical("").manifest.id == "saas_web"
    assert (tmp_path / "db" / "usage.sqlite").exists()


def test_check_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AUTOGEN_KERNEL__VERTICALS_DIR", str(ROOT / "verticals"))
    monkeypatch.setenv("AUTOGEN_LLM__API_KEY", "sk-secret")
    from kernel.config import get_settings

    get_settings.cache_clear()
    try:
        assert kernel_main.main(["check"]) == 0
    finally:
        get_settings.cache_clear()
    out = json.loads(capsys.readouterr().out)
    assert "saas_web" in out["verticals"]
    assert "sk-secret" not in json.dumps(out)
