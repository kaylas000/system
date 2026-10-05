from __future__ import annotations

import pytest
from pydantic import ValidationError

from kernel.persistence.serde import kernel_serde
from kernel.state import (
    FileChange,
    Task,
    TaskStatus,
    TokenUsage,
    VerificationGateResult,
    VerificationGateStatus,
    find_runnable_task,
    get_current_task,
    update_task_in_graph,
    validate_dag,
)


def t(tid: str, *deps: str, status: TaskStatus = TaskStatus.PENDING) -> Task:
    return Task(id=tid, name=tid, description=tid, depends_on=list(deps), status=status)


def test_token_usage_defaults_and_add() -> None:
    a = TokenUsage()  # spec version required model_name (ISSUES K-01)
    b = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, cost_usd=0.1, model_name="gpt")
    c = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.2, model_name="claude")
    total = a.add(b).add(c).add(b)
    assert (total.prompt_tokens, total.completion_tokens, total.total_tokens) == (3, 5, 8)
    assert total.cost_usd == pytest.approx(0.4)
    assert total.model_name == "claude+gpt"


def test_models_are_frozen() -> None:
    usage = TokenUsage()
    with pytest.raises(ValidationError):
        usage.prompt_tokens = 5  # type: ignore[misc]


def test_checkpoint_serde_roundtrip() -> None:
    serde = kernel_serde()
    task = t("a").model_copy(
        update={
            "file_changes": [FileChange(path="x.py", content="print(1)", action="create")],
            "verification_results": [
                VerificationGateResult(
                    gate_id="lint",
                    name="Lint",
                    status=VerificationGateStatus.FAILED,
                    command="ruff",
                    exit_code=1,
                    duration_ms=5,
                    files_to_fix=["x.py"],
                )
            ],
        }
    )
    value = {"task_graph": [task], "status": TaskStatus.COMPLETED, "usage": TokenUsage(total_tokens=3)}
    restored = serde.loads_typed(serde.dumps_typed(value))
    assert restored == value
    assert isinstance(restored["task_graph"][0].verification_results[0].status, VerificationGateStatus)


@pytest.mark.parametrize(
    ("tasks", "expected"),
    [
        ([t("a"), t("b", "a"), t("c", "a", "b")], None),
        ([], None),
        ([t("a"), t("a")], "Duplicate"),
        ([t("a", "zzz")], "unknown"),
        ([t("a", "a")], "itself"),
        ([t("a", "c"), t("b", "a"), t("c", "b"), t("d")], "Cycle"),
    ],
)
def test_validate_dag(tasks: list[Task], expected: str | None) -> None:
    result = validate_dag(tasks)
    if expected is None:
        assert result is None
    else:
        assert result is not None and expected in result


def test_find_runnable_task_respects_dependencies() -> None:
    tasks = [t("a", status=TaskStatus.COMPLETED), t("b", "a", "c"), t("c", status=TaskStatus.SKIPPED), t("d", "b")]
    runnable = find_runnable_task(tasks)
    assert runnable is not None and runnable.id == "b"
    assert find_runnable_task([t("x", "y"), t("y", status=TaskStatus.VERIFICATION_FAILED)]) is None
    assert find_runnable_task([t("a", status=TaskStatus.COMPLETED)]) is None


def test_update_task_is_immutable() -> None:
    state = {"task_graph": [t("a"), t("b")], "current_task_id": "b"}
    current = get_current_task(state)  # type: ignore[arg-type]
    assert current is not None
    new_graph = update_task_in_graph(state, current.model_copy(update={"status": TaskStatus.COMPLETED}))  # type: ignore[arg-type]
    assert [x.status for x in new_graph] == [TaskStatus.PENDING, TaskStatus.COMPLETED]
    assert state["task_graph"][1].status == TaskStatus.PENDING
