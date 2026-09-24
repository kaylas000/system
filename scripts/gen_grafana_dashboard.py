"""
Generate the Grafana dashboard for the kernel metrics (written by the agent; replaces
specs/06_ops/observability/GRAFANA_DASHBOARDS.json whose queries use metric names that do not exist —
ISSUES O-xx). Every PromQL metric is checked against ``kernel.observability.metrics`` in tests.

    python scripts/gen_grafana_dashboard.py > deploy/grafana/dashboards/autogen-ops.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

DS = {"type": "prometheus", "uid": "${datasource}"}


def panel(
    pid: int,
    title: str,
    exprs: list[tuple[str, str]],
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    kind: str = "timeseries",
    unit: str | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": pid,
        "title": title,
        "type": kind,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": chr(ord("A") + i), "datasource": DS, "expr": e, "legendFormat": legend}
            for i, (e, legend) in enumerate(exprs)
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
    return p


def dashboard() -> dict[str, Any]:
    v = '{vertical=~"$vertical"}'
    panels = [
        panel(1, "Active runs", [(f"sum by (vertical) (autogen_runs_active{v})", "{{vertical}}")], 0, 0, 8, 6, "stat"),
        panel(2, "Queued runs", [("autogen_queue_depth", "queued")], 8, 0, 4, 6, "stat"),
        panel(
            3,
            "Run success rate (1h)",
            [
                (
                    f'sum(increase(autogen_runs_total{{status="completed",vertical=~"$vertical"}}[1h])) '
                    f"/ clamp_min(sum(increase(autogen_runs_total{v}[1h])), 1)",
                    "success",
                )
            ],
            12,
            0,
            6,
            6,
            "stat",
            "percentunit",
        ),
        panel(
            4,
            "LLM spend today",
            [("sum(increase(autogen_llm_cost_usd_total[24h]))", "USD")],
            18,
            0,
            6,
            6,
            "stat",
            "currencyUSD",
        ),
        panel(
            5,
            "Run duration p50 / p95",
            [
                (
                    f"histogram_quantile(0.5, sum by (le) (rate(autogen_run_duration_seconds_bucket{v}[$__rate_interval])))",
                    "p50",
                ),
                (
                    f"histogram_quantile(0.95, sum by (le) (rate(autogen_run_duration_seconds_bucket{v}[$__rate_interval])))",
                    "p95",
                ),
            ],
            0,
            6,
            12,
            8,
            unit="s",
        ),
        panel(
            6,
            "Finished runs by status",
            [(f"sum by (status) (increase(autogen_runs_total{v}[$__rate_interval]))", "{{status}}")],
            12,
            6,
            12,
            8,
        ),
        panel(
            7,
            "Node latency p95",
            [
                (
                    f"histogram_quantile(0.95, sum by (le, node) (rate(autogen_node_duration_seconds_bucket{v}[$__rate_interval])))",
                    "{{node}}",
                )
            ],
            0,
            14,
            12,
            8,
            unit="s",
        ),
        panel(
            8,
            "Node errors / interrupts",
            [
                (f"sum by (node) (increase(autogen_node_errors_total{v}[$__rate_interval]))", "error {{node}}"),
                (f"sum by (type) (increase(autogen_interrupts_total{v}[$__rate_interval]))", "interrupt {{type}}"),
            ],
            12,
            14,
            12,
            8,
        ),
        panel(
            9,
            "LLM tokens / min by model",
            [("sum by (model, type) (rate(autogen_llm_tokens_total[$__rate_interval])) * 60", "{{model}} {{type}}")],
            0,
            22,
            12,
            8,
        ),
        panel(
            10,
            "LLM cost / hour by model",
            [("sum by (model) (rate(autogen_llm_cost_usd_total[$__rate_interval])) * 3600", "{{model}}")],
            12,
            22,
            12,
            8,
            unit="currencyUSD",
        ),
        panel(
            11,
            "LLM latency p95 / error rate",
            [
                (
                    "histogram_quantile(0.95, sum by (le, model) (rate(autogen_llm_latency_seconds_bucket[$__rate_interval])))",
                    "p95 {{model}}",
                ),
                (
                    'sum by (model) (rate(autogen_llm_requests_total{status="error"}[$__rate_interval]))',
                    "errors/s {{model}}",
                ),
            ],
            0,
            30,
            12,
            8,
        ),
        panel(
            12,
            "Gate results (1h)",
            [(f"sum by (gate, status) (increase(autogen_gate_results_total{v}[1h]))", "{{gate}} {{status}}")],
            12,
            30,
            12,
            8,
            "bargauge",
        ),
        panel(
            13,
            "Sandbox exec p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le, provider) (rate(autogen_sandbox_exec_duration_seconds_bucket[$__rate_interval])))",
                    "{{provider}}",
                )
            ],
            0,
            38,
            12,
            8,
            unit="s",
        ),
        panel(
            14,
            "Budget limit hits",
            [("sum by (scope) (increase(autogen_budget_exceeded_total[$__rate_interval]))", "{{scope}}")],
            12,
            38,
            12,
            8,
        ),
    ]
    return {
        "uid": "autogen-ops",
        "title": "AutoGen Platform — Operations",
        "tags": ["autogen", "llm", "agents"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {"name": "datasource", "type": "datasource", "query": "prometheus", "current": {}},
                {
                    "name": "vertical",
                    "type": "query",
                    "datasource": DS,
                    "query": "label_values(autogen_runs_total, vertical)",
                    "includeAll": True,
                    "multi": True,
                    "allValue": ".*",
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                },
            ]
        },
        "panels": panels,
    }


if __name__ == "__main__":
    json.dump(dashboard(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
