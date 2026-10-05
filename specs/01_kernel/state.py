# specs/01_kernel/state.py
"""
Single Source of Truth for the Agentic Kernel State.
All nodes read/write ONLY this structure.
Serialization: JSON (for Postgres Checkpointer) / MessagePack (for Redis cache).
"""

from __future__ import annotations
import uuid
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Literal, Annotated, Any, Union, TypedDict
from pydantic import BaseModel, Field, ConfigDict
import operator

# =============================================================================
# ENUMS & LITERALS
# =============================================================================


class RunStatus(str, Enum):
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


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VERIFICATION_FAILED = "verification_failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class VerificationGateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"  # Infrastructure error (sandbox down)


class NodeName(str, Enum):
    """Names must match LangGraph node names exactly."""

    PLANNER = "planner"
    GET_NEXT_TASK = "get_next_task"
    CODER = "coder"
    VERIFIER = "verifier"
    FIXER = "fixer"
    DOCUMENTER = "documenter"
    PACKAGER = "packager"
    HUMAN_REVIEW = "human_review"  # Interrupt node


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
    model_name: str

    def add(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            model_name=f"{self.model_name}+{other.model_name}",
        )


class FileChange(BaseModel):
    """Atomic file operation for sandbox application."""

    model_config = ConfigDict(frozen=True)
    path: str = Field(..., description="Relative path in workspace")
    content: str
    action: Literal["create", "update", "delete"]
    # Optional: unified diff for human review / git commit
    diff: Optional[str] = None


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
    files_to_fix: List[str] = Field(default_factory=list)
    # Parsed structured issues (e.g. from sarif/json output)
    issues: List[Dict[str, Any]] = Field(default_factory=list)


class Task(BaseModel):
    """Atomic unit of work from the Planner."""

    model_config = ConfigDict(frozen=True)
    id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    name: str
    description: str = Field(..., description="Human readable, used in prompts")
    # Reference to Skill ID in VerticalManifest. If None -> LLM Freeform Coding.
    skill_id: Optional[str] = None
    # Input parameters for the skill / context for the coder
    inputs: Dict[str, Any] = Field(default_factory=dict)
    # Dependencies: Task IDs that must be COMPLETED before this starts
    depends_on: List[str] = Field(default_factory=list)
    # Status tracked during execution
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent: Optional[NodeName] = None
    # Result artifacts
    file_changes: List[FileChange] = Field(default_factory=list)
    verification_results: List[VerificationGateResult] = Field(default_factory=list)
    retry_count: int = 0
    error_summary: Optional[str] = None


class ArtifactMetadata(BaseModel):
    """Final output metadata."""

    model_config = ConfigDict(frozen=True)
    project_name: str
    vertical_id: str
    tech_stack: Dict[str, str]
    git_commit_sha: Optional[str] = None
    artifact_path: str = Field(..., description="Path to zip/tar.gz in Object Storage")
    quality_report: Dict[str, Any] = Field(default_factory=dict)
    total_token_usage: TokenUsage
    duration_seconds: float


# =============================================================================
# MAIN STATE (TypedDict for LangGraph Compatibility)
# =============================================================================


class AgentState(TypedDict, total=False):
    """
    The Immutable State Object.
    LangGraph handles merging via `Annotated[..., operator.add]` for lists
    and `operator.or_` for dicts, but we prefer explicit returns in nodes.
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
    tech_stack_hints: Dict[str, str]
    constraints: List[str]
    context_files: List[Dict[str, str]]  # [{name, content_b64}]

    # --- Vertical Context (Injected at INIT) ---
    vertical_manifest: Dict[str, Any]  # Parsed Manifest.yaml (validated separately)
    available_skills: Dict[str, Any]  # {skill_id: skill_def}

    # --- Planning Phase ---
    spec_markdown: str  # Detailed SPEC.md generated by Planner
    architecture_markdown: str  # ARCHITECTURE.md
    task_graph: List[Task]  # The Plan (DAG flattened to list with depends_on)
    current_task_id: Optional[str]

    # --- Execution Context ---
    workspace_path: str  # Absolute path in Sandbox (e.g. /workspace/project)
    project_files: Annotated[Dict[str, str], operator.or_]  # Virtual FS cache: {rel_path: content_hash}
    # Note: Actual FS is in Sandbox. This is a metadata index for LLM context.

    # --- Verification & Fix Loop ---
    verification_history: Annotated[List[VerificationGateResult], operator.add]
    current_gate_results: List[VerificationGateResult]  # Results of last verifier run
    fix_attempt_count: int
    max_fix_retries: int

    # --- Output ---
    final_artifact: Optional[ArtifactMetadata]

    # --- Observability & Control ---
    token_usage: TokenUsage
    logs: Annotated[List[str], operator.add]  # Structured log lines for UI streaming
    human_interrupt_reason: Optional[str]  # Set if HITL triggered
    error: Optional[str]  # Fatal error message

    # --- Extensibility (Vertical-specific data) ---
    metadata: Dict[str, Any]  # Verticals can store anything here (e.g. db_schema, openapi_spec)


# =============================================================================
# HELPER FUNCTIONS (Pure, for Node Logic)
# =============================================================================


def get_current_task(state: AgentState) -> Optional[Task]:
    """Safely extract current task object."""
    if not state.get("current_task_id"):
        return None
    for task in state.get("task_graph", []):
        if task.id == state["current_task_id"]:
            return task
    return None


def update_task_in_graph(state: AgentState, updated_task: Task) -> List[Task]:
    """Return new task_graph with updated task (immutability)."""
    return [updated_task if t.id == updated_task.id else t for t in state["task_graph"]]


def get_pending_tasks(state: AgentState) -> List[Task]:
    return [t for t in state.get("task_graph", []) if t.status == TaskStatus.PENDING]


def get_failed_tasks(state: AgentState) -> List[Task]:
    return [t for t in state.get("task_graph", []) if t.status == TaskStatus.VERIFICATION_FAILED]


def is_plan_complete(state: AgentState) -> bool:
    tasks = state.get("task_graph", [])
    return all(t.status == TaskStatus.COMPLETED for t in tasks)


def calculate_total_tokens(state: AgentState) -> TokenUsage:
    total = TokenUsage()
    # Node usages are tracked in token_usage field directly now, but history might exist
    return state.get("token_usage", TokenUsage())
