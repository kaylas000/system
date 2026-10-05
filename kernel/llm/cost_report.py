"""
Cost reports (``specs/06_ops/cost_control/COST_REPORTER.py`` — no content in the spec; written by
the agent). Aggregates the ``UsageStore`` of the budget manager by day, model, vertical and run.

    autogen-costs --days 7            # markdown table
    autogen-costs --days 30 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from typing import Any

from .budget import SqliteUsageStore, UsageRecord, UsageStore, day_range


def _bucket() -> dict[str, Any]:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}


def _add(b: dict[str, Any], r: UsageRecord) -> None:
    b["calls"] += 1
    b["prompt_tokens"] += r.prompt_tokens
    b["completion_tokens"] += r.completion_tokens
    b["total_tokens"] += r.total_tokens
    b["cost_usd"] = round(b["cost_usd"] + r.cost_usd, 6)


def aggregate(records: list[UsageRecord], top_runs: int = 10) -> dict[str, Any]:
    total = _bucket()
    groups: dict[str, dict[str, dict[str, Any]]] = {
        k: defaultdict(_bucket) for k in ("days", "models", "verticals", "runs")
    }
    for r in records:
        _add(total, r)
        _add(groups["days"][r.day], r)
        _add(groups["models"][r.model or "unknown"], r)
        _add(groups["verticals"][r.vertical or "unknown"], r)
        _add(groups["runs"][r.run_id or "(no run)"], r)
    runs = sorted(groups["runs"].items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)[:top_runs]
    return {
        "total": total,
        "days": dict(sorted(groups["days"].items())),
        "models": dict(sorted(groups["models"].items(), key=lambda kv: -kv[1]["cost_usd"])),
        "verticals": dict(sorted(groups["verticals"].items(), key=lambda kv: -kv[1]["cost_usd"])),
        "top_runs": dict(runs),
    }


async def build_report(store: UsageStore, days: int = 7) -> dict[str, Any]:
    since, until = day_range(days)
    report = aggregate(await store.records(since, until))
    report["period"] = {"from": since, "to": until}
    return report


def to_markdown(report: dict[str, Any]) -> str:
    p = report.get("period", {})
    t = report["total"]
    lines = [
        f"# LLM costs {p.get('from', '')} .. {p.get('to', '')}",
        "",
        f"**Total:** ${t['cost_usd']:.4f} — {t['calls']} calls, {t['total_tokens']:,} tokens",
    ]
    for key, title in (
        ("days", "By day"),
        ("models", "By model"),
        ("verticals", "By vertical"),
        ("top_runs", "Top runs"),
    ):
        rows = report.get(key) or {}
        if not rows:
            continue
        lines += ["", f"## {title}", "", "| | calls | tokens | cost, $ |", "|---|---:|---:|---:|"]
        lines += [f"| {k} | {v['calls']} | {v['total_tokens']:,} | {v['cost_usd']:.4f} |" for k, v in rows.items()]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from ..config import get_settings

    ap = argparse.ArgumentParser(prog="autogen-costs", description="LLM cost report from the budget usage store")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=None, help="usage SQLite (default: AUTOGEN_BUDGET__USAGE_DB_PATH)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    path = a.db or get_settings().budget.usage_db_path
    if not path:
        print("no usage store configured (AUTOGEN_BUDGET__USAGE_DB_PATH)", file=sys.stderr)
        return 1
    store = SqliteUsageStore(path)
    try:
        report = asyncio.run(build_report(store, a.days))
    finally:
        store.close()
    print(json.dumps(report, indent=2) if a.json else to_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
