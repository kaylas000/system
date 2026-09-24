"""End-to-end kernel runs with a scripted LLM, LocalSandbox and InMemorySaver."""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Any

import pytest

from kernel.config import HITLSection, Settings
from kernel.graph import MissingDependencyError, build_graph
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest
from kernel.runner import pending_interrupts, resume_run, start_run
from kernel.sandbox import LocalSandbox
from kernel.state import InterruptType, RunStatus, TaskStatus

from .fakes import ExplodingGate, FakeLLM, FakeVertical, NoBugGate, Scripted, code, plan

REQUEST = GenerateRequest(prompt="Build a demo", vertical_id="fake")


class Harness:
    def __init__(self, tmp_path: Path, settings: Settings, script: list[Scripted], **vkw: Any) -> None:
        self.llm = FakeLLM(script)
        self.vertical = FakeVertical(**vkw)
        self.sandbox = LocalSandbox(tmp_path / "sandboxes")
        self.settings = settings
        self.graph = build_graph(
            self.vertical, llm=self.llm, sandbox=self.sandbox, settings=settings, checkpointer=memory_checkpointer()
        )
        self.run_id = ""

    async def start(self) -> dict[str, Any]:
        self.run_id, result = await start_run(self.graph, REQUEST, settings=self.settings)
        return result

    async def resume(self, decision: dict[str, Any]) -> dict[str, Any]:
        return await resume_run(self.graph, self.run_id, decision, settings=self.settings)

    async def interrupt(self) -> dict[str, Any]:
        pending = await pending_interrupts(self.graph, self.run_id)
        assert len(pending) == 1, pending
        payload: dict[str, Any] = pending[0]
        return payload


def task_status(result: dict[str, Any]) -> dict[str, TaskStatus]:
    return {t.id: t.status for t in result["task_graph"]}


async def test_happy_path_with_fix_loop(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path,
        settings,
        [
            plan(("t1", []), ("t2", ["t1"])),
            code("src/a.txt", "hello BUG"),  # coder t1 -> gate fails
            code("src/a.txt", "hello fixed"),  # fixer -> gate passes
            code("src/b.txt", "second"),  # coder t2
        ],
    )
    result = await h.start()

    assert result["status"] == RunStatus.COMPLETED, result.get("error")
    assert task_status(result) == {"t1": TaskStatus.COMPLETED, "t2": TaskStatus.COMPLETED}
    assert [t.retry_count for t in result["task_graph"]] == [1, 0]
    assert h.vertical.completed == ["t1", "t2"]
    assert h.vertical.finalized
    assert result["metadata"]["readme"] == "generated"
    # t1 failed + fixed, t2 passed, then the project-level (final) verification in the packager
    assert [r.status.value for r in result["verification_history"]] == ["failed", "passed", "passed", "passed"]
    assert result["final_artifact"].quality_report["final_verification_passed"] is True
    assert set(result["project_files"]) == {"src/a.txt", "src/b.txt"}
    # models routed per settings
    assert [c["model"] for c in h.llm.calls] == ["router/planner", "router/coder", "router/coder", "router/coder"]
    usage = result["token_usage"]
    assert usage.total_tokens == 60 and usage.cost_usd == pytest.approx(0.004)

    artifact = result["final_artifact"]
    assert artifact.project_name == "demo"
    assert artifact.quality_report["tasks_completed"] == 2
    with tarfile.open(artifact.artifact_path) as tar:
        names = set(tar.getnames())
    assert {"./src/a.txt", "./src/b.txt"} <= names
    assert h.sandbox._roots == {}  # packager closed the sandbox


