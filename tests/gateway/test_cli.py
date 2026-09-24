from __future__ import annotations

import json
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from kernel.config import Settings
from kernel.gateway.cli import Api, cli
from tests.gateway.test_api import Env
from tests.gateway.test_composer import SmartLLM, _plan
from tests.kernel.fakes import code, plan


class Harness:
    def __init__(self, env: Env, client: TestClient, key: str) -> None:
        self.env, self.client, self.key = env, client, key
        self.runner = CliRunner()

    def __call__(self, *args: str, key: str | None = None, input: str | None = None) -> Any:
        api = Api("http://testserver/v1", key or self.key, client=self.client)
        return self.runner.invoke(cli, list(args), obj={"api": api}, input=input, catch_exceptions=False)


@pytest.fixture
def harness_factory(tmp_path: Path, settings: Settings) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(responses: list[Any] | None = None, llm: Any = None, plan_review: bool = False) -> Harness:
        env = Env(tmp_path, settings, responses or [], llm=llm, plan_review=plan_review)
        client = TestClient(env.app)
        client.__enter__()
        clients.append(client)
        key = client.portal.call(env.key)["X-API-Key"]
        return Harness(env, client, key)

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def test_verticals_and_errors(harness_factory: Any) -> None:
    h = harness_factory()
    r = h("verticals")
    assert r.exit_code == 0 and "fake" in r.output and "other" in r.output
    r = h("--json", "verticals")
    assert {v["id"] for v in json.loads(r.output)} == {"fake", "other"}
    r = h("verticals", "show", "fake")
    assert r.exit_code == 0 and "Fake Vertical" in r.output
    r = h("verticals", key="agk_bad_key")
    assert r.exit_code == 1 and "HTTP 401" in r.output
    r = h("status", "run_nope")
    assert r.exit_code == 1 and "HTTP 404" in r.output


def test_generate_watch_download(harness_factory: Any, tmp_path: Path) -> None:
    h = harness_factory([plan(("t1", [])), code("a.txt", "one")])
    ctx = tmp_path / "openapi.yaml"
    ctx.write_text("openapi: 3.1.0\n")
    out = tmp_path / "out"
    r = h("generate", "build it", "-v", "fake", "--hint", "db=postgres", "--context-file", str(ctx), "--watch",
          "--download", str(out))  # fmt: skip
    assert r.exit_code == 0, r.output
    assert "started" in r.output and "completed" in r.output
    run_id = r.output.split("started")[1].split()[0]
    archive = out / f"{run_id}.tar.gz"
    with tarfile.open(archive) as tar:
        assert {"./a.txt", "./openapi.yaml"} <= set(tar.getnames())
    call = next(c for c in h.env.llm.calls if "CONTEXT FILES" in "\n".join(m.content for m in c["messages"]))
    assert call is not None
    r = h("--json", "runs")
    assert [(x["run_id"], x["status"]) for x in json.loads(r.output)] == [(run_id, "completed")]


def test_hitl_commands(harness_factory: Any, tmp_path: Path) -> None:
    h = harness_factory([plan(("t1", [])), code("a.txt", "one")], plan_review=True)
    r = h("--json", "generate", "build it", "-v", "fake")
    run_id = json.loads(r.output)["run_id"]
    h.client.portal.call(h.env.manager.wait, run_id)

    r = h("status", run_id)
    assert "needs_human_input" in r.output and "plan_review" in r.output
    r = h("interrupt", run_id)
    assert "Proposed plan" in r.output and "t1" in r.output and "approve" in r.output
    r = h("approve", run_id, "--action", "skip_gate")
    assert r.exit_code == 1 and "HTTP 400" in r.output
    r = h("approve", run_id, "--action", "approve", "-m", "lgtm")
    assert r.exit_code == 0 and "submitted" in r.output
    h.client.portal.call(h.env.manager.wait, run_id)

    assert "completed" in h("status", run_id).output
    r = h("logs", run_id)
    assert "planner" in r.output
    r = h("logs", run_id, "--follow")
    assert "planner" in r.output and "run stopped: completed" in r.output
    dest = tmp_path / "p.tar.gz"
    assert h("download", run_id, "-o", str(dest)).exit_code == 0 and dest.exists()
    r = h("cancel", run_id)
    assert r.exit_code == 1 and "HTTP 409" in r.output


def test_compose_commands(harness_factory: Any, tmp_path: Path) -> None:
    h = harness_factory(llm=SmartLLM(composition=_plan()))
    saved = tmp_path / "plan.json"
    r = h("compose", "billing suite", "--dry-run", "--save-plan", str(saved))
    assert r.exit_code == 0, r.output
    assert "Billing Suite" in r.output and "shared/openapi.yaml" in r.output and saved.exists()

    out = tmp_path / "dl"
    r = h("compose", "--plan", str(saved), "--watch", "--download", str(out))
    assert r.exit_code == 0, r.output
    assert "composition started" in r.output and "infra" in r.output and "completed" in r.output
    (archive,) = out.glob("cmp_*.tar.gz")
    with tarfile.open(archive) as tar:
        assert "billing-suite/README.md" in tar.getnames()
    cid = archive.name.removesuffix(".tar.gz")
    r = h("composition", "status", cid)
    assert "api" in r.output and "infra" in r.output and "completed" in r.output
    r = h("--json", "runs", "--parent", cid)
    assert len(json.loads(r.output)) == 3
