"""
Single Source of Truth for the Agentic Kernel State.

Implements ``specs/01_kernel/state.py``. Deviations from the spec are listed
in ``specs/ISSUES.md`` (K-01 … K-05):

* ``TokenUsage.model_name`` has a default (spec: required, but ``TokenUsage()``
  was used without it).
* ``AgentState`` gains ``sandbox_id`` (required by tools / verticals), and the
  HITL bookkeeping fields ``interrupt_type``, ``human_decision``, ``next_after_human``.
* ``datetime`` values are timezone-aware (UTC).
"""

from __future__ import annotations

import operator
import uuid
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# ENUMS & LITERALS
# =============================================================================


class RunStatus(StrEnum):
    """High-level lifecycle status."""

    QUEUED = "queued"
    INITIALIZING = "initializing"
    PLANNING = "planning"
    CODING = "coding"
    VERIFYING = "verifying"
    FIXING = "fixing"
    DOCUMENTING = "documenting"
    PACKAGING = "packaging"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN_INPUT = "needs_human_input"  # HITL Interrupt


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VERIFICATION_FAILED = "verification_failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class VerificationGateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"  # Infrastructure error (sandbox down)


class NodeName(StrEnum):
    """Names must match LangGraph node names exactly."""

    INITIALIZE = "initialize"
    PLANNER = "planner"
    GET_NEXT_TASK = "get_next_task"
    CODER = "coder"
    VERIFIER = "verifier"
    FIXER = "fixer"
    DOCUMENTER = "documenter"
    PACKAGER = "packager"
    HUMAN_REVIEW = "human_review"  # Interrupt node


class InterruptType(StrEnum):
    """Why the graph stopped at ``human_review``."""

    PLAN_REVIEW = "plan_review"
    INVALID_PLAN = "invalid_plan"
    GATE_FAILURE = "gate_failure"
    INFRA_ERROR = "infra_error"
    NODE_ERROR = "node_error"
    DESTRUCTIVE_ACTION = "destructive_action"
    BUDGET_EXCEEDED = "budget_exceeded"


HumanAction = Literal["approve", "reject", "edit", "abort", "skip_gate", "retry"]

# =============================================================================
# NESTED DATA MODELS (Pydantic for validation/serialization)
# =============================================================================


class TokenUsage(BaseModel):
    """Tracked per LLM call, aggregated in state."""

    model_config = ConfigDict(frozen=True)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model_name: str = ""

    def add(self, other: TokenUsage) -> TokenUsage:
        names = sorted({n for n in (*self.model_name.split("+"), *other.model_name.split("+")) if n})
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            model_name="+".join(names),
        )


class FileChange(BaseModel):
    """Atomic file operation for sandbox application."""

    model_config = ConfigDict(frozen=True)
    path: str = Field(..., description="Relative path in workspace")
    content: str = ""
    action: Literal["create", "update", "delete"]
    # Optional: unified diff for human review / git commit
    diff: str | None = None


class VerificationGateResult(BaseModel):
    """Result of a single verification gate (lint, test, build, etc)."""

    model_config = ConfigDict(frozen=True)
    gate_id: str = Field(..., description="Unique ID from VerticalManifest (e.g. 'lint_ts', 'pytest_unit')")
    name: str
    status: VerificationGateStatus
    command: str = Field(..., description="Command executed in sandbox")
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int
    # Files that need fixing (extracted from stderr by parser)
    files_to_fix: list[str] = Field(default_factory=list)
    # Parsed structured issues (e.g. from sarif/json output)
    issues: list[dict[str, Any]] = Field(default_factory=list)


class Task(BaseModel):
    """Atomic unit of work from the Planner."""

    model_config = ConfigDict(frozen=True)
    id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    name: str
    description: str = Field(..., description="Human readable, used in prompts")
    # Reference to Skill ID in VerticalManifest. If None -> LLM Freeform Coding.
    skill_id: str | None = None
    # Input parameters for the skill / context for the coder
    inputs: dict[str, Any] = Field(default_factory=dict)
    # Dependencies: Task IDs that must be COMPLETED before this starts
    depends_on: list[str] = Field(default_factory=list)
    # Status tracked during execution
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent: NodeName | None = None
    # Result artifacts
    file_changes: list[FileChange] = Field(default_factory=list)
    verification_results: list[VerificationGateResult] = Field(default_factory=list)
    retry_count: int = 0
    error_summary: str | None = None


class ArtifactMetadata(BaseModel):
    """Final output metadata."""

    model_config = ConfigDict(frozen=True)
    project_name: str
    vertical_id: str
    tech_stack: dict[str, str]
    git_commit_sha: str | None = None
    artifact_path: str = Field(..., description="Path to zip/tar.gz in Object Storage")
    quality_report: dict[str, Any] = Field(default_factory=dict)
    total_token_usage: TokenUsage
    duration_seconds: float


# =============================================================================
# MAIN STATE (TypedDict for LangGraph Compatibility)
# =============================================================================