async def test_escalation_then_skip_gate(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path,
        settings,
        [
            plan(("t1", []), ("t2", ["t1"])),
            code("a.txt", "BUG"),
            code("a.txt", "BUG 1"),
            code("a.txt", "BUG 2"),
            code("b.txt", "fine"),
        ],
    )
    result = await h.start()
    assert result["status"] == RunStatus.NEEDS_HUMAN_INPUT
    assert result["interrupt_type"] == InterruptType.GATE_FAILURE
    payload = await h.interrupt()
    assert payload["interrupt_type"] == "gate_failure"
    assert payload["payload"]["task"]["id"] == "t1"
    assert "skip_gate" in payload["actions"]

    result = await h.resume({"action": "skip_gate", "comment": "known issue"})
    assert result["status"] == RunStatus.COMPLETED
    assert task_status(result) == {"t1": TaskStatus.SKIPPED, "t2": TaskStatus.COMPLETED}
    assert result["final_artifact"].quality_report["tasks_skipped"] == 1


async def test_escalation_then_human_edit_fixes(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path,
        settings,
        [
            plan(("t1", [])),
            code("a.txt", "BUG"),
            code("a.txt", "BUG 1"),
            code("a.txt", "BUG 2"),
        ],
    )
    await h.start()
    result = await h.resume(
        {"action": "edit", "edited_data": {"file_changes": [{"path": "a.txt", "content": "clean", "action": "update"}]}}
    )
    assert result["status"] == RunStatus.COMPLETED
    assert task_status(result) == {"t1": TaskStatus.COMPLETED}


async def test_escalation_then_retry_resets_budget(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path,
        settings,
        [
            plan(("t1", [])),
            code("a.txt", "BUG"),
            code("a.txt", "BUG 1"),
            code("a.txt", "BUG 2"),
            code("a.txt", "finally ok"),  # after retry: verifier fails again -> fixer gets a fresh budget
        ],
    )
    await h.start()
    result = await h.resume({"action": "retry"})
    assert result["status"] == RunStatus.COMPLETED
    assert result["task_graph"][0].retry_count == 1


async def test_abort(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path, settings, [plan(("t1", [])), code("a.txt", "BUG"), code("a.txt", "BUG"), code("a.txt", "BUG")]
    )
    await h.start()
    result = await h.resume({"action": "abort"})
    assert result["status"] == RunStatus.FAILED
    assert result.get("final_artifact") is None
    assert h.sandbox._roots == {}
    assert await pending_interrupts(h.graph, h.run_id) == []


async def test_disallowed_action_keeps_waiting(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path, settings, [plan(("t1", [])), code("a.txt", "BUG"), code("a.txt", "BUG"), code("a.txt", "BUG")]
    )
    await h.start()
    result = await h.resume({"action": "approve_everything"})
    assert result["status"] == RunStatus.NEEDS_HUMAN_INPUT
    assert (await h.interrupt())["interrupt_type"] == "gate_failure"


async def test_plan_review_approve_and_reject(tmp_path: Path, settings: Settings) -> None:
    settings = settings.model_copy(update={"hitl": HITLSection(plan_review=True)})
    h = Harness(
        tmp_path,
        settings,
        [
            plan(("t1", [])),
            plan(("t1", []), ("t2", ["t1"])),
            code("a.txt", "one"),
            code("b.txt", "two"),
        ],
    )
    result = await h.start()
    assert result["interrupt_type"] == InterruptType.PLAN_REVIEW
    payload = await h.interrupt()
    assert [t["id"] for t in payload["payload"]["task_graph"]] == ["t1"]

    result = await h.resume({"action": "reject", "comment": "add a second task"})
    assert result["interrupt_type"] == InterruptType.PLAN_REVIEW
    replan_user_msg = h.llm.calls[1]["messages"][1].content
    assert "add a second task" in replan_user_msg

    result = await h.resume({"action": "approve"})
    assert result["status"] == RunStatus.COMPLETED
    assert task_status(result) == {"t1": TaskStatus.COMPLETED, "t2": TaskStatus.COMPLETED}


