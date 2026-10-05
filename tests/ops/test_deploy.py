"""Static checks of deployment artifacts (no Docker / Kubernetes / Terraform registry needed)."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from kernel.config import Settings
from kernel.observability import metrics

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def metric_names() -> set[str]:
    pytest.importorskip("prometheus_client")
    return {str(obj._name) for obj in vars(metrics).values() if hasattr(obj, "_name")}  # counters: without _total


def settings_env_ok(name: str) -> bool:
    """AUTOGEN_<SECTION>__<FIELD> must map to a real Settings field."""
    if not name.startswith("AUTOGEN_"):
        return True
    section, _, field = name.removeprefix("AUTOGEN_").lower().partition("__")
    info = Settings.model_fields.get(section)
    if info is None or info.annotation is None:
        return False
    return field in info.annotation.model_fields  # type: ignore[union-attr]


def _base_metric(name: str) -> str:
    return re.sub(r"_(bucket|count|sum|created|total)$", "", name)


def promql_metrics(expr: str) -> set[str]:
    return {_base_metric(m) for m in re.findall(r"\b(autogen_[a-z_]+)", expr)}


# --- docker compose ---------------------------------------------------------------------------
def _compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def test_compose_matches_compose_spec_schema() -> None:
    schema = json.loads((Path(__file__).parent / "schemas" / "compose-spec.json").read_text())
    errors = [e.message for e in jsonschema.Draft7Validator(schema).iter_errors(_compose())]
    assert errors == []


def test_compose_wiring() -> None:
    doc = _compose()
    services = doc["services"]
    kernel_env = services["kernel"]["environment"]
    assert all(settings_env_ok(k) for k in kernel_env), [k for k in kernel_env if not settings_env_ok(k)]
    for svc in services.values():
        for dep in svc.get("depends_on", {}):
            assert dep in services
        for vol in svc.get("volumes", []):
            src = vol.split(":")[0]
            if src.startswith("./"):
                assert (DEPLOY / src).exists(), src
            else:
                assert src in doc["volumes"], src
    env_example = (DEPLOY / ".env.example").read_text()
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", (DEPLOY / "docker-compose.yml").read_text()))
    assert required and all(f"{v}=" in env_example for v in required)


# --- Dockerfiles ------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["kernel", "knowledge"])
def test_dockerfiles_reference_existing_lock(name: str) -> None:
    text = (DEPLOY / "docker" / f"Dockerfile.{name}").read_text()
    assert f"deploy/docker/requirements-{name}.lock" in text
    lock = (DEPLOY / "docker" / f"requirements-{name}.lock").read_text()
    assert re.search(r"^litellm==", lock, re.M) and re.search(r"^qdrant-client==", lock, re.M)
    assert "USER 10001" in text
    for env in re.findall(r"(AUTOGEN_[A-Z_]+)=", text):
        assert settings_env_ok(env), env
    if name == "kernel":
        assert re.search(r"^fastapi==", lock, re.M) and 'CMD ["python", "-m", "kernel.main", "serve"' in text
    ignore = (ROOT / ".dockerignore").read_text()
    assert "!deploy/docker/requirements-*.lock" in ignore


# --- Helm -------------------------------------------------------------------------------------
CHART = DEPLOY / "helm" / "autogen-kernel"


def test_helm_values_env_names() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert all(settings_env_ok(k) for k in values["config"])
    assert values["replicaCount"] == 1  # in-memory run manager (ISSUES O-15)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_lint_and_template() -> None:
    subprocess.run(["helm", "lint", str(CHART), "--strict"], check=True, capture_output=True)
    out = subprocess.run(
        [
            "helm",
            "template",
            "t",
            str(CHART),
            "--set",
            "ingress.enabled=true",
            "--set",
            "pdb.enabled=true",
            "--set",
            "autoscaling.enabled=true",
            "--set",
            "secrets.AUTOGEN_LLM__API_KEY=x",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    kinds = {d["kind"] for d in yaml.safe_load_all(out) if d}
    assert {
        "Deployment",
        "Service",
        "ConfigMap",
        "Secret",
        "Ingress",
        "PodDisruptionBudget",
        "HorizontalPodAutoscaler",
        "NetworkPolicy",
        "PersistentVolumeClaim",
        "ServiceAccount",
    } <= kinds


# --- Terraform --------------------------------------------------------------------------------
TF = DEPLOY / "terraform" / "aws"


def _tf_text() -> str:
    text = "\n".join(p.read_text() for p in sorted(TF.glob("*.tf")))
    return re.sub(r"(?m)^\s*#.*$|\s#\s.*$", "", text)  # drop comments


def test_terraform_references_are_declared() -> None:
    text = _tf_text()
    declared_vars = set(re.findall(r'^variable "([a-z0-9_]+)"', text, re.M))
    used_vars = set(re.findall(r"\bvar\.([a-z0-9_]+)", text))
    assert used_vars <= declared_vars, used_vars - declared_vars
    modules = set(re.findall(r'^module "([a-z0-9_]+)"', text, re.M))
    assert set(re.findall(r"\bmodule\.([a-z0-9_]+)", text)) <= modules
    resources = set(re.findall(r'^resource "([a-z0-9_]+)" "([a-z0-9_]+)"', text, re.M))
    datas = set(re.findall(r'^data "([a-z0-9_]+)" "([a-z0-9_]+)"', text, re.M))
    for rtype, rname in re.findall(r"(?<!data\.)\b(aws_[a-z0-9_]+|random_[a-z0-9_]+)\.([a-z0-9_]+)\.", text):
        assert (rtype, rname) in resources, (rtype, rname)
    for rtype, rname in re.findall(r"\bdata\.([a-z0-9_]+)\.([a-z0-9_]+)", text):
        assert (rtype, rname) in datas, (rtype, rname)
    # single-line blocks may hold only one argument (spec defect)
    assert not re.search(r"\{[^{}\n]*=[^{}\n]*,[^{}\n]*=[^{}\n]*\}", re.sub(r"=\s*\{[^\n]*\}", "", text))


def test_terraform_parses_with_hcl2() -> None:
    hcl2 = pytest.importorskip("hcl2")
    for path in TF.glob("*.tf"):
        with path.open() as fh:
            assert hcl2.load(fh)


# --- Grafana / Prometheus ---------------------------------------------------------------------
def test_grafana_dashboard_is_generated_and_uses_real_metrics() -> None:
    spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "gen_grafana_dashboard.py")
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    stored = json.loads((DEPLOY / "grafana" / "dashboards" / "autogen-ops.json").read_text())
    assert stored == json.loads(json.dumps(gen.dashboard())), "run scripts/gen_grafana_dashboard.py"
    used = set().union(*(promql_metrics(t["expr"]) for p in stored["panels"] for t in p["targets"]))
    assert used and used <= metric_names(), used - metric_names()


def test_prometheus_alerts_use_real_metrics() -> None:
    rules = yaml.safe_load((DEPLOY / "prometheus" / "alerts.yml").read_text())
    used = set().union(*(promql_metrics(r["expr"]) for g in rules["groups"] for r in g["rules"]))
    assert used and used <= metric_names(), used - metric_names()