class AgentState(TypedDict, total=False):
    """
    The Immutable State Object.

    Nodes return partial updates. Lists annotated with ``operator.add`` are
    appended to, ``project_files`` is merged with ``operator.or_``; every other
    key is replaced as a whole.
    """

    # --- Run Identity ---
    run_id: str
    thread_id: str  # LangGraph checkpoint thread_id
    status: RunStatus
    created_at: datetime
    updated_at: datetime

    # --- Input ---
    user_prompt: str
    vertical_id: str
    tech_stack_hints: dict[str, str]
    constraints: list[str]
    context_files: list[dict[str, str]]  # [{name, content_base64}]
    max_budget_usd: float | None

    # --- Vertical Context (Injected at INIT) ---
    vertical_manifest: dict[str, Any]  # Parsed Manifest.yaml (validated separately)
    available_skills: dict[str, Any]  # {skill_id: skill_def}
    skill_outputs: dict[str, dict[str, Any]]  # {skill_id: outputs} of executed skills (templates: skill_output())

    # --- Planning Phase ---
    spec_markdown: str  # Detailed SPEC.md generated by Planner
    architecture_markdown: str  # ARCHITECTURE.md
    task_graph: list[Task]  # The Plan (DAG flattened to list with depends_on)
    current_task_id: str | None

    # --- Execution Context ---
    sandbox_id: str  # [ISSUES K-02] used by ToolRegistry / verticals
    workspace_path: str  # Absolute path in Sandbox (e.g. /workspace/project)
    project_files: Annotated[dict[str, str], operator.or_]  # Virtual FS cache: {rel_path: content_hash}

    # --- Verification & Fix Loop ---
    verification_history: Annotated[list[VerificationGateResult], operator.add]
    current_gate_results: list[VerificationGateResult]  # Results of last verifier run
    fix_attempt_count: int
    max_fix_retries: int

    # --- Output ---
    final_artifact: ArtifactMetadata | None

    # --- Observability & Control ---
    token_usage: TokenUsage
    logs: Annotated[list[str], operator.add]  # Structured log lines for UI streaming
    human_interrupt_reason: str | None  # Set if HITL triggered
    error: str | None  # Fatal error message

    # --- HITL bookkeeping [ISSUES K-03] ---
    interrupt_type: InterruptType | None
    human_decision: dict[str, Any] | None
    next_after_human: str | None
    failed_node: str | None  # Node that raised (for "retry" after NODE_ERROR)

    # --- Extensibility (Vertical-specific data) ---
    metadata: dict[str, Any]  # Verticals can store anything here (e.g. db_schema, openapi_spec)


# =============================================================================
# HELPER FUNCTIONS (Pure, for Node Logic)
# =============================================================================


def utcnow() -> datetime:
    return datetime.now(UTC)


def get_current_task(state: AgentState) -> Task | None:
    """Safely extract current task object."""
    task_id = state.get("current_task_id")
    if not task_id:
        return None
    for task in state.get("task_graph", []):
        if task.id == task_id:
            return task
    return None


def update_task_in_graph(state: AgentState, updated_task: Task) -> list[Task]:
    """Return new task_graph with updated task (immutability)."""
    return [updated_task if t.id == updated_task.id else t for t in state.get("task_graph", [])]


def get_pending_tasks(state: AgentState) -> list[Task]:
    return [t for t in state.get("task_graph", []) if t.status == TaskStatus.PENDING]


def get_failed_tasks(state: AgentState) -> list[Task]:
    return [t for t in state.get("task_graph", []) if t.status == TaskStatus.VERIFICATION_FAILED]


def is_plan_complete(state: AgentState) -> bool:
    tasks = state.get("task_graph", [])
    return all(t.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED) for t in tasks)


def calculate_total_tokens(state: AgentState) -> TokenUsage:
    return state.get("token_usage", TokenUsage())


def find_runnable_task(tasks: list[Task]) -> Task | None:
    """First PENDING task whose dependencies are all COMPLETED or SKIPPED."""
    done = {t.id for t in tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)}
    for task in tasks:
        if task.status == TaskStatus.PENDING and all(dep in done for dep in task.depends_on):
            return task
    return None


def validate_dag(tasks: list[Task]) -> str | None:
    """
    Validate the task graph with Kahn's algorithm.

    Returns ``None`` if valid, otherwise a human-readable error
    (duplicate IDs, unknown dependencies, self-dependencies or cycles).
    """
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        return f"Duplicate task ids: {dupes}"
    known = set(ids)
    for t in tasks:
        missing = [d for d in t.depends_on if d not in known]
        if missing:
            return f"Task {t.id} depends on unknown tasks: {missing}"
        if t.id in t.depends_on:
            return f"Task {t.id} depends on itself"

    indegree = {t.id: len(set(t.depends_on)) for t in tasks}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for dep in set(t.depends_on):
            dependents[dep].append(t.id)
    queue = deque(i for i, d in indegree.items() if d == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for nxt in dependents[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(tasks):
        cyclic = sorted(i for i, d in indegree.items() if d > 0)
        return f"Cycle detected between tasks: {cyclic}"
    return None