async def test_plan_review_edit(tmp_path: Path, settings: Settings) -> None:
    settings = settings.model_copy(update={"hitl": HITLSection(plan_review=True)})
    h = Harness(tmp_path, settings, [plan(("t1", []), ("t2", ["t1"])), code("x.txt", "only")])
    await h.start()
    # cyclic edit is rejected, graph keeps waiting
    bad = [
        {"id": "a", "name": "A", "description": "a", "depends_on": ["b"]},
        {"id": "b", "name": "B", "description": "b", "depends_on": ["a"]},
    ]
    result = await h.resume({"action": "edit", "edited_data": {"task_graph": bad}})
    assert result["status"] == RunStatus.NEEDS_HUMAN_INPUT
    good = [{"id": "only", "name": "Only", "description": "single task"}]
    result = await h.resume({"action": "edit", "edited_data": {"task_graph": good}})
    assert result["status"] == RunStatus.COMPLETED
    assert task_status(result) == {"only": TaskStatus.COMPLETED}


async def test_invalid_plan_then_replan(tmp_path: Path, settings: Settings) -> None:
    h = Harness(
        tmp_path,
        settings,
        [
            plan(("a", ["b"]), ("b", ["a"])),  # cycle
            plan(("a", [])),
            code("a.txt", "ok"),
        ],
    )
    result = await h.start()
    assert result["interrupt_type"] == InterruptType.INVALID_PLAN
    assert "Cycle" in result["error"] or "cycle" in result["error"]
    result = await h.resume({"action": "retry"})
    assert result["status"] == RunStatus.COMPLETED


async def test_too_many_tasks_is_invalid_plan(tmp_path: Path, settings: Settings) -> None:
    h = Harness(tmp_path, settings, [plan(("a", []), ("b", []), ("c", []))], max_tasks=2)
    result = await h.start()
    assert result["interrupt_type"] == InterruptType.INVALID_PLAN
    assert "limit is 2" in result["error"]


async def test_node_error_retry(tmp_path: Path, settings: Settings) -> None:
    h = Harness(tmp_path, settings, [plan(("t1", [])), RuntimeError("LLM gateway 502"), code("a.txt", "ok")])
    result = await h.start()
    assert result["interrupt_type"] == InterruptType.NODE_ERROR
    assert result["failed_node"] == "coder"
    assert "LLM gateway 502" in result["error"]
    result = await h.resume({"action": "retry"})
    assert result["status"] == RunStatus.COMPLETED


async def test_unsafe_path_from_llm_is_node_error(tmp_path: Path, settings: Settings) -> None:
    h = Harness(tmp_path, settings, [plan(("t1", [])), code("../../etc/passwd", "pwned")])
    result = await h.start()
    assert result["interrupt_type"] == InterruptType.NODE_ERROR
    assert "Unsafe file path" in result["error"]


async def test_infra_error_in_gate(tmp_path: Path, settings: Settings) -> None:
    gate = ExplodingGate()
    h = Harness(tmp_path, settings, [plan(("t1", [])), code("a.txt", "ok")], gates=[NoBugGate(), gate])
    result = await h.start()
    assert result["interrupt_type"] == InterruptType.INFRA_ERROR
    assert "exploding" in result["error"]
    h.vertical.gates = [NoBugGate()]  # "infrastructure recovered"
    result = await h.resume({"action": "retry"})
    assert result["status"] == RunStatus.COMPLETED


async def test_per_run_dependency_override(tmp_path: Path, settings: Settings) -> None:
    graph = build_graph(checkpointer=memory_checkpointer())  # no builder-level defaults at all
    llm = FakeLLM([plan(("t1", [])), code("a.txt", "ok")])
    _, result = await start_run(
        graph,
        REQUEST,
        settings=settings,
        vertical=FakeVertical(),
        llm_client=llm,
        sandbox=LocalSandbox(tmp_path / "sb"),
    )
    assert result["status"] == RunStatus.COMPLETED
    assert result["final_artifact"].artifact_path.startswith(str(tmp_path))


async def test_missing_dependencies_raise(settings: Settings) -> None:
    graph = build_graph(checkpointer=memory_checkpointer())
    with pytest.raises(MissingDependencyError):
        await start_run(graph, REQUEST, settings=settings)
