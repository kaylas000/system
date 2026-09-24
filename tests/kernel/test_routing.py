from __future__ import annotations

from typing import Any

from kernel.graph import routing
from kernel.state import InterruptType, VerificationGateResult, VerificationGateStatus


def gate(status: VerificationGateStatus) -> VerificationGateResult:
    return VerificationGateResult(gate_id="g", name="g", status=status, command="c", exit_code=0, duration_ms=1)


def s(**kw: Any) -> Any:
    return kw


def test_error_always_goes_to_human() -> None:
    err = s(error="boom", current_task_id="t", current_gate_results=[gate(VerificationGateStatus.PASSED)])
    for router in (
        routing.route_after_initialize,
        routing.route_after_planner,
        routing.route_get_next_task,
        routing.route_after_coder,
        routing.route_after_verifier,
        routing.route_after_fixer,
        routing.route_after_documenter,
        routing.route_after_packager,
    ):
        assert router(err) == "human_review", router.__name__


def test_happy_routes() -> None:
    assert routing.route_after_initialize(s()) == "planner"
    assert routing.route_after_planner(s()) == "get_next_task"
    assert routing.route_after_planner(s(interrupt_type=InterruptType.PLAN_REVIEW)) == "human_review"
    assert routing.route_get_next_task(s(current_task_id="t1")) == "coder"
    assert routing.route_get_next_task(s(current_task_id=None)) == "documenter"
    assert routing.route_after_coder(s()) == "verifier"
    assert routing.route_after_fixer(s()) == "verifier"
    assert routing.route_after_documenter(s()) == "packager"
    assert routing.route_after_packager(s()) == "__end__"


def test_route_after_verifier() -> None:
    passed = [gate(VerificationGateStatus.PASSED), gate(VerificationGateStatus.SKIPPED)]
    failed = [gate(VerificationGateStatus.PASSED), gate(VerificationGateStatus.FAILED)]
    assert routing.route_after_verifier(s(current_task_id="t", current_gate_results=passed)) == "get_next_task"
    assert routing.route_after_verifier(s(current_task_id="t", current_gate_results=failed)) == "fixer"
    assert routing.route_after_verifier(s(current_task_id=None, current_gate_results=passed)) == "human_review"


def test_route_after_human_review() -> None:
    assert routing.route_after_human_review(s()) == "__end__"
    for target in ("planner", "verifier", "get_next_task", "human_review", "coder"):
        assert routing.route_after_human_review(s(next_after_human=target)) == target
    assert routing.route_after_human_review(s(next_after_human="rm -rf")) == "human_review"


def test_routers_do_not_mutate_state() -> None:
    state = s(error="x", current_task_id="t", current_gate_results=[gate(VerificationGateStatus.FAILED)])
    snapshot = dict(state)
    routing.route_after_verifier(state)
    routing.route_get_next_task(state)
    assert state == snapshot
