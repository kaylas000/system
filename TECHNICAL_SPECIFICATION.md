# Техническое задание: Agentic Platform (AutoGen)

Единый документ, собранный из 7 частей спецификации без сокращений и изменений содержания.

## Оглавление

1. Ответ №1: Kernel Spec — Спецификация Ядра Платформы
2. Ответ №2: Infra Spec — Инфраструктура Исполнения и Инструменты
3. Ответ №3: Skills Framework Spec — Система Навыков (Skills), Манифесты, Плагины
4. Ответ №4: Knowledge Spec — Конвейер Знаний (RAG, Graph DB, Ingestion, Retrieval)
5. Ответ №5: Vertical Spec — SaaS Web Implementation (Next.js 14+ / React / tRPC / Prisma)
6. Ответ №6: Ops Spec — CI/CD, Observability, Cost Control, HITL UI, Deployment
7. Ответ №7: Gateway Spec — Unified API, Router, Cross-Vertical Orchestration

---

# Ответ №1: Kernel Spec — Спецификация Ядра Платформы

Это **фундамент**. Весь остальной код (вертикали, скиллы, инструменты) будет реализовывать протоколы, определенные здесь, и оперировать стейтом, описанным ниже.

Сохрани эти файлы в структуру:
```text
specs/01_kernel/
├── ARCHITECTURE.md
├── state.py
├── graph_topology.md
└── protocols.py
```

---

## 1. `specs/01_kernel/ARCHITECTURE.md`

````markdown
# Agentic Platform Kernel — Architecture Specification

**Version:** 1.0
**Status:** Draft for Implementation
**Author:** Platform Team

---

## 1. Core Philosophy

1.  **State is King:** Single, immutable-ish state object (`AgentState`) passed by reference through the graph. No hidden context.
2.  **Graph over Chain:** Cyclic directed graph (LangGraph) enabling `Generate -> Verify -> Fix` loops. No linear pipelines.
3.  **Tools > Skills > Agents:**
    *   **Tools:** Atomic actions (FS read, Shell exec, LSP query, LLM call). Stateless, pure.
    *   **Skills:** Deterministic templates + logic (Jinja2 + Python hooks) for boilerplate. Zero LLM latency.
    *   **Agents (Nodes):** LLM-powered reasoning steps (Planner, Coder, Fixer). High latency, high value.
4.  **Vertical Isolation:** Kernel knows *nothing* about Next.js, Python, or Terraform. It executes `IVertical` protocol.
5.  **Observability First:** Every node emits structured traces (LangSmith/OTel). Token costs tracked per node per run.

---

## 2. High-Level Architecture (Mermaid)

```mermaid
graph TD
    subgraph "Client Layer"
        API[Unified Gateway / CLI]
    end

    subgraph "Kernel Core (Single Process)"
        Router[Vertical Router] --> Kernel[Kernel Runtime\n(LangGraph App)]
        Kernel --> Checkpointer[(Postgres Checkpointer)]
        Kernel --> ToolRegistry[Tool Registry]
    end

    subgraph "Vertical Plugin (Loaded Dynamically)"
        Vertical[IVertical Implementation]
        Vertical --> Skills[Skill Registry]
        Vertical --> Prompts[Prompt Templates]
        Vertical --> Gates[Verification Gates]
        Vertical --> Manifest[Manifest.yaml]
    end

    subgraph "External Infrastructure (Microservices)"
        Sandbox[Sandbox Pool\n(E2B / Daytona / Docker)]
        LLM[LLM Gateway\n(LiteLLM / vLLM)]
        VectorDB[(Vector DB\nQdrant)]
        GraphDB[(Graph DB\nKuzu/Neo4j)]
        Postgres[(Postgres\nState & Artifacts)]
    end

    Kernel -.->|Protocol| Vertical
    Kernel -->|Tools| ToolRegistry
    ToolRegistry -->|FS/Shell/LSP| Sandbox
    ToolRegistry -->|RAG| VectorDB
    ToolRegistry -->|Code Graph| GraphDB
    ToolRegistry -->|LLM Call| LLM
    Kernel -->|Persist State| Checkpointer
```

---

## 3. Main Execution Loop (Macro Cycle)

```mermaid
stateDiagram-v2
    [*] --> INITIALIZE: Request Received
    INITIALIZE --> PLANNING: Load Vertical, Create State
    PLANNING --> TASK_LOOP: Plan Generated (TaskGraph)

    state TASK_LOOP {
        GET_NEXT_TASK --> CODING: Task Assigned
        CODING --> VERIFICATION: Artifacts Produced
        VERIFICATION --> FIX_LOOP: Gate Failed
        VERIFICATION --> GET_NEXT_TASK: Gate Passed
        VERIFICATION --> FINALIZATION: All Tasks Done
        
        state FIX_LOOP {
            ANALYZE_ERROR --> PATCHING: Fix Strategy
            PATCHING --> VERIFICATION: Retry (max 3)
        }
    }

    FINALIZATION --> DOCUMENTATION: Generate Docs/ADR
    DOCUMENTATION --> PACKAGING: Create Artifact Zip
    PACKAGING --> COMPLETED: Success
    COMPLETED --> [*]
```

---

## 4. Key Design Decisions

| Decision | Rationale | Implementation Detail |
| :--- | :--- | :--- |
| **LangGraph as Engine** | Native cycles, streaming, checkpointing, human-in-the-loop. | `StateGraph` compiled with `PostgresSaver`. |
| **Pydantic State** | Type safety, validation, serialization for checkpoints. | `AgentState` = `TypedDict` + `Pydantic Models` for nested objects. |
| **Dependency Injection via Protocols** | Testability, Vertical swapability, Kernel purity. | `protocols.py` defines `IVertical`, `ISandbox`, `ILLMClient`, `ITool`. |
| **Structured Output Only** | No regex parsing of LLM output. Reliability. | `Instructor` + `response_model=PydanticModel` for *every* LLM call. |
| **Tool Calling via Tool Registry** | Centralized logging, retries, permissions, sandbox routing. | `ToolRegistry.execute(tool_name, args, state)` returns `ToolResult`. |
| **Vertical as Config + Code** | Vertical = `Manifest.yaml` + `skills/` + `prompts/` + `IVertical` impl. | Loaded via entrypoint `vertical.py:create_vertical()`. |

---

## 5. Non-Functional Requirements (NFR)

| Metric | Target | Measurement |
| :--- | :--- | :--- |
| **Cold Start (Kernel + Vertical Load)** | < 2s | Import time + Manifest parse. |
| **State Checkpoint Latency** | < 100ms | Postgres `INSERT` + JSONB serialization. |
| **LLM Call Overhead (Kernel)** | < 50ms | LiteLLM routing + Instructor parsing. |
| **Max Concurrent Runs (per Kernel instance)** | 50+ | Async `ainvoke`, connection pooling. |
| **Token Budget Enforcement** | Hard Limit | `state.token_usage.total > config.max_tokens` -> `GraphInterrupt`. |

---

## 6. Directory Structure (Kernel Source)

```text
kernel/
├── __init__.py
├── config.py              # Pydantic Settings (env vars)
├── state.py               # AgentState, Task, VerificationResult, etc.
├── graph/
│   ├── __init__.py
│   ├── builder.py         # build_graph(vertical: IVertical) -> CompiledGraph
│   ├── nodes/             # Shared nodes (planner, coder, verifier, fixer, documenter)
│   │   ├── __init__.py
│   │   ├── planner.py
│   │   ├── coder.py
│   │   ├── verifier.py
│   │   ├── fixer.py
│   │   └── documenter.py
│   └── routing.py         # Conditional edge logic (route_after_verification)
├── tools/
│   ├── __init__.py
│   ├── registry.py        # ToolRegistry, BaseTool, ToolResult
│   ├── filesystem.py      # Read/Write/Glob/Grep tools
│   ├── shell.py           # Exec tool (streaming)
│   ├── lsp.py             # LSP Client Tool (goto_def, hover, refs)
│   ├── rag.py             # Retrieval Tool
│   └── git.py             # Commit, Diff, Branch tools
├── vertical/
│   ├── __init__.py
│   ├── loader.py          # load_vertical(vertical_id) -> IVertical
│   ├── registry.py        # VerticalRegistry (scan verticals/ folder)
│   └── protocols.py       # IVertical, IVerifierGate, ISkillExecutor
├── llm/
│   ├── __init__.py
│   ├── client.py          # LiteLLM wrapper with Instructor, Cost Tracking
│   └── prompts.py         # PromptCompiler (Jinja2 + FewShotSelector)
├── persistence/
│   ├── __init__.py
│   └── checkpointer.py    # PostgresSaver wrapper
└── observability/
    ├── __init__.py
    └── tracing.py         # LangSmith / OTel setup
```

---

## 7. Configuration (Environment Variables)

```yaml
# kernel/config.yaml (loaded via Pydantic Settings)
kernel:
  max_concurrent_runs: 20
  default_max_retries: 3
  recursion_limit: 50 # LangGraph recursion limit

llm:
  gateway_url: "http://litellm:4000" # or https://api.openai.com
  default_model: "router/primary_coder" # LiteLLM router alias
  planner_model: "router/planner"       # Smart model (Opus/Sonnet/GPT-4o)
  fixer_model: "router/fast_fixer"      # Fast model (Haiku/4o-mini)
  request_timeout: 120
  max_tokens_per_run: 2_000_000

sandbox:
  provider: "e2b" # or "daytona", "docker"
  api_key: "${SANDBOX_API_KEY}"
  default_image: "my-registry/autogen-sandbox-base:latest"
  cpu: 2
  memory_mb: 4096
  timeout_sec: 300

database:
  postgres_dsn: "postgresql://user:pass@localhost:5432/autogen"
  pool_size: 20

vector_db:
  qdrant_url: "http://qdrant:6333"
  api_key: "${QDRANT_API_KEY}"

observability:
  langsmith_api_key: "${LANGSMITH_API_KEY}"
  langsmith_project: "autogen-kernel"
  otel_endpoint: "http://jaeger:4317"
```
````

---

## 2. `specs/01_kernel/state.py`

> **Критически важный файл.** Агент-разработчик должен скопировать его 1-в-1. Никаких `Any`, только строгая типизация.

```python
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
```

---

## 3. `specs/01_kernel/graph_topology.md`

````markdown
# Kernel Graph Topology & Routing Logic

**Graph Engine:** `langgraph.graph.StateGraph`
**State:** `AgentState` (from `state.py`)
**Checkpointer:** `PostgresSaver` (async)

---

## 1. Node Definitions

| Node Name | Function | Description | Retry Policy |
| :--- | :--- | :--- | :--- |
| `initialize` | `initialize_node` | Load Vertical, validate prompt, create sandbox, init state. | None (Fatal) |
| `planner` | `planner_node` | Generate `spec_markdown`, `architecture_markdown`, `task_graph`. | 1 Retry (on JSON parse fail) |
| `get_next_task` | `get_next_task_node` | **Logic Node (No LLM).** Select next PENDING task with resolved deps. Set `current_task_id`. | N/A |
| `coder` | `coder_node` | Execute Skill OR Freeform Coding. Apply FileChanges to Sandbox. | 0 (Errors caught by Verifier) |
| `verifier` | `verifier_node` | Run **ALL** Verification Gates for current task (or project). Parallel execution. | 0 |
| `fixer` | `fixer_node` | Analyze `current_gate_results`, read failed files, generate patches. | Max 3 (tracked in `fix_attempt_count`) |
| `documenter` | `documenter_node` | Generate README, ARCHITECTURE.md, API docs, Changelog. | 1 |
| `packager` | `packager_node` | Create ZIP, upload to S3/MinIO, generate `ArtifactMetadata`. | 1 |
| `human_review` | `human_review_node` | **Interrupt Node.** Pauses graph. Waits for external signal (Approve/Edit/Abort). | Infinite (Wait) |

---

## 2. Edge Definitions (Mermaid)

```mermaid
graph TD
    START --> INIT[initialize]
    INIT --> PLANNER[planner]
    PLANNER --> GET_TASK[get_next_task]

    GET_TASK -->|Has Task| CODER[coder]
    GET_TASK -->|No Tasks| DOC[documenter]

    CODER --> VERIFIER[verifier]

    VERIFIER -->|Gates Failed & Retries < Max| FIXER[fixer]
    VERIFIER -->|Gates Passed| GET_TASK
    VERIFIER -->|Infra Error| HUMAN[human_review]
    VERIFIER -->|Max Retries Exceeded| HUMAN

    FIXER --> VERIFIER

    DOC --> PACK[packager]
    PACK --> END[__end__]

    HUMAN -.->|User: Approve/Fix Plan| PLANNER
    HUMAN -.->|User: Approve Fix/Skip Gate| VERIFIER
    HUMAN -.->|User: Abort| END
```

---

## 3. Routing Logic (Python Implementation for `graph/routing.py`)

```python
# specs/01_kernel/graph/routing.py (Reference Implementation)

from typing import Literal
from ..state import AgentState, TaskStatus, VerificationGateStatus, NodeName

def route_after_initialize(state: AgentState) -> Literal["planner", "human_review"]:
    """Check if vertical loaded correctly."""
    if state.get("error"):
        return "human_review" # Config error
    return "planner"

def route_after_planner(state: AgentState) -> Literal["get_next_task", "human_review"]:
    """Validate Planner output."""
    if not state.get("task_graph"):
        state["error"] = "Planner produced empty task graph."
        return "human_review"
    # Validate DAG (no cycles, deps exist)
    if not validate_dag(state["task_graph"]):
        state["error"] = "Planner produced invalid DAG (cycles or missing deps)."
        return "human_review"
    return "get_next_task"

def route_get_next_task(state: AgentState) -> Literal["coder", "documenter"]:
    """Pure logic: Find next runnable task."""
    pending = [t for t in state["task_graph"] if t.status == TaskStatus.PENDING]
    # Check dependencies
    completed_ids = {t.id for t in state["task_graph"] if t.status == TaskStatus.COMPLETED}
    
    for task in pending:
        if all(dep in completed_ids for dep in task.depends_on):
            # Found runnable task
            state["current_task_id"] = task.id
            # Mark IN_PROGRESS immediately to avoid race conditions in parallel runs
            # (State update happens via node return)
            return "coder"
    
    # No runnable tasks. Check if any failed/blocked.
    if any(t.status == TaskStatus.VERIFICATION_FAILED for t in state["task_graph"]):
        # This case should ideally be handled by verifier->fixer loop
        # But if we land here, something is wrong.
        state["error"] = "Deadlock: Pending tasks exist but deps unmet, and failed tasks present."
        return "human_review" # or raise Error
    
    return "documenter" # Plan complete

def route_after_verifier(state: AgentState) -> Literal["fixer", "get_next_task", "human_review"]:
    """The Core Control Flow Decision."""
    results = state.get("current_gate_results", [])
    current_task_id = state.get("current_task_id")
    
    if not current_task_id:
        return "human_review" # Invalid state

    # 1. Check for Infrastructure Errors (Sandbox down, Timeout)
    infra_errors = [r for r in results if r.status == VerificationGateStatus.ERROR]
    if infra_errors:
        state["error"] = f"Infrastructure failure in gates: {[r.gate_id for r in infra_errors]}"
        return "human_review"

    # 2. Check for Failures
    failed_gates = [r for r in results if r.status == VerificationGateStatus.FAILED]
    
    if not failed_gates:
        # SUCCESS: Mark task COMPLETED, clear fix counter, go to next
        return "get_next_task"

    # 3. FAILURE: Check Retry Budget
    task = next(t for t in state["task_graph"] if t.id == current_task_id)
    
    if task.retry_count >= state.get("max_fix_retries", 3):
        state["error"] = f"Task {current_task_id} exceeded max fix retries ({task.retry_count}). Last errors: {[r.stderr for r in failed_gates]}"
        return "human_review" # Escalate to human

    # 4. RETRY: Prepare for Fixer
    state["fix_attempt_count"] = task.retry_count + 1
    # Collect files to fix from all failed gates
    files_to_fix = set()
    for r in failed_gates:
        files_to_fix.update(r.files_to_fix)
    state["metadata"]["files_to_fix"] = list(files_to_fix)
    state["metadata"]["failed_gate_ids"] = [r.gate_id for r in failed_gates]
    
    return "fixer"

def route_after_fixer(state: AgentState) -> Literal["verifier", "human_review"]:
    """Fixer either produced patches (-> verifier) or gave up (-> human)."""
    # Fixer node updates task.file_changes and increments task.retry_count
    # If fixer returns empty patches / error -> human
    if state.get("error") and "Fixer failed" in state["error"]:
        return "human_review"
    return "verifier"

def route_after_documenter(state: AgentState) -> Literal["packager", "human_review"]:
    if state.get("error"):
        return "human_review"
    return "packager"

def route_after_packager(state: AgentState) -> Literal["__end__", "human_review"]:
    if state.get("error"):
        return "human_review"
    return "__end__"

# --- Helper ---
def validate_dag(tasks: list) -> bool:
    """Simple Kahn's algorithm check."""
    # Implementation omitted for brevity. Must check cycles & valid dep IDs.
    return True
```

---

## 4. Node Signatures (Contract for `graph/nodes/*.py`)

All nodes **MUST** follow this signature:
```python
async def node_name(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """
    Returns a PARTIAL state update (dict).
    LangGraph merges this into the main state.
    Keys not returned remain unchanged.
    """
    # 1. Extract config (vertical, llm_client, tool_registry, sandbox)
    vertical: IVertical = config["configurable"]["vertical"]
    tool_registry: ToolRegistry = config["configurable"]["tool_registry"]
    llm_client: ILLMClient = config["configurable"]["llm_client"]
    
    # 2. Logic...
    
    # 3. Return delta
    return {
        "status": RunStatus.CODING,
        "updated_at": datetime.utcnow(),
        "task_graph": new_task_graph, # Full list replacement
        "logs": [f"[{node_name}] Started task {task_id}"],
        "token_usage": updated_token_usage, # Full replacement (aggregated)
    }
```

---

## 5. Checkpointing Strategy

*   **Checkpoint After:** `planner`, `coder` (after file write), `verifier`, `fixer`, `documenter`, `packager`.
*   **Checkpoint Key:** `thread_id` = `run_id` (passed in `config.configurable.thread_id`).
*   **Serialization:** `PostgresSaver` uses `json.dumps` with custom `PydanticEncoder`.
*   **Resume Logic:** Gateway calls `graph.ainvoke(None, config={"configurable": {"thread_id": run_id}})` with `None` input to resume from last checkpoint.

---

## 6. Human-in-the-Loop (HITL) Interrupts

Implemented via `NodeInterrupt` (LangGraph native) or `graph.update_state` from external API.

**Interrupt Points:**
1.  **Post-Planner:** User reviews `spec_markdown` + `task_graph`. Actions: `Approve`, `Edit Plan`, `Abort`.
2.  **Post-Verifier (Max Retries):** User sees error logs. Actions: `Skip Gate`, `Edit Code Manually`, `Abort`.
3.  **Pre-Destructive Action:** `packager` about to overwrite production artifact (optional).

**Interrupt Payload (sent to UI via WebSocket):**
```json
{
  "run_id": "...",
  "interrupt_type": "plan_review",
  "payload": {
    "spec_markdown": "...",
    "task_graph": [...],
    "estimated_cost_usd": 0.45
  },
  "actions": ["approve", "edit_plan", "abort"]
}
```
````

---

## 4. `specs/01_kernel/protocols.py`

> **Контракты (Interfaces).** Kernel зависит от абстракций. Вертикали и Инфраструктура их реализуют.

```python
# specs/01_kernel/protocols.py
"""
Protocol Definitions (Interfaces) for Dependency Inversion.
Kernel imports ONLY these. Concrete implementations live in:
- verticals/<name>/vertical_impl.py
- kernel/tools/...
- kernel/llm/client.py
"""

from __future__ import annotations
import abc
from typing import Protocol, Dict, List, Any, Optional, AsyncIterator, runtime_checkable
from pydantic import BaseModel
from .state import AgentState, Task, FileChange, VerificationGateResult, VerificationGateStatus

# =============================================================================
# LLM ABSTRACTION
# =============================================================================


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None


class LLMResponse(BaseModel):
    content: str
    tool_calls: Optional[List[Dict]] = None
    usage: Dict[str, int]  # {prompt, completion, total}
    model: str
    cost_usd: float


@runtime_checkable
class ILLMClient(Protocol):
    """Unified LLM Interface (Structured Output + Streaming)."""

    async def achat(
        self,
        messages: List[LLMMessage],
        model: str,
        response_model: Optional[type[BaseModel]] = None,  # Instructor style
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """Single call. Returns parsed Pydantic model if response_model given."""
        ...

    async def astream_chat(self, messages: List[LLMMessage], model: str, **kwargs) -> AsyncIterator[str]:
        """Streaming text chunks (for UI)."""
        ...

    def estimate_tokens(self, messages: List[LLMMessage], model: str) -> int:
        """Local estimation (tiktoken) for budget checks."""
        ...


# =============================================================================
# SANDBOX / EXECUTION ENVIRONMENT
# =============================================================================


class SandboxSpec(BaseModel):
    image: str
    cpu: int = 2
    memory_mb: int = 4096
    env_vars: Dict[str, str] = {}
    ports: List[int] = []  # Ports to expose (e.g. 3000, 8000)
    timeout_sec: int = 300


class CommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class FileStat(BaseModel):
    path: str
    size: int
    is_dir: bool
    modified_at: float


@runtime_checkable
class ISandbox(Protocol):
    """Abstract Sandbox Interface. Implementation: E2B, Daytona, Docker, Local."""

    async def create(self, spec: SandboxSpec) -> str:
        """Create sandbox. Returns sandbox_id."""
        ...

    async def close(self, sandbox_id: str) -> None:
        """Destroy sandbox."""
        ...

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        workdir: str = "/workspace",
        env: Optional[Dict[str, str]] = None,
        stream: bool = False,
    ) -> CommandResult:
        """Execute command. Stream=True returns async generator of chunks."""
        ...

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        """Write single file (creates parent dirs)."""
        ...

    async def write_files(self, sandbox_id: str, files: List[FileChange]) -> None:
        """Batch write (atomic-ish)."""
        ...

    async def read_file(self, sandbox_id: str, path: str) -> str: ...

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> List[FileStat]: ...

    async def download_dir(self, sandbox_id: str, src_path: str, dest_path: str) -> None:
        """Download directory as zip/tar for artifact packaging."""
        ...

    async def get_preview_url(self, sandbox_id: str, port: int) -> str:
        """Get public URL for exposed port (for E2E tests / Human Review)."""
        ...


# =============================================================================
# TOOLS (Called by Nodes via ToolRegistry)
# =============================================================================


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = {}


@runtime_checkable
class ITool(Protocol):
    """Base protocol for all tools."""

    name: str
    description: str
    parameters_json_schema: Dict[str, Any]  # For LLM function calling

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult: ...


# Specific Tool Protocols (for type hinting in nodes)
@runtime_checkable
class IFileSystemTool(ITool, Protocol):
    async def read(self, sandbox_id: str, path: str) -> ToolResult: ...
    async def write(self, sandbox_id: str, path: str, content: str) -> ToolResult: ...
    async def glob(self, sandbox_id: str, pattern: str) -> ToolResult: ...
    async def grep(self, sandbox_id: str, pattern: str, path: str) -> ToolResult: ...


@runtime_checkable
class IShellTool(ITool, Protocol):
    async def run(self, sandbox_id: str, command: str, workdir: str) -> ToolResult: ...


@runtime_checkable
class ILSPTool(ITool, Protocol):
    async def goto_definition(self, sandbox_id: str, file: str, line: int, col: int) -> ToolResult: ...
    async def find_references(self, sandbox_id: str, file: str, line: int, col: int) -> ToolResult: ...
    async def hover(self, sandbox_id: str, file: str, line: int, col: int) -> ToolResult: ...
    async def symbols(self, sandbox_id: str, file: str) -> ToolResult: ...  # Document symbols


@runtime_checkable
class IRAGTool(ITool, Protocol):
    async def retrieve(self, query: str, filters: Dict[str, Any], top_k: int) -> ToolResult: ...
    async def retrieve_by_symbol(self, symbol_name: str, file_path: str) -> ToolResult: ...


# =============================================================================
# VERTICAL INTERFACE (The Plugin Contract)
# =============================================================================


class VerticalManifest(BaseModel):
    """Parsed manifest.yaml"""

    id: str
    name: str
    version: str
    description: str
    runtime: Dict[str, Any]  # sandbox spec overrides
    skills: List[str]  # skill_ids
    planner: Dict[str, Any]  # config for planner node
    verifier: Dict[str, Any]  # gates config
    prompts: Dict[str, str]  # prompt_name -> template_path
    rag_collections: List[str]


class SkillDef(BaseModel):
    id: str
    name: str
    description: str
    inputs_json_schema: Dict[str, Any]
    template_dir: str  # relative to vertical root
    post_scripts: List[str]  # commands to run after render
    validation: List[str]  # commands to validate success


@runtime_checkable
class IVertical(Protocol):
    """Main Vertical Entry Point. Loaded by Kernel at startup."""

    @property
    def manifest(self) -> VerticalManifest: ...

    @property
    def skills(self) -> Dict[str, SkillDef]: ...

    async def initialize_state(self, request: "GenerateRequest") -> Dict[str, Any]:
        """
        Called by `initialize_node`.
        Returns partial state dict: {workspace_path, vertical_manifest, available_skills, ...}
        """
        ...

    def get_planner_prompt(self, state: AgentState) -> str:
        """Render system prompt for Planner (Jinja2 + Context)."""
        ...

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        """Render system prompt for Coder/Fixer."""
        ...

    def get_verification_gates(self, state: AgentState, task: Optional[Task]) -> List["IVerificationGate"]:
        """
        Return list of gates to run.
        If task is None -> Project-level gates (Build, E2E, Security).
        If task provided -> Task-level gates (Lint, Unit Test for changed files).
        """
        ...

    def get_skill_executor(self, skill_id: str) -> "ISkillExecutor":
        """Return executor for specific skill."""
        ...

    async def on_task_complete(self, state: AgentState, task: Task) -> None:
        """Hook: Update derived artifacts (e.g. regenerate OpenAPI spec after model change)."""
        ...

    async def finalize(self, state: AgentState) -> AgentState:
        """Final modifications before packaging (e.g. generate root README)."""
        ...


@runtime_checkable
class IVerificationGate(Protocol):
    """Single verification gate (Lint, Test, Typecheck, Build, Custom)."""

    id: str
    name: str
    description: str
    # If True, runs on every task. If False, runs only at project finalization.
    runs_on_every_task: bool

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, state: AgentState
    ) -> VerificationGateResult:
        """Run the gate. Must return structured result."""
        ...


@runtime_checkable
class ISkillExecutor(Protocol):
    """Executes a deterministic skill (Template + Scripts)."""

    skill_def: SkillDef

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, inputs: Dict[str, Any]
    ) -> List[FileChange]:
        """
        1. Render Jinja2 templates from skill_def.template_dir with inputs.
        2. Write files to sandbox.
        3. Run post_scripts sequentially.
        4. Run validation commands.
        5. Return list of FileChange applied.
        """
        ...


# =============================================================================
# REQUEST/RESPONSE (Gateway Contract)
# =============================================================================


class GenerateRequest(BaseModel):
    prompt: str
    vertical_id: Optional[str] = None
    tech_stack_hints: Dict[str, str] = {}
    constraints: List[str] = []
    context_files: List[Dict[str, str]] = []  # {name, content_base64}
    max_budget_usd: Optional[float] = None
    webhook_url: Optional[str] = None  # For async completion


class GenerateResponse(BaseModel):
    run_id: str
    status: str
    message: str
    stream_url: str  # WebSocket URL for logs
```

---

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet)

> **CONTEXT FOR NEXT STEP (Infra Spec):**
> Ты реализуешь `specs/01_kernel/`.
> 1. Создай пакет `kernel/` с файлами: `config.py`, `state.py`, `protocols.py`.
> 2. Реализуй `graph/builder.py`: функция `build_graph(vertical: IVertical) -> CompiledStateGraph`.
>    *   Используй `StateGraph(AgentState)`.
>    *   Добавь ноды-заглушки (raise NotImplementedError) для: `initialize`, `planner`, `get_next_task`, `coder`, `verifier`, `fixer`, `documenter`, `packager`, `human_review`.
>    *   Подключи `routing.py` логику через `add_conditional_edges`.
>    *   Настрой `PostgresSaver` чекпоинтер.
> 3. Напиши `kernel/graph/routing.py` ровно как в спеке.
> 4. Напиши `kernel/tools/registry.py`: класс `ToolRegistry` с методами `register(tool: ITool)`, `execute(name, args, state)`.
> 5. **Тест:** `tests/kernel/test_graph_compilation.py` — проверяет, что граф компилируется без ошибок, имеет все ноды и ребра из `graph_topology.md`.

---

### ✅ Чек-лист готовности Кернела (Definition of Done для Ответа №1)

- [ ] `state.py` проходит `mypy --strict` и `pydantic` валидацию на примере JSON.
- [ ] `protocols.py` не имеет зависимостей от реализаций (нет импортов `e2b`, `qdrant`, `langchain`).
- [ ] `graph_topology.md` покрывает все циклы (Fix Loop, Task Loop, Human Loop).
- [ ] `ARCHITECTURE.md` содержит актуальные Mermaid диаграммы.
- [ ] Готов к внедрению `verticals/saas_web/` (следующий шаг).

---

---

# Ответ №2: Infra Spec — Инфраструктура Исполнения и Инструменты

Это **мышцы и нервы** системы. Без качественной реализации этого слоя агент будет «слепым и парализованным».

Сохрани в структуру:
```text
specs/02_infra/
├── sandbox/
│   ├── SANDBOX_API.md
│   ├── DOCKERFILE.sandbox
│   ├── SANDBOX_MANAGER.py
│   └── PROVIDERS.md
├── tools/
│   ├── TOOL_REGISTRY.py
│   ├── FILESYSTEM_TOOL.py
│   ├── SHELL_TOOL.py
│   ├── LSP_TOOL.py
│   ├── RAG_TOOL.py
│   └── GIT_TOOL.py
└── llm_gateway/
    ├── LLM_CLIENT.py
    └── COST_TRACKER.py
```

---

## 1. `specs/02_infra/sandbox/SANDBOX_API.md`

````markdown
# Sandbox Execution Environment Specification

**Goal:** Secure, reproducible, language-agnostic code execution with LSP support.

## 1. Architecture Options (Priority Order)

| Provider | Pros | Cons | Use Case |
| :--- | :--- | :--- | :--- |
| **E2B (Code Interpreter SDK)** | Best DX, built-in FS/Shell/Net tools, persistent sessions, Python/JS/Go/Rust support, **File Watcher API**. | Cost (managed), Vendor lock-in (mitigated by our `ISandbox` protocol). | **Default for MVP & Cloud.** |
| **Daytona** | Open Source, Kubernetes-native, "Workspace" concept, GPU support, SSH access. | Self-host ops overhead. | **Enterprise On-Prem / GPU workloads.** |
| **Modal / Fly.io Machines** | Serverless, fast cold start (<1s), GPU, custom images. | API differs from "persistent VM" model. | **Burst scaling / CI jobs.** |
| **Custom Docker (gVisor/Kata)** | Full control, zero cost (own iron), max isolation. | High implementation effort (snapshots, networking, file sync). | **Air-gapped / High Security.** |

**Decision:** Implement `ISandbox` for **E2B** first (fastest MVP). Add `DaytonaProvider` later. Protocol ensures zero kernel changes.

---

## 2. Sandbox Lifecycle & State

```mermaid
stateDiagram-v2
    [*] --> CREATING: SandboxManager.create(spec)
    CREATING --> READY: Image pulled, services started (LSP, File Watcher)
    CREATING --> FAILED: Timeout / Image pull error
    
    READY --> EXECUTING: exec() / write_file()
    EXECUTING --> READY: Command finished
    EXECUTING --> ERROR: Process killed / OOM
    
    READY --> SNAPSHOTTING: Checkpoint (State save)
    SNAPSHOTTING --> READY
    
    READY --> CLOSING: SandboxManager.close() / TTL expired
    CLOSING --> [*]: Resources released
```

### Critical Runtime Requirements (Base Image)
**All Vertical Sandboxes MUST inherit from `autogen-sandbox-base`**.
```dockerfile
# specs/02_infra/sandbox/DOCKERFILE.sandbox.base
FROM ubuntu:24.04 AS base

# 1. System Deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget unzip ca-certificates gnupg2 software-properties-common \
    build-essential pkg-config libssl-dev \
    # LSP Servers (Multi-lang)
    nodejs npm python3 python3-pip python3-venv golang-go \
    && rm -rf /var/lib/apt/lists/*

# 2. Universal Tools
# Tree-sitter CLI (for AST parsing in tools)
RUN npm install -g tree-sitter-cli

# 3. Language Specific LSPs (Installed globally for speed)
# TypeScript/JavaScript
RUN npm install -g typescript-language-server vscode-langservers-extracted @vue/language-server
# Python
RUN pip install --no-cache-dir 'python-lsp-server[all]' ruff-lsp basedpyright
# Go
RUN go install golang.org/x/tools/gopls@latest && mv /root/go/bin/gopls /usr/local/bin/
# Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    ~/.cargo/bin/rustup component add rust-analyzer && \
    ln -s ~/.cargo/bin/rust-analyzer /usr/local/bin/
# Terraform
RUN wget -q https://releases.hashicorp.com/terraform/1.9.0/terraform_1.9.0_linux_amd64.zip && \
    unzip terraform_1.9.0_linux_amd64.zip -d /usr/local/bin/ && rm terraform_1.9.0_linux_amd64.zip

# 4. Code Quality Tools (Used by Verifier Gates)
RUN npm install -g eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin prettier
RUN pip install --no-cache-dir mypy pytest pytest-cov bandit safety
RUN go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest && \
    mv /root/go/bin/golangci-lint /usr/local/bin/
RUN cargo install cargo-audit taplo-cli

# 5. User & Permissions (Non-root for security)
ARG USERNAME=autogen
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME
RUN echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/$USERNAME

USER $USERNAME
WORKDIR /workspace
ENV PATH="/home/$USERNAME/.local/bin:/home/$USERNAME/go/bin:$PATH"
```

### Vertical-Specific Images (Example: SaaS Web)
```dockerfile
# specs/02_infra/sandbox/DOCKERFILE.sandbox.saas_web
FROM my-registry/autogen-sandbox-base:latest AS saas-web

USER root
# Node version manager (fnm) for precise versions
RUN curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir /usr/local/bin --skip-shell
RUN fnm install 20 && fnm default 20
# pnpm
RUN npm install -g pnpm@9
# Playwright deps
RUN npx playwright install-deps chromium
USER autogen

# Pre-cache common deps (speeds up 'pnpm install' drastically)
RUN mkdir -p /workspace/.cache && chown autogen:autogen /workspace/.cache
ENV PNPM_STORE_PATH=/workspace/.cache/pnpm-store
```
````

---

## 3. `specs/02_infra/sandbox/SANDBOX_MANAGER.py`

```python
# specs/02_infra/sandbox/SANDBOX_MANAGER.py
"""
Sandbox Pool Manager.
Handles: Creation, Reuse (Session affinity), Health Checks, Cleanup, Quotas.
"""

from __future__ import annotations
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, List, AsyncGenerator
from contextlib import asynccontextmanager

from kernel.protocols import ISandbox, SandboxSpec, CommandResult, FileStat, FileChange
from kernel.config import settings

# --- Provider Implementations (Abstracted) ---


class E2BSandbox(ISandbox):
    """Wrapper around e2b_code_interpreter.Sandbox"""

    def __init__(self, sandbox_id: str, sandbox_obj: Any):  # Any = e2b.Sandbox
        self.sandbox_id = sandbox_id
        self._sandbox = sandbox_obj
        self._created_at = datetime.utcnow()

    @classmethod
    async def create(cls, spec: SandboxSpec) -> "E2BSandbox":
        from e2b_code_interpreter import Sandbox as E2BSandboxClient

        # Map our spec to E2B params
        sbx = await asyncio.to_thread(
            E2BSandboxClient.create,
            template=spec.image,  # E2B uses template IDs
            timeout=spec.timeout_sec,
            env_vars=spec.env_vars,
        )
        return cls(sbx.sandbox_id, sbx)

    async def close(self) -> None:
        await asyncio.to_thread(self._sandbox.close)

    async def exec(
        self, command: str, workdir: str = "/workspace", env: Optional[Dict] = None, stream: bool = False
    ) -> CommandResult:
        # E2B supports streaming via .start() but we use blocking for simplicity here
        proc = await asyncio.to_thread(self._sandbox.commands.run, command, cwd=workdir, env=env)
        return CommandResult(
            exit_code=proc.exit_code, stdout=proc.stdout, stderr=proc.stderr, duration_ms=0
        )  # E2B doesn't give duration easily

    async def write_file(self, path: str, content: str) -> None:
        await asyncio.to_thread(self._sandbox.files.write, path, content)

    async def write_files(self, files: List[FileChange]) -> None:
        # Batch write via tar upload is faster
        import tarfile, io

        tar_bytes = io.BytesIO()
        with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
            for f in files:
                data = f.content.encode()
                info = tarfile.TarInfo(name=f.path)
                info.size = len(data)
                info.mtime = int(datetime.utcnow().timestamp())
                tar.addfile(info, io.BytesIO(data))
        tar_bytes.seek(0)
        await asyncio.to_thread(self._sandbox.files.write_bytes, tar_bytes.read(), "/workspace")  # Extract at root

    async def read_file(self, path: str) -> str:
        return await asyncio.to_thread(self._sandbox.files.read, path)

    async def list_files(self, path: str = "/workspace") -> List[FileStat]:
        entries = await asyncio.to_thread(self._sandbox.files.list, path)
        return [FileStat(path=e.name, size=e.size, is_dir=e.is_dir, modified_at=e.mtime) for e in entries]

    async def download_dir(self, src_path: str, dest_path: str) -> None:
        # E2B: download as zip
        zip_bytes = await asyncio.to_thread(self._sandbox.download, src_path)
        with open(dest_path, "wb") as f:
            f.write(zip_bytes)

    async def get_preview_url(self, port: int) -> str:
        return self._sandbox.get_host(port)


# --- Pool Manager ---


@dataclass
class SandboxSlot:
    sandbox: ISandbox
    spec: SandboxSpec
    created_at: datetime
    last_used: datetime
    in_use: bool = False
    vertical_id: Optional[str] = None  # Affinity: reuse sandbox for same vertical (warm caches)


class SandboxManager:
    def __init__(self, max_pool_size: int = 20, ttl_minutes: int = 30):
        self._pool: Dict[str, SandboxSlot] = {}
        self._lock = asyncio.Lock()
        self._max_pool = max_pool_size
        self._ttl = timedelta(minutes=ttl_minutes)
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        async with self._lock:
            for slot in self._pool.values():
                await slot.sandbox.close()
            self._pool.clear()

    @asynccontextmanager
    async def acquire(self, vertical_id: str, spec: SandboxSpec) -> AsyncGenerator[ISandbox, None]:
        """Get sandbox from pool or create new. Returns context manager for auto-release."""
        sandbox_id = None
        async with self._lock:
            # 1. Try find warm sandbox for this vertical
            for sid, slot in self._pool.items():
                if not slot.in_use and slot.vertical_id == vertical_id and slot.spec == spec:
                    slot.in_use = True
                    slot.last_used = datetime.utcnow()
                    sandbox_id = sid
                    break

            # 2. Create new if pool not full
            if not sandbox_id and len(self._pool) < self._max_pool:
                sbx = await E2BSandbox.create(spec)  # TODO: Factory pattern for providers
                slot = SandboxSlot(
                    sandbox=sbx,
                    spec=spec,
                    created_at=datetime.utcnow(),
                    last_used=datetime.utcnow(),
                    in_use=True,
                    vertical_id=vertical_id,
                )
                self._pool[sbx.sandbox_id] = slot
                sandbox_id = sbx.sandbox_id

            # 3. Wait for release (simplified: raise error if exhausted)
            if not sandbox_id:
                raise RuntimeError("Sandbox pool exhausted")

        try:
            yield self._pool[sandbox_id].sandbox
        finally:
            async with self._lock:
                if sandbox_id in self._pool:
                    self._pool[sandbox_id].in_use = False
                    self._pool[sandbox_id].last_used = datetime.utcnow()

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = datetime.utcnow()
            async with self._lock:
                to_remove = [
                    sid for sid, slot in self._pool.items() if not slot.in_use and (now - slot.last_used) > self._ttl
                ]
                for sid in to_remove:
                    await self._pool[sid].sandbox.close()
                    del self._pool[sid]


# Global Singleton (initialized in Kernel main)
sandbox_manager: Optional[SandboxManager] = None
```

---

## 4. `specs/02_infra/tools/TOOL_REGISTRY.py`

```python
# specs/02_infra/tools/TOOL_REGISTRY.py
"""
Central Tool Registry.
- Registers tools (implementations of ITool).
- Handles: Permissions, Logging, Retries, Metrics, Sandbox Injection.
- Provides OpenAI Function Calling schemas for LLM.
"""

from __future__ import annotations
import json
import time
import logging
from typing import Dict, Any, Optional, List, Type
from pydantic import BaseModel, Field

from kernel.protocols import ITool, ToolResult, ISandbox, AgentState
from kernel.state import TokenUsage

logger = logging.getLogger("autogen.tools")


class ToolRegistry:
    def __init__(self, sandbox_manager: "SandboxManager"):
        self._tools: Dict[str, ITool] = {}
        self._sandbox_manager = sandbox_manager
        # Metrics
        self._call_counts: Dict[str, int] = {}
        self._error_counts: Dict[str, int] = {}
        self._latencies: Dict[str, List[float]] = {}

    def register(self, tool: ITool) -> None:
        if tool.name in self._tools:
            logger.warning(f"Overriding tool: {tool.name}")
        self._tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def get(self, name: str) -> Optional[ITool]:
        return self._tools.get(name)

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """Return OpenAI Function Calling compatible schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(
        self, tool_name: str, args: Dict[str, Any], state: AgentState, sandbox_id: Optional[str] = None
    ) -> ToolResult:
        """Main entry point. Injects sandbox_id from state if not provided."""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found")

        # Resolve Sandbox
        target_sandbox_id = sandbox_id or state.get("sandbox_id")
        if not target_sandbox_id:
            return ToolResult(success=False, error="No sandbox_id in state or args")

        start = time.perf_counter()
        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1

        try:
            logger.debug(f"Executing tool: {tool_name} with args: {json.dumps(args, default=str)[:200]}")
            result = await tool.execute(target_sandbox_id, args, state)

            latency = time.perf_counter() - start
            self._latencies.setdefault(tool_name, []).append(latency)

            if not result.success:
                self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
                logger.warning(f"Tool {tool_name} failed: {result.error}")

            return result
        except Exception as e:
            self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
            logger.exception(f"Tool {tool_name} crashed")
            return ToolResult(success=False, error=f"Tool execution exception: {str(e)}")

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "calls": self._call_counts,
            "errors": self._error_counts,
            "avg_latency_ms": {k: sum(v) / len(v) * 1000 for k, v in self._latencies.items() if v},
        }
```

---

## 5. `specs/02_infra/tools/FILESYSTEM_TOOL.py`

```python
# specs/02_infra/tools/FILESYSTEM_TOOL.py
"""
Filesystem Tools (Read, Write, Glob, Grep, List).
Critical: Implements 'Virtual FS Cache' in State to minimize Sandbox IO.
"""

from __future__ import annotations
import json
import pathspec
from typing import Dict, Any, List, Optional
from kernel.protocols import ITool, ToolResult, IFileSystemTool, ISandbox
from kernel.state import AgentState

# --- Schemas (for LLM Function Calling) ---

READ_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative path to file"},
        "offset": {"type": "integer", "default": 0, "description": "Line offset (0-based)"},
        "limit": {"type": "integer", "default": 200, "description": "Max lines to read"},
    },
    "required": ["path"],
}

WRITE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative path"},
        "content": {"type": "string", "description": "File content"},
        "mode": {"type": "string", "enum": ["create", "update", "append"], "default": "create"},
    },
    "required": ["path", "content"],
}

GLOB_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.ts)"},
        "path": {"type": "string", "default": "."},
    },
    "required": ["pattern"],
}

GREP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern"},
        "path": {"type": "string", "default": "."},
        "include": {"type": "string", "description": "File glob filter"},
    },
    "required": ["pattern"],
}

LIST_TOOL_SCHEMA = {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}

# --- Implementation ---


class FileSystemTool(IFileSystemTool):
    name = "filesystem"
    description = "Read, write, list, and search files in the sandbox workspace."

    # Combined schema for LLM (dispatches via 'action' arg)
    parameters_json_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "glob", "grep", "list"]},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "pattern": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
            "include": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "update", "append"]},
        },
        "required": ["action"],
    }

    def __init__(self, sandbox_manager: "SandboxManager"):
        self.sandbox_manager = sandbox_manager

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args.pop("action")
        method_map = {
            "read": self._read,
            "write": self._write,
            "glob": self._glob,
            "grep": self._grep,
            "list": self._list,
        }
        if action not in method_map:
            return ToolResult(success=False, error=f"Unknown action: {action}")
        return await method_map[action](sandbox_id, args, state)

    # --- Cache Helpers ---
    def _get_cache(self, state: AgentState) -> Dict[str, str]:
        return state.setdefault("project_files", {})  # {rel_path: content_hash}

    def _update_cache(self, state: AgentState, path: str, content: str):
        import hashlib

        state["project_files"][path] = hashlib.sha256(content.encode()).hexdigest()

    # --- Actions ---
    async def _read(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        path = args["path"]
        offset = args.get("offset", 0)
        limit = args.get("limit", 200)

        # 1. Check Cache
        cache = self._get_cache(state)
        # Note: We don't cache content in state (too big), only hashes.
        # But we CAN cache small files (<10KB) in metadata if needed.

        # 2. Read from Sandbox
        try:
            sbx = self.sandbox_manager.get_sandbox(sandbox_id)  # Need access to sandbox obj
            content = await sbx.read_file(path)
            lines = content.splitlines()
            selected = lines[offset : offset + limit]
            return ToolResult(
                success=True,
                data={
                    "path": path,
                    "content": "\n".join(selected),
                    "total_lines": len(lines),
                    "showing_lines": f"{offset + 1}-{offset + len(selected)}",
                },
            )
        except FileNotFoundError:
            return ToolResult(success=False, error=f"File not found: {path}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    async def _write(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        path = args["path"]
        content = args["content"]
        mode = args.get("mode", "create")

        sbx = self.sandbox_manager.get_sandbox(sandbox_id)

        if mode == "append":
            try:
                existing = await sbx.read_file(path)
                content = existing + "\n" + content
            except FileNotFoundError:
                pass  # Create new

        await sbx.write_file(path, content)
        self._update_cache(state, path, content)

        return ToolResult(success=True, data={"path": path, "bytes": len(content), "mode": mode})

    async def _glob(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        pattern = args["pattern"]
        base_path = args.get("path", ".")
        sbx = self.sandbox_manager.get_sandbox(sandbox_id)
        # Use shell `find` for speed and glob support
        cmd = f"find {base_path} -type f -name '{pattern}' 2>/dev/null | head -500"
        res = await sbx.exec(cmd)
        files = res.stdout.strip().split("\n") if res.stdout.strip() else []
        return ToolResult(success=True, data={"files": files, "count": len(files)})

    async def _grep(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        pattern = args["pattern"]
        base_path = args.get("path", ".")
        include = args.get("include", "*")
        sbx = self.sandbox_manager.get_sandbox(sandbox_id)
        # Use ripgrep (rg) if available, else grep -r
        cmd = f"rg --no-heading --line-number --json '{pattern}' -g '{include}' {base_path} 2>/dev/null | head -100"
        res = await sbx.exec(cmd)
        matches = []
        for line in res.stdout.strip().split("\n"):
            if line:
                try:
                    matches.append(json.loads(line))
                except:
                    pass
        return ToolResult(success=True, data={"matches": matches, "count": len(matches)})

    async def _list(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        path = args.get("path", ".")
        sbx = self.sandbox_manager.get_sandbox(sandbox_id)
        files = await sbx.list_files(path)
        return ToolResult(success=True, data={"files": [f.model_dump() for f in files]})
```

---

## 6. `specs/02_infra/tools/SHELL_TOOL.py`

```python
# specs/02_infra/tools/SHELL_TOOL.py
"""
Shell Execution Tool.
Supports: Streaming output, Timeout, Working Directory, Environment Variables.
Security: Command allowlist/denylist (configurable per Vertical).
"""

from __future__ import annotations
import shlex
import asyncio
from typing import Dict, Any, Optional, AsyncGenerator
from kernel.protocols import ITool, ToolResult, IShellTool, ISandbox
from kernel.state import AgentState

SHELL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Command to execute"},
        "workdir": {"type": "string", "default": "/workspace", "description": "Working directory"},
        "env": {"type": "object", "description": "Additional env vars"},
        "timeout": {"type": "integer", "default": 120, "description": "Timeout in seconds"},
        "stream": {"type": "boolean", "default": False, "description": "Stream output to logs"},
    },
    "required": ["command"],
}


class ShellTool(IShellTool):
    name = "shell"
    description = "Execute shell commands in the sandbox. Use for builds, tests, linters, git."
    parameters_json_schema = SHELL_TOOL_SCHEMA

    # Security: Commands that are NEVER allowed
    DENYLIST = {"rm -rf /", "mkfs", "dd if=", "shutdown", "reboot", ":(){ :|:& };:"}
    # Allowlist (optional, if enabled in vertical config)
    ALLOWLIST_PREFIXES = (
        "pnpm ",
        "npm ",
        "python ",
        "pip ",
        "pytest ",
        "mypy ",
        "ruff ",
        "tsc ",
        "eslint ",
        "go ",
        "cargo ",
        "terraform ",
        "git ",
        "cat ",
        "ls ",
        "head ",
        "tail ",
    )

    def __init__(self, sandbox_manager: "SandboxManager", allowlist_enabled: bool = True):
        self.sandbox_manager = sandbox_manager
        self.allowlist_enabled = allowlist_enabled

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        command = args["command"].strip()
        workdir = args.get("workdir", "/workspace")
        env = args.get("env", {})
        timeout = args.get("timeout", 120)
        stream = args.get("stream", False)

        # Security Check
        if self._is_dangerous(command):
            return ToolResult(success=False, error=f"Command blocked by security policy: {command}")

        sbx = self.sandbox_manager.get_sandbox(sandbox_id)

        try:
            if stream:
                # For streaming, we need a different sandbox API (async generator)
                # This is a simplified blocking version. Real impl uses `proc = await sandbox.process.start(...)`
                pass

            # Merge env: State env > Tool args env > Sandbox default
            merged_env = {**state.get("sandbox_env", {}), **env}

            result = await asyncio.wait_for(sbx.exec(command, workdir=workdir, env=merged_env), timeout=timeout)

            return ToolResult(success=result.exit_code == 0, data=result.model_dump())

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Command timed out after {timeout}s",
                data={"exit_code": -1, "stdout": "", "stderr": "TIMEOUT"},
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _is_dangerous(self, cmd: str) -> bool:
        if not self.allowlist_enabled:
            return False
        # Check denylist
        for bad in self.DENYLIST:
            if bad in cmd:
                return True
        # Check allowlist prefix (first word)
        first_word = shlex.split(cmd)[0] if cmd else ""
        # Allowlist check logic...
        return False  # Simplified
```

---

## 7. `specs/02_infra/tools/LSP_TOOL.py`

```python
# specs/02_infra/tools/LSP_TOOL.py
"""
Language Server Protocol (LSP) Client Tool.
CRITICAL for Agent Code Intelligence.
Allows Agent to: Go to Definition, Find References, Hover, Document Symbols.
Implementation: JSON-RPC 2.0 over stdio to language servers in Sandbox.
"""

from __future__ import annotations
import json
import asyncio
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Literal
from dataclasses import dataclass, field
from kernel.protocols import ITool, ToolResult, ILSPTool, ISandbox
from kernel.state import AgentState

LSP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["goto_definition", "find_references", "hover", "document_symbols", "workspace_symbols"],
        },
        "file_path": {"type": "string", "description": "Absolute or relative path in workspace"},
        "line": {"type": "integer", "description": "1-based line number"},
        "character": {"type": "integer", "description": "1-based character offset"},
        "query": {"type": "string", "description": "For workspace_symbols"},
    },
    "required": ["action"],
}

# --- LSP Client (Manages one client per language per sandbox) ---


@dataclass
class LSPClient:
    """Manages JSON-RPC connection to a language server process."""

    sandbox: ISandbox
    language_id: str
    command: List[str]  # e.g. ["typescript-language-server", "--stdio"]
    root_uri: str  # file:///workspace
    _process: Any = field(default=None, init=False)  # asyncio subprocess
    _request_id: int = field(default=0, init=False)
    _pending: Dict[int, asyncio.Future] = field(default_factory=dict, init=False)
    _initialized: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def start(self):
        # Launch process in sandbox via shell 'coproc' or dedicated exec?
        # E2B/Daytona allow running background processes.
        # Simplified: We assume sandbox has a daemon manager or we use a persistent shell.
        # BEST: Use Sandbox `process.start` (E2B) or `session.command` (Daytona) for background LSP.
        # Here we simulate via a persistent connection.
        pass

    async def _send_request(self, method: str, params: Dict) -> Any:
        self._request_id += 1
        req_id = self._request_id
        future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        # Write to stdin of LSP process
        # await self._process.stdin.write((json.dumps(msg) + "\r\n").encode())
        # ... read response from stdout in background task ...
        return await future

    async def initialize(self):
        if self._initialized:
            return
        await self._send_request(
            "initialize",
            {
                "processId": None,
                "rootUri": self.root_uri,
                "capabilities": {},
                "workspaceFolders": [{"uri": self.root_uri, "name": "workspace"}],
            },
        )
        await self._send_request("initialized", {})
        self._initialized = True

    async def shutdown(self):
        await self._send_request("shutdown", {})
        await self._send_request("exit", {})

    # --- High Level API ---
    async def goto_definition(self, file: str, line: int, char: int) -> List[Dict]:
        uri = Path(file).resolve().as_uri()
        return await self._send_request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": char - 1}},
        )

    async def find_references(self, file: str, line: int, char: int, include_declaration=True) -> List[Dict]:
        uri = Path(file).resolve().as_uri()
        return await self._send_request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": char - 1},
                "context": {"includeDeclaration": include_declaration},
            },
        )

    async def hover(self, file: str, line: int, char: int) -> Dict:
        uri = Path(file).resolve().as_uri()
        return await self._send_request(
            "textDocument/hover", {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": char - 1}}
        )

    async def document_symbols(self, file: str) -> List[Dict]:
        uri = Path(file).resolve().as_uri()
        return await self._send_request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})

    async def workspace_symbols(self, query: str) -> List[Dict]:
        return await self._send_request("workspace/symbol", {"query": query})


# --- Tool Wrapper ---


class LSPTool(ILSPTool):
    name = "lsp"
    description = "Code Intelligence: Go to Definition, Find References, Hover, Symbols. Use to understand codebase before editing."
    parameters_json_schema = LSP_TOOL_SCHEMA

    # Language -> LSP Command Mapping (Must match base image)
    LSP_COMMANDS = {
        "typescript": ["typescript-language-server", "--stdio"],
        "javascript": ["typescript-language-server", "--stdio"],
        "python": ["pylsp"],  # or basedpyright-langserver
        "go": ["gopls"],
        "rust": ["rust-analyzer"],
        "terraform": ["terraform-ls", "serve"],
    }

    def __init__(self, sandbox_manager: "SandboxManager"):
        self.sandbox_manager = sandbox_manager
        self._clients: Dict[str, Dict[str, LSPClient]] = {}  # sandbox_id -> {lang: client}

    async def _get_client(self, sandbox_id: str, language: str) -> LSPClient:
        if sandbox_id not in self._clients:
            self._clients[sandbox_id] = {}
        if language not in self._clients[sandbox_id]:
            cmd = self.LSP_COMMANDS.get(language)
            if not cmd:
                raise ValueError(f"No LSP for language: {language}")
            sbx = self.sandbox_manager.get_sandbox(sandbox_id)
            # Need a way to start background process in sandbox.
            # E2B: `sandbox.process.start(cmd, ...)` returns Process handle with stdin/stdout.
            # For spec, we assume `sbx.start_background_process(cmd)` returns (stdin_writer, stdout_reader).
            stdin, stdout = await sbx.start_background_process(cmd)
            client = LSPClient(sandbox=sbx, language_id=language, command=cmd, root_uri="file:///workspace")
            client._process = (stdin, stdout)  # Hack for spec
            # Start reader task
            asyncio.create_task(client._read_loop(stdout))
            await client.initialize()
            self._clients[sandbox_id][language] = client
        return self._clients[sandbox_id][language]

    def _detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        mapping = {
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".py": "python",
            ".go": "go",
            ".rs": "rust",
            ".tf": "terraform",
            ".hcl": "terraform",
        }
        return mapping.get(ext, "typescript")  # Default fallback

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args["action"]
        file_path = args.get("file_path")

        if action == "workspace_symbols":
            lang = "typescript"  # Default
            client = await self._get_client(sandbox_id, lang)
            result = await client.workspace_symbols(args["query"])
            return ToolResult(success=True, data={"symbols": result})

        if not file_path:
            return ToolResult(success=False, error="file_path required for this action")

        language = self._detect_language(file_path)
        client = await self._get_client(sandbox_id, language)

        line = args["line"]
        char = args["character"]

        try:
            if action == "goto_definition":
                result = await client.goto_definition(file_path, line, char)
            elif action == "find_references":
                result = await client.find_references(file_path, line, char)
            elif action == "hover":
                result = await client.hover(file_path, line, char)
            elif action == "document_symbols":
                result = await client.document_symbols(file_path)
            else:
                return ToolResult(success=False, error=f"Unknown action: {action}")

            return ToolResult(success=True, data=result)
        except Exception as e:
            return ToolResult(success=False, error=f"LSP Error: {str(e)}")
```

---

## 8. `specs/02_infra/tools/RAG_TOOL.py`

```python
# specs/02_infra/tools/RAG_TOOL.py
"""
Retrieval-Augmented Generation Tool.
Interfaces with Vector DB (Qdrant) and Graph DB (Kuzu).
Implements Hybrid Search (Vector + BM25) + Graph Traversal.
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, Literal
from pydantic import BaseModel, Field
from kernel.protocols import ITool, ToolResult, IRAGTool
from kernel.state import AgentState


# --- Config ---
class RagConfig(BaseModel):
    qdrant_url: str
    qdrant_api_key: Optional[str] = None
    default_collection: str = "code_chunks"
    default_limit: int = 10
    score_threshold: float = 0.65


# --- Data Models ---


class RetrievedChunk(BaseModel):
    id: str
    content: str
    score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Enriched fields
    intent: Optional[str] = None
    pattern: Optional[str] = None
    symbol_name: Optional[str] = None
    file_path: Optional[str] = None
    language: Optional[str] = None


class RagResult(BaseModel):
    chunks: List[RetrievedChunk]
    query: str
    total_found: int


RAG_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["retrieve", "retrieve_by_symbol", "retrieve_by_file", "graph_neighbors"]},
        "query": {"type": "string", "description": "Natural language query or symbol name"},
        "filters": {"type": "object", "description": "Metadata filters (language, framework, repo, tag)"},
        "top_k": {"type": "integer", "default": 10},
        "symbol_name": {"type": "string"},
        "file_path": {"type": "string"},
        "depth": {"type": "integer", "default": 1, "description": "Graph traversal depth"},
    },
    "required": ["action"],
}

# --- Implementation ---


class RagTool(IRAGTool):
    name = "rag"
    description = "Retrieve relevant code examples, patterns, docs, and architecture context from Knowledge Base."
    parameters_json_schema = RAG_TOOL_SCHEMA

    def __init__(self, config: RagConfig):
        self.config = config
        self._qdrant_client = None  # Lazy init
        self._kuzu_conn = None  # Lazy init

    def _get_qdrant(self):
        if not self._qdrant_client:
            from qdrant_client import AsyncQdrantClient

            self._qdrant_client = AsyncQdrantClient(url=self.config.qdrant_url, api_key=self.config.qdrant_api_key)
        return self._qdrant_client

    def _get_kuzu(self):
        if not self._kuzu_conn:
            import kuzu

            db = kuzu.Database("./kuzu_db")  # Or remote
            self._kuzu_conn = kuzu.Connection(db)
        return self._kuzu_conn

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args["action"]

        try:
            if action == "retrieve":
                result = await self._retrieve(args)
            elif action == "retrieve_by_symbol":
                result = await self._retrieve_by_symbol(args)
            elif action == "retrieve_by_file":
                result = await self._retrieve_by_file(args)
            elif action == "graph_neighbors":
                result = await self._graph_neighbors(args)
            else:
                return ToolResult(success=False, error=f"Unknown action: {action}")

            return ToolResult(success=True, data=result.model_dump())
        except Exception as e:
            return ToolResult(success=False, error=f"RAG Error: {str(e)}")

    async def _retrieve(self, args: Dict) -> RagResult:
        query = args["query"]
        top_k = args.get("top_k", self.config.default_limit)
        filters = args.get("filters", {})

        # 1. Embed Query (Use fast local model or API)
        # For spec, assume `embed(query)` returns vector
        vector = await self._embed(query)

        # 2. Hybrid Search (Vector + BM25)
        # Qdrant supports hybrid via `prefetch` (Vector) + `query` (BM25) or Fusion API
        # Simplified: Vector search with filters
        client = self._get_qdrant()

        # Build Filter
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

        must = []
        for k, v in filters.items():
            if isinstance(v, list):
                must.append(FieldCondition(key=k, match=MatchAny(any=v)))
            else:
                must.append(FieldCondition(key=k, match=MatchValue(value=v)))

        # Add vertical filter automatically from state context if available
        # vertical_id = state.get("vertical_id") -> filters["vertical_id"] = vertical_id

        search_result = await client.query_points(
            collection_name=self.config.default_collection,
            query=vector,
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            with_payload=True,
            score_threshold=self.config.score_threshold,
        )

        chunks = [
            RetrievedChunk(id=hit.id, content=hit.payload.pop("content", ""), score=hit.score, metadata=hit.payload)
            for hit in search_result.points
        ]
        return RagResult(chunks=chunks, query=query, total_found=len(chunks))

    async def _retrieve_by_symbol(self, args: Dict) -> RagResult:
        """Exact match on symbol_name + file_path context."""
        symbol = args["symbol_name"]
        file_path = args.get("file_path", "")
        client = self._get_qdrant()

        res = await client.query_points(
            collection_name=self.config.default_collection,
            query_filter=Filter(
                must=[
                    FieldCondition(key="symbol_name", match=MatchValue(value=symbol)),
                    FieldCondition(key="file_path", match=MatchValue(value=file_path)),  # Optional
                ]
            ),
            limit=5,
            with_payload=True,
        )
        return RagResult(chunks=[...], query=symbol, total_found=len(res.points))

    async def _retrieve_by_file(self, args: Dict) -> RagResult:
        """Get ALL chunks for a specific file (for context loading)."""
        file_path = args["file_path"]
        client = self._get_qdrant()
        res = await client.query_points(
            collection_name=self.config.default_collection,
            query_filter=Filter(must=[FieldCondition(key="file_path", match=MatchValue(value=file_path))]),
            limit=100,  # Get all chunks for file
            with_payload=True,
        )
        # Sort by line number
        chunks = sorted([...], key=lambda c: c.metadata.get("start_line", 0))
        return RagResult(chunks=chunks, query=file_path, total_found=len(chunks))

    async def _graph_neighbors(self, args: Dict) -> RagResult:
        """Traverse Code Graph (Callers/Callees/Imports)."""
        symbol = args["symbol_name"]
        file_path = args.get("file_path")
        depth = args.get("depth", 1)

        conn = self._get_kuzu()
        # Cypher query for Kuzu
        # MATCH (n:Function {name: $symbol, file: $file}) -[:CALLS*1..$depth]-> (m) RETURN m
        query = """
        MATCH (n {name: $symbol, file_path: $file}) -[r:CALLS|IMPORTS|INHERITS*1..$depth]-> (m)
        RETURN m.name, m.type, m.file_path, m.code, m.intent
        LIMIT 50
        """
        result = conn.execute(query, {"symbol": symbol, "file": file_path, "depth": depth})

        chunks = []
        while result.has_next():
            row = result.get_next()
            chunks.append(
                RetrievedChunk(
                    id=f"{row[2]}:{row[0]}",
                    content=row[3] or "",
                    score=1.0,
                    metadata={"symbol_name": row[0], "symbol_type": row[1], "file_path": row[2]},
                    intent=row[4],
                )
            )
        return RagResult(chunks=chunks, query=f"graph:{symbol}", total_found=len(chunks))

    async def _embed(self, text: str) -> List[float]:
        # Call Embedding Model (Local SentenceTransformer or API)
        # Use `nomic-embed-text` or `text-embedding-3-small`
        # Cache embeddings for repeated queries in same run.
        return [0.0] * 768  # Placeholder
```

---

## 9. `specs/02_infra/llm_gateway/LLM_CLIENT.py`

```python
# specs/02_infra/llm_gateway/LLM_CLIENT.py
"""
LLM Client Wrapper.
- Unified Interface (LiteLLM Router).
- Structured Output via Instructor.
- Cost Tracking & Budget Enforcement.
- Automatic Retries / Fallbacks.
- Prompt Caching Headers (Anthropic/OpenAI).
"""

from __future__ import annotations
import json
import time
import uuid
from typing import Dict, Any, Optional, List, AsyncIterator, Type
from pydantic import BaseModel
import instructor
from litellm import acompletion, acompletion_cost
from litellm.types.utils import ModelResponse

from kernel.protocols import ILLMClient, LLMMessage, LLMResponse
from kernel.config import settings
from kernel.state import TokenUsage


class LiteLLMClient(ILLMClient):
    def __init__(self):
        # Patch LiteLLM with Instructor for structured output
        self.client = instructor.from_litellm(acompletion, mode=instructor.Mode.JSON)
        # Configure LiteLLM Router (from config.yaml)
        # litellm.router = Router(model_list=settings.llm.model_list)

    async def achat(
        self,
        messages: List[LLMMessage],
        model: str,
        response_model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        start = time.perf_counter()

        # Convert to LiteLLM format
        llm_messages = [m.model_dump(exclude_none=True) for m in messages]

        try:
            if response_model:
                # Instructor handles parsing & validation
                parsed_obj = await self.client.chat.completions.create(
                    model=model,
                    messages=llm_messages,
                    response_model=response_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                # Extract raw response for usage tracking
                raw_resp = parsed_obj._raw_response if hasattr(parsed_obj, "_raw_response") else None
                content = parsed_obj.model_dump_json()
            else:
                raw_resp = await acompletion(
                    model=model, messages=llm_messages, temperature=temperature, max_tokens=max_tokens, **kwargs
                )
                content = raw_resp.choices[0].message.content
                parsed_obj = None

            # Cost Calculation
            cost = 0.0
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            if raw_resp:
                usage = raw_resp.usage.model_dump() if raw_resp.usage else usage
                try:
                    cost = acompletion_cost(model=model, usage=raw_resp.usage)
                except:
                    pass  # Cost calc failed

            return LLMResponse(
                content=content,
                tool_calls=None,  # Handled by Instructor
                usage=usage,
                model=model,
                cost_usd=cost,
            )
        except Exception as e:
            # Log error, re-raise for Node to handle (retry logic)
            raise RuntimeError(f"LLM Call failed ({model}): {str(e)}") from e

    async def astream_chat(self, messages: List[LLMMessage], model: str, **kwargs) -> AsyncIterator[str]:
        llm_messages = [m.model_dump(exclude_none=True) for m in messages]
        stream = await acompletion(model=model, messages=llm_messages, stream=True, **kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def estimate_tokens(self, messages: List[LLMMessage], model: str) -> int:
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model(model.replace("router/", ""))
        except:
            encoding = tiktoken.get_encoding("cl100k_base")

        total = 0
        for m in messages:
            total += len(encoding.encode(m.content))
            # Add overhead for role/tags
            total += 4
        return total
```

---

## 10. `specs/02_infra/tools/GIT_TOOL.py` (Bonus: Version Control in Sandbox)

```python
# specs/02_infra/tools/GIT_TOOL.py
"""
Git Tool for Sandbox.
Enables: Commit history, Diff generation, Branch management, PR preparation.
"""

from __future__ import annotations
from typing import Dict, Any, List
from kernel.protocols import ITool, ToolResult
from kernel.state import AgentState

GIT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["init", "status", "diff", "add", "commit", "log", "branch", "push"]},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Git subcommand args"},
        "message": {"type": "string", "description": "Commit message"},
    },
    "required": ["action"],
}


class GitTool(ITool):
    name = "git"
    description = "Git operations in sandbox workspace. Use for version control, diffs, commits."
    parameters_json_schema = GIT_TOOL_SCHEMA

    def __init__(self, sandbox_manager: "SandboxManager"):
        self.sandbox_manager = sandbox_manager

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args["action"]
        git_args = args.get("args", [])
        message = args.get("message")

        sbx = self.sandbox_manager.get_sandbox(sandbox_id)

        try:
            if action == "init":
                cmd = "git init && git config user.email 'agent@autogen.local' && git config user.name 'AutoGen Agent'"
            elif action == "add":
                cmd = f"git add {' '.join(git_args)}"
            elif action == "commit":
                if not message:
                    return ToolResult(success=False, error="Message required for commit")
                cmd = f'git commit -m "{message}"'
            elif action == "diff":
                cmd = f"git diff {' '.join(git_args)}"
            elif action == "status":
                cmd = "git status --porcelain"
            elif action == "log":
                cmd = f"git log --oneline -n 20 {' '.join(git_args)}"
            elif action == "branch":
                cmd = f"git branch {' '.join(git_args)}"
            elif action == "push":
                cmd = f"git push {' '.join(git_args)}"
            else:
                return ToolResult(success=False, error=f"Unknown git action: {action}")

            res = await sbx.exec(cmd, workdir="/workspace")
            return ToolResult(success=res.exit_code == 0, data=res.model_dump())
        except Exception as e:
            return ToolResult(success=False, error=str(e))
```

---

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №2)

> **CONTEXT FOR NEXT STEP (Vertical Spec):**
> Ты реализуешь `specs/02_infra/`.
> 1. Создай `kernel/tools/` с файлами: `registry.py`, `filesystem.py`, `shell.py`, `lsp.py`, `rag.py`, `git.py`.
>    *   Все классы должны наследовать протоколы из `kernel.protocols`.
>    *   `ToolRegistry` должен инжектироваться в ноды через `config["configurable"]["tool_registry"]`.
> 2. Создай `kernel/sandbox/manager.py` с классом `SandboxManager` (пул, TTL, affinity).
>    *   Реализуй `E2BSandbox` адаптер для `ISandbox`.
>    *   Напиши `Dockerfile.sandbox.base` и `Dockerfile.sandbox.saas_web` (как в спеке).
> 3. Создай `kernel/llm/client.py` с `LiteLLMClient`.
> 4. **Интеграционный тест (`tests/infra/test_tools_integration.py`):**
>    *   Запуск `SandboxManager` (mock или реальный E2B если есть ключи).
>    *   Регистрация всех инструментов в `ToolRegistry`.
>    *   Тест `filesystem.write -> read -> glob -> grep`.
>    *   Тест `shell.exec("pnpm --version")` -> успех.
>    *   Тест `lsp.goto_definition` на простом TS файле (проверка, что LSP сервер запускается и отвечает).
>    *   Тест `rag.retrieve` (мок Qdrant/Kuzu).

---

### ✅ Чек-лист готовности Инфраструктуры (Definition of Done для Ответа №2)

- [ ] `Dockerfile.sandbox.base` собирается без ошибок (`docker build -t autogen-base .`).
- [ ] `E2BSandbox` (или `DockerSandbox`) проходит все методы `ISandbox` в тестах.
- [ ] `ToolRegistry` выдает корректные OpenAI Function Schemas для всех инструментов.
- [ ] `LSPTool` успешно подключается к `typescript-language-server` в песочнице и возвращает определения.
- [ ] `RagTool` выполняет запрос к моку Qdrant/Kuzu и возвращает `RagResult`.
- [ ] `ShellTool` блокирует опасные команды (`rm -rf /`) и пропускает разрешенные (`pnpm test`).
- [ ] Токены и стоимость трекаются в `LLMClient` и возвращаются в `LLMResponse`.

---

---

# Ответ №3: Skills Framework Spec — Система Навыков (Skills), Манифесты, Плагины

Это **руки** системы. Скиллы позволяют агенту не «писать код с нуля» (медленно, с галлюцинациями), а **инстанцировать проверенные паттерны** (мгновенно, детерминированно).

Сохрани в структуру:
```text
specs/03_skills/
├── SKILL_SPEC.md
├── MANIFEST_SCHEMA.yaml
├── skill/
│   ├── SKILL_DEFINITION.py
│   ├── SKILL_EXECUTOR.py
│   ├── TEMPLATE_ENGINE.py
│   └── HOOKS.py
├── registry/
│   ├── SKILL_REGISTRY.py
│   ├── VERTICAL_LOADER.py
│   └── MARKETPLACE_CLIENT.py
└── examples/
    ├── init_nextjs_app_router/
    │   ├── skill.yaml
    │   ├── templates/
    │   │   ├── package.json.j2
    │   │   ├── tsconfig.json.j2
    │   │   └── src/app/layout.tsx.j2
    │   └── hooks.py
    └── add_prisma_model/
        ├── skill.yaml
        ├── templates/
        │   └── prisma/schema.prisma.j2
        └── hooks.py
```

---

## 1. `specs/03_skills/SKILL_SPEC.md`

````markdown
# Skills Framework Specification

**Version:** 1.0
**Core Concept:** **Skill = Template Bundle + Render Logic + Validation Gates.**
Skills are the atomic units of *deterministic* generation. They produce boilerplate, config, scaffolding, migrations — everything that follows a pattern.

---

## 1. Philosophy: Why Skills?

| Approach | Pros | Cons | Use Case |
| :--- | :--- | :--- | :--- |
| **Pure LLM Generation** | Flexible, handles novel logic | Slow, hallucinates imports, inconsistent style, token-heavy | Business logic, algorithms, complex conditionals |
| **Skills (Templates)** | **Instant**, **Deterministic**, **Type-safe**, **Reviewable**, **Versioned** | Rigid, requires maintenance | Project init, CRUD scaffolds, Config files, Dockerfiles, CI pipelines, Boilerplate patterns |

**Rule of Thumb:** If you can write a **Jinja2 template** for it with < 5 conditional branches → **Make it a Skill**. If it requires "thinking" → **LLM Agent (Coder Node)**.

---

## 2. Skill Anatomy

```
skill_id/
├── skill.yaml              # Manifest (Metadata, Inputs, Dependencies, Gates)
├── templates/              # Jinja2 Templates (.j2 files preserving directory structure)
│   ├── package.json.j2
│   └── src/...
├── hooks.py                # Optional Python Logic (Pre/Post Render, Validation, LSP edits)
└── assets/                 # Static files (images, binary blobs, large configs)
```

---

## 3. Skill Lifecycle (Execution Flow)

```mermaid
sequenceDiagram
    participant CoderNode as Coder Node
    participant SkillRegistry as Skill Registry
    participant SkillExecutor as Skill Executor
    participant Sandbox as Sandbox (FS/Shell)
    participant LSP as LSP Server

    CoderNode->>SkillRegistry: get_executor(skill_id, inputs)
    SkillRegistry->>SkillExecutor: Load SkillDef, Validate Inputs (JSON Schema)
    SkillExecutor->>SkillExecutor: 1. PRE_RENDER Hook (modify inputs, fetch deps)
    SkillExecutor->>TemplateEngine: 2. RENDER (Jinja2 -> FileChanges[])
    SkillExecutor->>Sandbox: 3. APPLY (Write Files)
    SkillExecutor->>Sandbox: 4. POST_SCRIPTS (Shell: install deps, format, lint)
    SkillExecutor->>LSP: 5. SYNC (Notify LSP of file changes - optional)
    SkillExecutor->>Sandbox: 6. VALIDATION (Run Validation Commands)
    SkillExecutor-->>CoderNode: Result: FileChanges[] + VerificationResult
```

---

## 4. Skill Composition & Dependencies

Skills can depend on other skills (DAG).
*   `init_nextjs` → provides `package.json`, `tsconfig`.
*   `add_tailwind` → **depends_on**: `init_nextjs`. Modifies `package.json`, `tailwind.config.ts`, `globals.css`.
*   `add_shadcn_ui` → **depends_on**: `add_tailwind`. Runs `pnpm dlx shadcn-ui add button`.

**Dependency Resolution:** Handled by Planner (Task Graph). Skill Executor assumes dependencies are already applied.

---

## 5. Versioning & Marketplace

*   **SemVer:** `skill.yaml` has `version: "1.2.0"`.
*   **Registry:** Local FS (`verticals/<name>/skills/`) + Remote (GitHub Releases / OCI Registry / Custom API).
*   **Lockfile:** `skills.lock.json` in project root (generated by Planner) pins exact versions for reproducibility.
*   **Upgrade Command:** `autogen skill upgrade <skill_id> @latest` → updates lockfile, runs migration hooks.
````

---

## 2. `specs/03_skills/MANIFEST_SCHEMA.yaml`

> **Схема `skill.yaml` (Source of Truth для скилла).**

```yaml
# specs/03_skills/MANIFEST_SCHEMA.yaml
# JSON Schema для валидации skill.yaml (используется в SkillLoader)

$id: "https://autogen.dev/schemas/skill-manifest-v1.json"
title: Skill Manifest
type: object
required: [id, name, version, description, inputs, template_dir, entrypoint]
properties:
  # --- Identity ---
  id:
    type: string
    pattern: '^[a-z0-9-_]+$'
    description: "Unique ID (npm-style: 'init_nextjs', 'add_prisma_model')"
  name:
    type: string
    description: "Human readable name"
  version:
    type: string
    pattern: '^\d+\.\d+\.\d+(-[a-z0-9.]+)?$'
    description: "SemVer. Major breaking changes = new skill ID."
  description:
    type: string
    description: "What this skill does, for Planner/LLM context."
  
  # --- Categorization ---
  category:
    type: string
    enum: [scaffold, config, feature, integration, migration, test, ci_cd, docs]
    default: scaffold
  tags:
    type: array
    items: {type: string}
    description: "Search tags: 'auth', 'database', 'ui', 'aws'"

  # --- Interface (Contract) ---
  inputs:
    type: object
    description: "JSON Schema for skill parameters. Used by Planner & UI."
    additionalProperties: 
      type: object
      properties:
        type: {type: string, enum: [string, integer, boolean, array, object]}
        description: {type: string}
        default: {}
        enum: {type: array}
        required: {type: array, items: {type: string}}
  
  # --- Dependencies ---
  depends_on:
    type: array
    items: {type: string}
    description: "List of Skill IDs that MUST be executed before this one."
  
  provides:
    type: array
    items: {type: string}
    description: "Capabilities provided (e.g. 'nextjs_app_router', 'prisma_client'). Used for capability matching."

  # --- Execution ---
  template_dir:
    type: string
    default: "templates"
    description: "Relative path to Jinja2 templates root."
  
  entrypoint:
    type: string
    default: "hooks.py"
    description: "Python module with hooks (pre_render, post_render, validate)."
  
  # Commands to run AFTER files are written (in Sandbox shell)
  post_scripts:
    type: array
    items: {type: string}
    description: "e.g. 'pnpm install', 'pnpm run format', 'prisma generate'"
    default: []
  
  # Environment variables needed for post_scripts
  env:
    type: object
    additionalProperties: {type: string}
    default: {}

  # --- Validation (Quality Gates specific to this skill) ---
  validation:
    type: array
    items: {type: string}
    description: "Commands that MUST pass for skill to be considered successful."
    default: []
    examples:
      - "pnpm run typecheck"
      - "pnpm run lint --max-warnings=0"
      - "test -f prisma/schema.prisma"

  # --- Output Metadata (for downstream skills/agents) ---
  outputs:
    type: object
    description: "Structured data returned to State for other skills."
    additionalProperties: {type: string}
    example:
      nextjs_version: "14.2.0"
      package_manager: "pnpm"
      router_type: "app_router"

  # --- Compatibility ---
  compatible_verticals:
    type: array
    items: {type: string}
    description: "Vertical IDs this skill works with. Empty = all."
    default: []
  
  min_kernel_version:
    type: string
    default: "1.0.0"
```

---

## 3. `specs/03_skills/skill/SKILL_DEFINITION.py`

```python
# specs/03_skills/skill/SKILL_DEFINITION.py
"""
Pydantic Models for Skill Definition.
Loaded from skill.yaml + hooks.py discovery.
"""

from __future__ import annotations
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Awaitable, Type
from pydantic import BaseModel, Field, ConfigDict
from kernel.state import FileChange, VerificationGateResult

# --- Input/Output Schemas ---


class SkillInputSchema(BaseModel):
    """Dynamic schema generated from skill.yaml 'inputs'."""

    # We use a generic dict validated against JSON Schema at runtime
    model_config = ConfigDict(extra="allow")


class SkillOutput(BaseModel):
    """Structured output returned to Kernel State."""

    file_changes: List[FileChange] = []
    outputs: Dict[str, Any] = {}  # e.g. {"nextjs_version": "14"}
    verification_results: List[VerificationGateResult] = []
    metadata: Dict[str, Any] = {}  # Arbitrary data for next skills


# --- Hook Types ---


class HookContext(BaseModel):
    """Context passed to all hooks."""

    skill_id: str
    skill_dir: Path
    inputs: Dict[str, Any]
    sandbox: Any  # ISandbox instance
    sandbox_id: str
    workspace: str
    state: Dict[str, Any]  # Full AgentState (read-only for pre-hooks)
    tool_registry: Any  # ToolRegistry instance


# Hook Signatures
PreRenderHook = Callable[[HookContext], Awaitable[Dict[str, Any]]]  # Returns modified inputs
PostRenderHook = Callable[[HookContext, List[FileChange]], Awaitable[List[FileChange]]]  # Can modify FileChanges
ValidationHook = Callable[[HookContext], Awaitable[List[VerificationGateResult]]]  # Custom validation

# --- Main Skill Definition ---


class SkillDef(BaseModel):
    """Loaded, validated, ready-to-execute Skill."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # From skill.yaml
    id: str
    name: str
    version: str
    description: str
    category: str = "scaffold"
    tags: List[str] = []
    inputs_schema: Dict[str, Any] = Field(default_factory=dict)  # Raw JSON Schema
    depends_on: List[str] = []
    provides: List[str] = []
    template_dir: str = "templates"
    entrypoint: str = "hooks.py"
    post_scripts: List[str] = []
    env: Dict[str, str] = {}
    validation: List[str] = []
    outputs_schema: Dict[str, Any] = {}
    compatible_verticals: List[str] = []
    min_kernel_version: str = "1.0.0"

    # Runtime (Loaded from hooks.py)
    _pre_render: Optional[PreRenderHook] = Field(default=None, exclude=True)
    _post_render: Optional[PostRenderHook] = Field(default=None, exclude=True)
    _validate: Optional[ValidationHook] = Field(default=None, exclude=True)

    @classmethod
    def load_from_dir(cls, skill_dir: Path) -> "SkillDef":
        """Load skill.yaml, discover hooks, validate structure."""
        manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"skill.yaml not found in {skill_dir}")

        import yaml

        data = yaml.safe_load(manifest_path.read_text())

        # Load Hooks
        hooks_path = skill_dir / data.get("entrypoint", "hooks.py")
        hooks = {}
        if hooks_path.exists():
            spec = importlib.util.spec_from_file_location(f"skill_hooks_{data['id']}", hooks_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Discover hooks by name convention
            for name in ["pre_render", "post_render", "validate"]:
                if hasattr(module, name) and inspect.iscoroutinefunction(getattr(module, name)):
                    hooks[f"_{name}"] = getattr(module, name)

        return cls(**data, **hooks)

    def validate_inputs(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Validate inputs against JSON Schema (using jsonschema library)."""
        from jsonschema import validate, ValidationError

        try:
            validate(instance=inputs, schema=self.inputs_schema)
            return inputs
        except ValidationError as e:
            raise ValueError(f"Skill '{self.id}' input validation failed: {e.message}") from e
```

---

## 4. `specs/03_skills/skill/TEMPLATE_ENGINE.py`

```python
# specs/03_skills/skill/TEMPLATE_ENGINE.py
"""
Jinja2 Template Engine with Custom Filters & Globals for Code Gen.
Features:
- Strict Undefined (catch typos in templates)
- Custom Filters: to_json, to_yaml, slugify, class_name, snake_case, indent
- Globals: `now()`, `uuid()`, `env()`, `skill_output(skill_id)`
- Template Inheritance & Includes
- Streaming for large files (optional)
"""

from __future__ import annotations
import json
import yaml
import re
import uuid as uuid_lib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from jinja2 import Environment, FileSystemLoader, StrictUndefined, meta, TemplateError
from jinja2.filters import FILTERS as JINJA_DEFAULT_FILTERS

from kernel.state import FileChange

# --- Custom Filters ---


def to_json(value: Any, indent: int = 2) -> str:
    return json.dumps(value, indent=indent, ensure_ascii=False, default=str)


def to_yaml(value: Any) -> str:
    return yaml.dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False)


def slugify(value: str) -> str:
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", value.lower())).strip("-")


def class_name(value: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[-_\s]+", value) if w)


def snake_case(value: str) -> str:
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def pascal_case(value: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[-_\s]+", value) if w)


def indent(text: str, spaces: int = 4) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())


def regex_replace(text: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, text)


def file_exists(path: str, base_dir: Path) -> bool:
    return (base_dir / path).exists()


# --- Globals ---


def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.utcnow().strftime(fmt)


def uuid_short() -> str:
    return uuid_lib.uuid4().hex[:8]


def env(var: str, default: str = "") -> str:
    import os

    return os.getenv(var, default)


# --- Engine ---


class TemplateEngine:
    def __init__(self, template_dirs: List[Path], skill_outputs: Dict[str, Dict]):
        """
        template_dirs: List of directories to search (Skill dir, Vertical dir, Global dir).
        skill_outputs: Outputs from previously executed skills in this run {skill_id: outputs_dict}.
        """
        self.loader = FileSystemLoader([str(d) for d in template_dirs])
        self.env = Environment(
            loader=self.loader,
            undefined=StrictUndefined,  # Fail fast on missing vars
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            autoescape=False,  # Code gen usually doesn't need HTML escaping
        )
        self._register_filters()
        self._register_globals(skill_outputs)

    def _register_filters(self):
        self.env.filters.update(
            {
                "to_json": to_json,
                "to_yaml": to_yaml,
                "slugify": slugify,
                "class_name": class_name,
                "snake_case": snake_case,
                "pascal_case": pascal_case,
                "indent": indent,
                "regex_replace": regex_replace,
            }
        )
        # Keep default jinja filters too
        for k, v in JINJA_DEFAULT_FILTERS.items():
            if k not in self.env.filters:
                self.env.filters[k] = v

    def _register_globals(self, skill_outputs: Dict[str, Dict]):
        self.env.globals.update(
            {
                "now": now,
                "uuid": uuid_short,
                "env": env,
                "skill_output": lambda skill_id, key, default=None: skill_outputs.get(skill_id, {}).get(key, default),
                "has_skill": lambda skill_id: skill_id in skill_outputs,
            }
        )

    def render_string(self, template_str: str, context: Dict[str, Any]) -> str:
        try:
            template = self.env.from_string(template_str)
            return template.render(**context)
        except TemplateError as e:
            raise RuntimeError(f"Template render error: {e}") from e

    def render_file(self, template_path: Path, context: Dict[str, Any]) -> str:
        try:
            template = self.env.get_template(str(template_path))
            return template.render(**context)
        except TemplateError as e:
            raise RuntimeError(f"Template render error in {template_path}: {e}") from e

    def list_templates(self) -> List[str]:
        return self.env.list_templates()

    def get_template_ast(self, template_path: Path):
        """For static analysis / variable extraction."""
        source = self.loader.get_source(self.env, str(template_path))[0]
        return self.env.parse(source)

    def find_undeclared_variables(self, template_path: Path) -> set:
        ast = self.get_template_ast(template_path)
        return meta.find_undeclared_variables(ast)


# --- High Level Render Function ---


async def render_skill_templates(
    skill_dir: Path, inputs: Dict[str, Any], skill_outputs: Dict[str, Dict], workspace_root: Path
) -> List[FileChange]:
    """
    Renders all *.j2 files in skill_dir/template_dir preserving directory structure.
    Strips .j2 extension.
    """
    template_dir = skill_dir / "templates"
    if not template_dir.exists():
        return []

    engine = TemplateEngine(
        template_dirs=[template_dir, workspace_root],  # Allow inheriting from workspace
        skill_outputs=skill_outputs,
    )

    context = {**inputs, "skill_dir": str(skill_dir), "workspace_root": str(workspace_root)}

    changes = []
    for j2_path in template_dir.rglob("*.j2"):
        relative_path = j2_path.relative_to(template_dir)
        # Remove .j2 extension
        target_path = relative_path.with_suffix("")

        content = engine.render_file(j2_path, context)

        # Determine action: create vs update (check if exists in workspace)
        action = "create"
        if (workspace_root / target_path).exists():
            action = "update"

        changes.append(FileChange(path=str(target_path), content=content, action=action))
    return changes
```

---

## 5. `specs/03_skills/skill/HOOKS.py`

```python
# specs/03_skills/skill/HOOKS.py
"""
Standard Hook Interface & Built-in Helpers.
Hooks allow imperative logic where templates are insufficient:
- Complex conditional file generation
- Interacting with LSP (e.g., add import to existing file)
- Running shell commands not covered by post_scripts
- Modifying existing files (AST manipulation via Tree-sitter/LSP)
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional
from pathlib import Path
from kernel.state import FileChange, VerificationGateResult, VerificationGateStatus
from kernel.protocols import ISandbox, ITool
from .SKILL_DEFINITION import HookContext

# --- Example Hook Implementation (for reference) ---

# File: verticals/saas_web/skills/add_prisma_model/hooks.py


async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    """
    Runs BEFORE template rendering.
    Use to: fetch data, compute derived values, validate complex rules.
    Return dict to MERGE into inputs.
    """
    inputs = ctx.inputs
    model_name = inputs["model_name"]

    # Auto-generate table name if not provided
    if "table_name" not in inputs:
        inputs["table_name"] = snake_case(model_name) + "s"

    # Check if model already exists (via LSP/Grep)
    # This prevents overwriting
    # skill_output = ctx.state.get("skill_outputs", {}).get("init_prisma")
    # ...

    return inputs


async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    """
    Runs AFTER templates rendered but BEFORE files written to sandbox.
    Use to: modify generated FileChanges, add extra files based on logic.
    """
    # Example: If adding a relation, also update the related model file
    if ctx.inputs.get("add_relation"):
        related_model = ctx.inputs["related_model"]
        # Use LSP Tool to find related model file and insert relation field
        # lsp_tool = ctx.tool_registry.get("lsp")
        # ...
        pass
    return file_changes


async def validate(ctx: HookContext) -> List[VerificationGateResult]:
    """
    Runs AFTER post_scripts.
    Use for: Custom validation logic beyond shell commands.
    Returns list of VerificationGateResult.
    """
    results = []
    # Example: Check Prisma syntax via `prisma validate`
    # This is already covered by post_scripts usually.
    # But here we can do semantic checks:
    # "Ensure every model has @id field"
    # Parse generated schema.prisma with Tree-sitter...
    return results


# --- Built-in Helpers for Hooks ---


class HookHelpers:
    @staticmethod
    async def run_shell(ctx: HookContext, command: str, workdir: str = None) -> Dict:
        shell_tool = ctx.tool_registry.get("shell")
        return (
            await shell_tool.execute(ctx.sandbox_id, {"command": command, "workdir": workdir or ctx.workspace}, {})
        ).data

    @staticmethod
    async def lsp_add_import(ctx: HookContext, file_path: str, import_stmt: str) -> List[FileChange]:
        """Use LSP to safely add import to existing file."""
        # 1. Read file
        fs_tool = ctx.tool_registry.get("filesystem")
        read_res = await fs_tool.execute(ctx.sandbox_id, {"action": "read", "path": file_path}, {})
        if not read_res.success:
            return []
        content = read_res.data["content"]

        # 2. Check if already exists
        if import_stmt in content:
            return []

        # 3. Use LSP to find best insertion point (after last import)
        lsp_tool = ctx.tool_registry.get("lsp")
        # This is complex - usually better to use Tree-sitter in hook directly
        # Simplified: prepend to file
        new_content = import_stmt + "\n" + content
        return [FileChange(path=file_path, content=new_content, action="update")]

    @staticmethod
    async def tree_sitter_edit(ctx: HookContext, file_path: str, edits: List[Dict]) -> FileChange:
        """Perform precise AST edits using Tree-sitter (Python side, reads file, edits, returns change)."""
        # Requires file content locally. Read via FS tool.
        fs_tool = ctx.tool_registry.get("filesystem")
        read_res = await fs_tool.execute(ctx.sandbox_id, {"action": "read", "path": file_path}, {})
        if not read_res.success:
            raise FileNotFoundError(file_path)

        code = read_res.data["content"]
        # Apply edits using tree-sitter (see tree_sitter_helpers.py)
        # new_code = apply_edits(code, edits)
        # return FileChange(path=file_path, content=new_code, action="update")
        pass
```

---

## 6. `specs/03_skills/skill/SKILL_EXECUTOR.py`

```python
# specs/03_skills/skill/SKILL_EXECUTOR.py
"""
Skill Executor: Orchestrates the full Skill Lifecycle.
Input: SkillDef, Inputs, Context (Sandbox, State, Tools)
Output: SkillOutput (FileChanges, Outputs, VerificationResults)
"""

from __future__ import annotations
import time
from typing import Dict, Any, List, Optional
from pathlib import Path

from kernel.protocols import ISandbox, ISkillExecutor, SkillDef, VerificationGateResult, VerificationGateStatus
from kernel.state import FileChange, AgentState
from kernel.tools.registry import ToolRegistry
from .TEMPLATE_ENGINE import render_skill_templates
from .SKILL_DEFINITION import HookContext


class SkillExecutor(ISkillExecutor):
    def __init__(self, skill_def: SkillDef, tool_registry: ToolRegistry):
        self.skill_def = skill_def
        self.tool_registry = tool_registry

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, inputs: Dict[str, Any], state: AgentState
    ) -> Dict[str, Any]:  # Returns dict matching SkillOutput

        start_time = time.time()
        workspace_path = Path(workspace)
        skill_dir = Path(state["vertical_manifest"]["skills_dir"]) / self.skill_def.id

        # 1. Validate Inputs
        validated_inputs = self.skill_def.validate_inputs(inputs)

        # 2. Prepare Context for Hooks
        hook_ctx = HookContext(
            skill_id=self.skill_def.id,
            skill_dir=skill_dir,
            inputs=validated_inputs,
            sandbox=sandbox,
            sandbox_id=sandbox_id,
            workspace=workspace,
            state=state,
            tool_registry=self.tool_registry,
        )

        # 3. PRE_RENDER HOOK
        if self.skill_def._pre_render:
            extra_inputs = await self.skill_def._pre_render(hook_ctx)
            validated_inputs.update(extra_inputs)

        # 4. RENDER TEMPLATES
        # Collect outputs from previous skills for `skill_output()` global
        prev_outputs = state.get("skill_outputs", {})

        file_changes = await render_skill_templates(
            skill_dir=skill_dir, inputs=validated_inputs, skill_outputs=prev_outputs, workspace_root=workspace_path
        )

        # 5. POST_RENDER HOOK
        if self.skill_def._post_render:
            file_changes = await self.skill_def._post_render(hook_ctx, file_changes)

        # 6. APPLY FILE CHANGES TO SANDBOX
        if file_changes:
            await sandbox.write_files(file_changes)

        # 7. RUN POST_SCRIPTS (Shell commands)
        script_results = []
        for script in self.skill_def.post_scripts:
            # Interpolate inputs into script
            rendered_script = self._interpolate(script, validated_inputs)
            shell_tool = self.tool_registry.get("shell")
            res = await shell_tool.execute(
                sandbox_id,
                {"command": rendered_script, "workdir": workspace, "env": self.skill_def.env, "timeout": 300},
                state,
            )
            script_results.append({"script": script, "result": res.data})
            if not res.success:
                # Don't fail fast here, let validation gates catch it
                pass

        # 8. VALIDATION (Shell commands from skill.yaml)
        validation_results = []
        for val_cmd in self.skill_def.validation:
            rendered_cmd = self._interpolate(val_cmd, validated_inputs)
            shell_tool = self.tool_registry.get("shell")
            res = await shell_tool.execute(
                sandbox_id, {"command": rendered_cmd, "workdir": workspace, "timeout": 120}, state
            )
            vr = VerificationGateResult(
                gate_id=f"{self.skill_def.id}_validation",
                name=f"Validation: {rendered_cmd[:50]}",
                status=VerificationGateStatus.PASSED if res.success else VerificationGateStatus.FAILED,
                command=rendered_cmd,
                exit_code=res.data.get("exit_code", -1),
                stdout=res.data.get("stdout", ""),
                stderr=res.data.get("stderr", ""),
                duration_ms=res.data.get("duration_ms", 0),
            )
            validation_results.append(vr)

        # 9. CUSTOM VALIDATION HOOK
        if self.skill_def._validate:
            custom_results = await self.skill_def._validate(hook_ctx)
            validation_results.extend(custom_results)

        # 10. Prepare Outputs
        outputs = self.skill_def.outputs_schema.copy()
        # Allow hooks to inject outputs via context? Or just static.
        # For dynamic outputs, hook could write to a known file and we read it.

        duration = int((time.time() - start_time) * 1000)

        return {
            "file_changes": file_changes,
            "outputs": outputs,
            "verification_results": validation_results,
            "metadata": {
                "duration_ms": duration,
                "scripts_run": len(self.skill_def.post_scripts),
                "files_changed": len(file_changes),
            },
        }

    def _interpolate(self, template: str, inputs: Dict) -> str:
        """Simple {{var}} interpolation for shell commands."""
        from jinja2 import Environment, BaseLoader

        env = Environment(loader=BaseLoader, undefined=StrictUndefined)
        return env.from_string(template).render(**inputs)
```

---

## 7. `specs/03_skills/registry/SKILL_REGISTRY.py`

```python
# specs/03_skills/registry/SKILL_REGISTRY.py
"""
Skill Registry: Loads, Caches, Resolves Dependencies, Executes Skills.
"""

from __future__ import annotations
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Set
from collections import defaultdict

from kernel.protocols import IVertical, SkillDef, ISkillExecutor
from kernel.state import AgentState
from ..skill.SKILL_DEFINITION import SkillDef as SkillDefModel
from ..skill.SKILL_EXECUTOR import SkillExecutor


class SkillRegistry:
    def __init__(self, vertical: IVertical, tool_registry: "ToolRegistry"):
        self.vertical = vertical
        self.tool_registry = tool_registry
        self._skills: Dict[str, SkillDefModel] = {}
        self._executors: Dict[str, SkillExecutor] = {}
        self._load_all()

    def _load_all(self):
        """Scan vertical skills directory."""
        skills_dir = Path(self.vertical.manifest.skills_dir)  # e.g. verticals/saas_web/skills
        if not skills_dir.exists():
            return

        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "skill.yaml").exists():
                try:
                    skill_def = SkillDefModel.load_from_dir(skill_dir)
                    # Check vertical compatibility
                    if (
                        skill_def.compatible_verticals
                        and self.vertical.manifest.id not in skill_def.compatible_verticals
                    ):
                        continue
                    self._skills[skill_def.id] = skill_def
                    self._executors[skill_def.id] = SkillExecutor(skill_def, self.tool_registry)
                except Exception as e:
                    print(f"[SkillRegistry] Failed to load skill {skill_dir.name}: {e}")

    def get_skill(self, skill_id: str) -> Optional[SkillDefModel]:
        return self._skills.get(skill_id)

    def get_executor(self, skill_id: str) -> Optional[ISkillExecutor]:
        return self._executors.get(skill_id)

    def list_skills(self, category: str = None, tags: List[str] = None) -> List[SkillDefModel]:
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.category == category]
        if tags:
            skills = [s for s in skills if any(t in s.tags for t in tags)]
        return skills

    def resolve_dependencies(self, skill_ids: List[str]) -> List[str]:
        """Topological sort of skill IDs based on depends_on."""
        # Kahn's algorithm
        graph = {sid: set(self._skills[sid].depends_on) for sid in skill_ids if sid in self._skills}
        # Add transitive deps
        all_nodes = set(skill_ids)
        for sid in skill_ids:
            self._collect_deps(sid, all_nodes, graph)

        # Rebuild graph for all_nodes
        full_graph = {sid: set(self._skills[sid].depends_on) & all_nodes for sid in all_nodes}

        # Topo sort
        indegree = defaultdict(int)
        for u in full_graph:
            for v in full_graph[u]:
                indegree[v] += 1

        queue = [u for u in full_graph if indegree[u] == 0]
        result = []
        while queue:
            u = queue.pop(0)
            result.append(u)
            for v in full_graph[u]:
                indegree[v] -= 1
                if indegree[v] == 0:
                    queue.append(v)

        if len(result) != len(full_graph):
            raise ValueError("Circular dependency detected in skills")
        return result

    def _collect_deps(self, skill_id: str, all_nodes: Set, graph: Dict):
        if skill_id not in self._skills:
            return
        for dep in self._skills[skill_id].depends_on:
            if dep not in all_nodes:
                all_nodes.add(dep)
                self._collect_deps(dep, all_nodes, graph)
```

---

## 8. `specs/03_skills/registry/VERTICAL_LOADER.py`

```python
# specs/03_skills/registry/VERTICAL_LOADER.py
"""
Vertical Loader: Discovers and loads Vertical Plugins.
A Vertical is a directory with:
- manifest.yaml (VerticalManifest)
- skills/ (Skill directories)
- prompts/ (Jinja2 prompt templates)
- rag_collections/ (list of Qdrant collection names)
"""

from __future__ import annotations
import yaml
import importlib.util
from pathlib import Path
from typing: Dict, Optional
from kernel.protocols import IVertical, VerticalManifest

class VerticalLoader:
    def __init__(self, verticals_root: Path):
        self.verticals_root = verticals_root
        self._verticals: Dict[str, IVertical] = {}

    def discover(self) -> Dict[str, VerticalManifest]:
        """Scan verticals_root for manifest.yaml."""
        manifests = {}
        for v_dir in self.verticals_root.iterdir():
            if v_dir.is_dir():
                manifest_path = v_dir / "manifest.yaml"
                if manifest_path.exists():
                    with open(manifest_path) as f:
                        data = yaml.safe_load(f)
                        data["skills_dir"] = str(v_dir / "skills")
                        data["prompts_dir"] = str(v_dir / "prompts")
                        manifests[data["id"]] = VerticalManifest(**data)
        return manifests

    def load_vertical(self, vertical_id: str) -> IVertical:
        if vertical_id in self._verticals:
            return self._verticals[vertical_id]
        
        v_dir = self.verticals_root / vertical_id
        if not v_dir.exists():
            raise ValueError(f"Vertical not found: {vertical_id}")
        
        # 1. Load Manifest
        with open(v_dir / "manifest.yaml") as f:
            manifest_data = yaml.safe_load(f)
        manifest_data["skills_dir"] = str(v_dir / "skills")
        manifest_data["prompts_dir"] = str(v_dir / "prompts")
        manifest = VerticalManifest(**manifest_data)
        
        # 2. Load Custom Implementation (vertical_impl.py) if exists
        impl_path = v_dir / "vertical_impl.py"
        vertical_impl = None
        if impl_path.exists():
            spec = importlib.util.spec_from_file_location(f"vertical_{vertical_id}", impl_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Expect a class `VerticalImpl` implementing `IVertical`
            if hasattr(module, "VerticalImpl"):
                vertical_impl = module.VerticalImpl(manifest)
            else:
                raise RuntimeError(f"vertical_impl.py must contain 'VerticalImpl' class")
        else:
            # 3. Default Implementation (GenericVertical)
            from .DEFAULT_VERTICAL import GenericVertical
            vertical_impl = GenericVertical(manifest)
        
        self._verticals[vertical_id] = vertical_impl
        return vertical_impl
```

---

## 9. `specs/03_skills/examples/init_nextjs_app_router/skill.yaml`

> **Пример реального скилла.** Кладется в `verticals/saas_web/skills/init_nextjs_app_router/skill.yaml`

```yaml
id: init_nextjs_app_router
name: "Initialize Next.js 14 App Router Project"
version: "1.3.0"
description: |
  Creates a production-ready Next.js 14 project with TypeScript, Tailwind CSS, ESLint, Prettier, Husky.
  Uses App Router, Server Components by default. Configures path aliases (@/).
  Installs dependencies and runs initial typecheck/lint.
category: scaffold
tags: [nextjs, react, typescript, tailwind, scaffold, starter]
depends_on: [] # Root skill
provides: [nextjs_app_router, typescript, tailwind, eslint, prettier, husky]

inputs:
  type: object
  properties:
    project_name:
      type: string
      pattern: '^[a-z0-9-]+$'
      description: "NPM package name (kebab-case)"
      default: "app"
    package_manager:
      type: string
      enum: [pnpm, npm, yarn, bun]
      default: "pnpm"
      description: "Package manager to use"
    typescript_strict:
      type: boolean
      default: true
    tailwind_version:
      type: string
      enum: ["3", "4"]
      default: "3"
    eslint_config:
      type: string
      enum: ["standard", "airbnb", "next-core-web-vitals"]
      default: "next-core-web-vitals"
    init_git:
      type: boolean
      default: true
    install_deps:
      type: boolean
      default: true
  required: [project_name]

template_dir: "templates"
entrypoint: "hooks.py"
post_scripts:
  - "{{ package_manager }} install"
  - "{{ package_manager }} run lint"
  - "{{ package_manager }} run typecheck"
validation:
  - "test -f package.json"
  - "test -f tsconfig.json"
  - "test -f tailwind.config.ts"
  - "{{ package_manager }} run build" # Critical: ensures prod build works
outputs:
  nextjs_version: "14.2.0"
  package_manager: "{{ package_manager }}"
  router_type: "app_router"
compatible_verticals: ["saas_web", "marketing_landing"]
min_kernel_version: "1.0.0"
```

---

## 10. `specs/03_skills/examples/init_nextjs_app_router/templates/package.json.j2`

```json
{
  "name": "{{ project_name }}",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint --max-warnings=0",
    "typecheck": "tsc --noEmit",
    "format": "prettier --write .",
    "test": "jest",
    "test:watch": "jest --watch",
    "test:ci": "jest --ci --coverage",
    "db:generate": "prisma generate",
    "db:push": "prisma db push",
    "db:migrate": "prisma migrate dev",
    "db:studio": "prisma studio",
    "prepare": "husky install"
  },
  "dependencies": {
    "next": "14.2.0",
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "zod": "^3.22.0",
    "react-hook-form": "^7.51.0",
    "@hookform/resolvers": "^3.3.0",
    "clsx": "^2.1.0",
    "tailwind-merge": "^2.2.0",
    "lucide-react": "^0.344.0"
  },
  "devDependencies": {
    "@types/node": "^20.11.0",
    "@types/react": "^18.2.0",
    "@types/react-dom": "^18.2.0",
    "typescript": "^5.3.0",
    "tailwindcss": "^3.4.0",
    "postcss": "^8.4.0",
    "autoprefixer": "^10.4.0",
    "eslint": "^8.56.0",
    "eslint-config-next": "14.2.0",
    "prettier": "^3.2.0",
    "prettier-plugin-tailwindcss": "^0.5.0",
    "husky": "^9.0.0",
    "lint-staged": "^15.2.0",
    "jest": "^29.7.0",
    "jest-environment-jsdom": "^29.7.0",
    "@testing-library/react": "^14.2.0",
    "@testing-library/jest-dom": "^6.4.0",
    "prisma": "^5.10.0",
    "@prisma/client": "^5.10.0"
  },
  "engines": {
    "node": ">=20.0.0"
  },
  "packageManager": "{{ package_manager }}@latest"
}
```

---

## 11. `specs/03_skills/examples/init_nextjs_app_router/hooks.py`

```python
# specs/03_skills/examples/init_nextjs_app_router/hooks.py
"""
Hooks for init_nextjs_app_router skill.
"""

from __future__ import annotations
from typing import Dict, Any, List
from kernel.state import FileChange, VerificationGateResult, VerificationGateStatus
from ..SKILL_DEFINITION import HookContext


async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    inputs = ctx.inputs

    # Derive values
    if "project_name" in inputs:
        # Ensure valid npm name
        inputs["project_name"] = inputs["project_name"].lower().replace("_", "-")

    # Set default ports for dev server
    inputs["dev_port"] = 3000

    return inputs


async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    # No post-render modifications needed for this skill
    return file_changes


async def validate(ctx: HookContext) -> List[VerificationGateResult]:
    """
    Custom validation: Check if package.json has correct structure.
    """
    results = []
    # We could parse package.json here and validate versions match
    # For now, rely on post_scripts validation (pnpm install, build)
    return results
```

---

## 12. `specs/03_skills/examples/add_prisma_model/skill.yaml`

> **Пример композитного скилла** (зависит от `init_prisma`).

```yaml
id: add_prisma_model
name: "Add Prisma Model & Migration"
version: "1.1.0"
description: |
  Adds a new Prisma model to schema.prisma, runs `prisma migrate dev` to create migration,
  and runs `prisma generate` to update client.
  Supports relations, indexes, enums.
category: feature
tags: [prisma, database, model, migration, backend]
depends_on: [init_prisma] # Must have Prisma initialized
provides: [prisma_model]

inputs:
  type: object
  required: [model_name, fields]
  properties:
    model_name:
      type: string
      pattern: '^[A-Z][a-zA-Z0-9]*$'
      description: "PascalCase model name (e.g. User, BlogPost)"
    table_name:
      type: string
      description: "Optional custom table name (snake_case)"
    fields:
      type: array
      description: "List of field definitions"
      items:
        type: object
        required: [name, type]
        properties:
          name:
            type: string
            pattern: '^[a-z][a-zA-Z0-9]*$'
          type:
            type: string
            enum: [String, Int, BigInt, Float, Decimal, DateTime, Boolean, Json, Bytes, Enum]
          is_id: {type: boolean, default: false}
          is_unique: {type: boolean, default: false}
          is_optional: {type: boolean, default: true}
          default: {type: string} # Prisma default expression e.g. "autoincrement()", "now()", "'DRAFT'"
          relation:
            type: object
            properties:
              model: {type: string}
              field: {type: string}
              on_delete: {type: string, enum: [Cascade, Restrict, NoAction, SetNull, SetDefault]}
    indexes:
      type: array
      items:
        type: object
        properties:
          fields: {type: array, items: {type: string}}
          unique: {type: boolean, default: false}
    enums:
      type: array
      items:
        type: object
        properties:
          name: {type: string}
          values: {type: array, items: {type: string}}

template_dir: "templates"
entrypoint: "hooks.py"
post_scripts:
  - "pnpm prisma generate"
  - "pnpm prisma migrate dev --name add_{{ snake_case(model_name) }} --create-only"
validation:
  - "pnpm prisma validate"
  - "test -f prisma/migrations/*/migration.sql"
outputs:
  model_name: "{{ model_name }}"
  table_name: "{{ table_name | default(snake_case(model_name) + 's') }}"
compatible_verticals: ["saas_web", "py_fastapi_microsvc"] # Works in any vertical with Prisma
```

---

## 13. `specs/03_skills/examples/add_prisma_model/templates/prisma/schema.prisma.j2`

```prisma
// This template is APPENDED to existing schema.prisma
// Hook `post_render` handles the actual insertion logic (see below)

// generator client {
//   provider = "prisma-client-js"
// }

// datasource db {
//   provider = "postgresql"
//   url      = env("DATABASE_URL")
// }

{% if enums %}
{% for enum in enums %}
enum {{ enum.name }} {
{% for value in enum.values %}
  {{ value }}
{% endfor %}
}
{% endfor %}
{% endif %}

model {{ model_name }} {
{% if table_name %}
  @@map("{{ table_name }}")
{% endif %}

{% for field in fields %}
  {{ field.name }} {{ field.type }} {% if field.is_id %}@id{% endif %} {% if field.is_unique %}@unique{% endif %} {% if field.is_optional %}?{% endif %} {% if field.default %}@default({{ field.default }}){% endif %}
  {% if field.relation %}
    @relation(name: "{{ model_name }}_{{ field.name }}", references: [id], onDelete: {{ field.relation.on_delete | default("Cascade") }})
  {% endif %}
{% endfor %}

{% if indexes %}
{% for index in indexes %}
  @@index([{{ index.fields | join(', ') }}]){% if index.unique %} @unique{% endif %}
{% endfor %}
{% endif %}
}
```

---

## 14. `specs/03_skills/examples/add_prisma_model/hooks.py`

```python
# specs/03_skills/examples/add_prisma_model/hooks.py
"""
Advanced Hook: Appends to existing schema.prisma using Tree-sitter.
This avoids full file rewrite and preserves formatting/comments.
"""

from __future__ import annotations
from typing import Dict, Any, List
from kernel.state import FileChange
from ..SKILL_DEFINITION import HookContext


async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    inputs = ctx.inputs
    # Ensure table_name default
    if "table_name" not in inputs:
        from ..TEMPLATE_ENGINE import snake_case

        inputs["table_name"] = snake_case(inputs["model_name"]) + "s"
    return inputs


async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    """
    Instead of writing new file, we want to APPEND to prisma/schema.prisma.
    The template renders the *new model block*. We inject it into existing file.
    """
    inputs = ctx.inputs
    model_block = None

    # Find the rendered model block in file_changes (there should be only one .j2 usually)
    for fc in file_changes:
        if fc.path.endswith("schema.prisma"):  # Our template targets this path
            model_block = fc.content
            # Remove from list, we handle manually
            file_changes.remove(fc)
            break

    if not model_block:
        return file_changes  # Should not happen

    # Read existing schema.prisma from Sandbox
    fs_tool = ctx.tool_registry.get("filesystem")
    read_res = await fs_tool.execute(ctx.sandbox_id, {"action": "read", "path": "prisma/schema.prisma"}, {})

    if not read_res.success:
        # File doesn't exist? Create it (fallback)
        file_changes.append(FileChange(path="prisma/schema.prisma", content=model_block, action="create"))
        return file_changes

    existing_content = read_res.data["content"]

    # Find insertion point: Before last `}` of file? Or after last model?
    # Robust way: Use Tree-sitter to find last top-level declaration.
    # Simplified heuristic: Insert before last non-empty line if it's `}` or just append.
    lines = existing_content.splitlines()

    # Find last line that is not whitespace/comment
    insert_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("//"):
            # Insert after this line
            insert_idx = i + 1
            break

    # Ensure blank line before new model
    new_lines = lines[:insert_idx] + ["", ""] + model_block.splitlines() + [""]
    new_content = "\n".join(new_lines)

    file_changes.append(FileChange(path="prisma/schema.prisma", content=new_content, action="update"))

    return file_changes


async def validate(ctx: HookContext) -> List[VerificationGateResult]:
    # Validation is handled by post_scripts: `prisma validate`
    return []
```

---

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №3)

> **CONTEXT FOR NEXT STEP (Vertical Spec):**
> Ты реализуешь `specs/03_skills/`.
> 1. Создай `kernel/skills/` пакет: `models.py` (SkillDef, SkillOutput), `engine.py` (TemplateEngine), `executor.py` (SkillExecutor), `hooks.py` (протоколы хуков).
> 2. Создай `kernel/skills/registry.py` (SkillRegistry с топологической сортировкой зависимостей).
> 3. Созди `kernel/vertical/loader.py` (VerticalLoader, GenericVertical).
> 4. Реализуй `GenericVertical` (базовая реализация `IVertical`), которая:
>    *   Инициализирует `SkillRegistry`.
>    *   Имплементирует `get_verification_gates` (читает gates из manifest + skill validations).
>    *   Имплементирует `get_skill_executor`.
>    *   Имплементирует промпты через `PromptCompiler` (пока заглушка).
> 5. Создай структуру примеров в `verticals/saas_web/skills/` (два скилла выше).
> 6. **Интеграционный тест (`tests/skills/test_skill_execution.py`):**
>    *   Запуск `SkillExecutor` для `init_nextjs_app_router` в песочнице.
>    *   Проверка, что `package.json`, `tsconfig.json`, `prisma/schema.prisma` созданы.
>    *   Проверка, что `post_scripts` (`pnpm install`, `lint`, `typecheck`, `build`) прошли успешно (статус PASSED).
>    *   Запуск `add_prisma_model` с зависимостью от первого.
>    *   Проверка, что схема обновилась, миграция создалась.

---

### ✅ Чек-лист готовности Skills Framework (Definition of Done для Ответа №3)

- [ ] `SkillDef` загружается из `skill.yaml` + `hooks.py` без ошибок.
- [ ] `TemplateEngine` рендерит Jinja2 с кастомными фильтрами (`to_json`, `snake_case`, `skill_output`).
- [ ] `SkillExecutor` выполняет полный цикл: Hooks -> Render -> Write -> PostScripts -> Validation.
- [ ] `SkillRegistry` корректно разрешает зависимости (`depends_on`) и возвращает топологический порядок.
- [ ] `VerticalLoader` находит вертикали, загружает манифесты и создает `GenericVertical`.
- [ ] Пример `init_nextjs_app_router` проходит все гейты (`pnpm build` успешен).
- [ ] Пример `add_prisma_model` корректно аппендит модель в существующую схему через хук.

---

---

# Ответ №4: Knowledge Spec — Конвейер Знаний (RAG, Graph DB, Ingestion, Retrieval)

Это **мозг** системы. Без качественного контекста агент — «джуниор, гуглящий StackOverflow». С этим слоем — «синьор, знающий твой код и лучшие практики наизусть».

Сохрани в структуру:
```text
specs/04_knowledge/
├── KNOWLEDGE_ARCH.md
├── ingestion/
│   ├── INGESTION_PIPELINE.py
│   ├── PARSERS.py
│   ├── CHUNKING_STRATEGIES.py
│   ├── ENRICHMENT.py
│   └── EMBEDDING.py
├── storage/
│   ├── VECTOR_DB_SCHEMA.md
│   ├── GRAPH_DB_SCHEMA.md
│   ├── QDRANT_CLIENT.py
│   └── KUZU_CLIENT.py
├── retrieval/
│   ├── RETRIEVAL_STRATEGIES.py
│   ├── HYBRID_SEARCH.py
│   ├── GRAPH_TRAVERSAL.py
│   └── RERANKING.py
└── cli/
    └── INGEST_CLI.py
```

---

## 1. `specs/04_knowledge/KNOWLEDGE_ARCH.md`

````markdown
# Knowledge Base Architecture Specification

**Goal:** Provide agents with *precise, relevant, structured* context for code generation, fixing, and architecture decisions.

## 1. Core Principles

1.  **Code-Aware Chunking > Text Chunking.** Chunk by AST nodes (Function, Class, Interface), not by characters/tokens.
2.  **Dual Storage:** Vector DB (Semantic Search) + Graph DB (Structural Relationships).
3.  **Enrichment is Mandatory.** Raw code chunks are low value. Every chunk must have: `intent`, `pattern`, `dependencies`, `complexity`, `side_effects`.
4.  **Versioned Knowledge.** Chunks tagged with `framework_version`, `language_version`. Retrieval filters by current project stack.
5.  **Continuous Ingestion.** Git Webhooks → Ingestion Pipeline → DB. Zero manual steps.

## 2. Data Flow (Mermaid)

```mermaid
graph LR
    subgraph Sources
        GH[GitHub/GitLab Repos]
        Docs[Documentation Sites]
        Local[Local Projects]
        Success[Successful Generations]
    end

    subgraph Ingestion Pipeline
        Trigger[Trigger: Webhook / Cron / CLI]
        Clone[Clone / Checkout]
        Parse[AST Parsing\nTree-sitter / LSP]
        Chunk[Semantic Chunking\nSymbol-based]
        Enrich[LLM Enrichment\nIntent, Pattern, Issues]
        Embed[Embedding\nnomic-embed-text / 3-small]
        GraphBuild[Graph Extraction\nCalls, Imports, Inherits]
    end

    subgraph Storage
        Qdrant[(Qdrant Vector DB\nPayload: Metadata + Content)]
        Kuzu[(Kuzu Graph DB\nNodes: File, Class, Func\nEdges: CALLS, IMPORTS, CONTAINS)]
        S3[(Object Store\nRaw .md Chunks for Debug)]
    end

    subgraph Retrieval
        Agent[Agent Node\n(Coder, Fixer, Planner)]
        Router{Retrieval Router}
        Hybrid[Hybrid Search\nVector + BM25 + Filters]
        GraphQ[Graph Traversal\nCallers, Callees, Deps]
        Rerank[Cross-Encoder Rerank]
    end

    Sources --> Trigger
    Trigger --> Clone --> Parse --> Chunk
    Chunk --> Enrich
    Enrich --> Embed
    Enrich --> GraphBuild
    Embed --> Qdrant
    GraphBuild --> Kuzu
    Chunk --> S3

    Agent --> Router
    Router --> Hybrid
    Router --> GraphQ
    Hybrid --> Rerank
    GraphQ --> Rerank
    Rerank --> Agent
```

## 3. Chunk Types (The Unit of Knowledge)

| Chunk Type | Source | Size | Use Case |
| :--- | :--- | :--- | :--- |
| **Symbol Chunk** | Function, Class, Interface, Method, Hook, Component | 50-500 lines | Primary retrieval unit. "How to write a Prisma model?" |
| **File Summary** | Whole File (Imports, Exports, Top-level symbols) | 20-50 lines | "What does `user_service.py` do?" |
| **Config Chunk** | `package.json`, `tsconfig.json`, `prisma/schema.prisma`, `Dockerfile` | Whole file | "Next.js 14 TS config best practices" |
| **Doc Chunk** | Markdown docs (Headings H1/H2/H3) | Section | "Vercel Deployment Guide" |
| **Pattern Chunk** | Curated "Golden Examples" (Manual/Extracted) | Variable | Few-shot prompts for Planner/Coder |
| **Error Fix Chunk** | `Error Signature` -> `Fix Diff` (Mined from successful Fix Loops) | Small | Fixer Agent: "TypeError X -> Add null check" |

## 4. Retrieval Strategies by Agent

| Agent | Primary Strategy | Secondary | Filters |
| :--- | :--- | :--- | :--- |
| **Planner** | `workspace_symbols` (Graph) + Doc Search | Pattern Chunks (Architecture) | `type: architecture`, `framework: nextjs` |
| **Coder** | Hybrid Search (Vector + BM25) on Symbol Chunks | Graph: `CALLS` / `IMPORTS` of current symbol | `language: ts`, `framework: nextjs`, `version: 14` |
| **Fixer** | Error Signature Match (Vector) + `files_to_fix` context | Graph: Dependencies of broken file | `type: fix_pattern`, `error_type: TypeError` |
| **Reviewer** | Graph Traversal (Architecture) + Convention Docs | Cross-file Symbol Search | `type: convention`, `layer: domain` |
````

---

## 2. `specs/04_knowledge/ingestion/PARSERS.py`

```python
# specs/04_knowledge/ingestion/PARSERS.py
"""
Multi-language AST Parsers using Tree-sitter.
Extracts: Symbols (Func, Class, Interface), Imports, Calls, Docstrings.
Output: Normalized Symbol objects for Chunking.
"""

from __future__ import annotations
import tree_sitter
from tree_sitter_languages import get_parser, get_language
from pathlib import Path
from typing import Dict, List, Any, Optional, Generator, Tuple
from dataclasses import dataclass, field
from enum import Enum


class SymbolType(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE_ALIAS = "type_alias"
    ENUM = "enum"
    CONSTANT = "constant"
    VARIABLE = "variable"
    IMPORT = "import"
    EXPORT = "export"
    COMPONENT = "component"  # React/Vue/Svelte specific
    HOOK = "hook"  # useXxx
    DECORATOR = "decorator"


@dataclass
class Symbol:
    name: str
    type: SymbolType
    file_path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    code: str
    signature: str  # First line(s) - def foo(...): / class Foo / interface Foo
    docstring: Optional[str] = None
    parent: Optional[str] = None  # Parent class/function name
    decorators: List[str] = field(default_factory=list)
    # Relationships (populated later)
    imports: List[str] = field(default_factory=list)  # Resolved import paths
    calls: List[str] = field(default_factory=list)  # Function calls inside

    # Enrichment fields (filled by Enrichment step)
    intent: str = ""
    pattern: str = ""
    complexity: str = "O(1)"
    side_effects: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


# --- Language Configs ---

LANGUAGE_CONFIG = {
    ".ts": {"lang": "typescript", "query_file": "ts_symbols.scm"},
    ".tsx": {"lang": "tsx", "query_file": "tsx_symbols.scm"},
    ".js": {"lang": "javascript", "query_file": "js_symbols.scm"},
    ".jsx": {"lang": "javascript", "query_file": "js_symbols.scm"},
    ".py": {"lang": "python", "query_file": "py_symbols.scm"},
    ".go": {"lang": "go", "query_file": "go_symbols.scm"},
    ".rs": {"lang": "rust", "query_file": "rs_symbols.scm"},
    ".java": {"lang": "java", "query_file": "java_symbols.scm"},
    ".cs": {"lang": "c_sharp", "query_file": "cs_symbols.scm"},
    ".php": {"lang": "php", "query_file": "php_symbols.scm"},
    ".rb": {"lang": "ruby", "query_file": "rb_symbols.scm"},
}

# Tree-sitter Queries (Simplified - real ones in .scm files)
# We define inline for spec completeness.
TS_QUERIES = {
    "typescript": """
    ; Functions
    (function_declaration name: (identifier) @name) @function
    (arrow_function) @function
    (method_definition name: (property_identifier) @name) @method

    ; Classes/Interfaces
    (class_declaration name: (type_identifier) @name) @class
    (interface_declaration name: (type_identifier) @name) @interface
    (type_alias_declaration name: (type_identifier) @name) @type_alias

    ; Imports
    (import_statement) @import

    ; Calls
    (call_expression function: (identifier) @name) @call
    """,
    "python": """
    (function_definition name: (identifier) @name) @function
    (class_definition name: (identifier) @name) @class
    (import_statement) @import
    (import_from_statement) @import
    (call function: (identifier) @name) @call
    """,
    # ... add for other languages
}


class MultiLanguageParser:
    def __init__(self):
        self._parsers: Dict[str, tree_sitter.Parser] = {}
        self._queries: Dict[str, tree_sitter.Query] = {}
        self._init_parsers()

    def _init_parsers(self):
        for ext, cfg in LANGUAGE_CONFIG.items():
            try:
                parser = get_parser(cfg["lang"])
                lang = get_language(cfg["lang"])
                # Load query from string or file
                query_str = TS_QUERIES.get(cfg["lang"], "")
                if query_str:
                    query = lang.query(query_str)
                    self._parsers[ext] = parser
                    self._queries[ext] = query
            except Exception as e:
                print(f"[Parser] Failed to init {ext}: {e}")

    def parse_file(self, file_path: Path, content: bytes) -> List[Symbol]:
        ext = file_path.suffix.lower()
        parser = self._parsers.get(ext)
        query = self._queries.get(ext)

        if not parser or not query:
            return [self._fallback_chunk(file_path, content)]

        tree = parser.parse(content)
        captures = query.captures(tree.root_node)

        symbols = []
        # Process captures (simplified logic)
        # Real implementation builds Symbol objects from node ranges
        for node, tag in captures:
            if tag in ("name", "function", "method", "class", "interface", "import", "call"):
                # Extract details...
                pass  # Detailed implementation below
        return symbols

    def _fallback_chunk(self, file_path: Path, content: bytes) -> Symbol:
        """Fallback for unsupported languages: treat whole file as one chunk."""
        text = content.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        return Symbol(
            name=file_path.name,
            type=SymbolType.CONSTANT,  # Generic
            file_path=str(file_path),
            start_line=1,
            end_line=len(lines),
            start_byte=0,
            end_byte=len(content),
            code=text,
            signature=f"File: {file_path.name}",
            docstring=text[:500],
        )

    # --- Advanced Extraction Helpers ---

    def extract_symbol_details(self, node: tree_sitter.Node, code: bytes, file_path: str, tag: str) -> Optional[Symbol]:
        """Extracts a fully populated Symbol from a capture."""
        # This is where the heavy lifting happens per language.
        # Use node.type, node.children, node.start_point, node.end_point.
        # Use helper functions: get_node_text, find_child_by_type.
        pass
```

---

## 3. `specs/04_knowledge/ingestion/CHUNKING_STRATEGIES.py`

```python
# specs/04_knowledge/ingestion/CHUNKING_STRATEGIES.py
"""
Chunking Strategies.
Transforms flat list of Symbols into Retrieval Chunks with Context.
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path
from .PARSERS import Symbol, SymbolType


@dataclass
class RetrievalChunk:
    id: str  # Global unique: repo#file#symbol
    content: str  # The text to embed (Code + Docstring + Metadata summary)
    metadata: Dict[str, Any]  # Payload for Qdrant
    # Relationships for Graph DB
    graph_edges: List[Tuple[str, str, str]] = []  # (src_id, rel_type, dst_id)


class ChunkingStrategy:
    def __init__(self, repo_name: str, repo_url: str, branch: str, framework_version: str = ""):
        self.repo_name = repo_name
        self.repo_url = repo_url
        self.branch = branch
        self.framework_version = framework_version

    def create_chunks(self, symbols: List[Symbol], file_content_map: Dict[str, str]) -> List[RetrievalChunk]:
        """
        Main entry point.
        1. Create Symbol Chunks.
        2. Create File Summary Chunks.
        3. Create Config Chunks (if detected).
        4. Build Graph Edges.
        """
        chunks = []
        symbol_map = {f"{s.file_path}:{s.name}": s for s in symbols}

        # 1. Symbol Chunks
        for sym in symbols:
            if sym.type in (SymbolType.IMPORT, SymbolType.EXPORT, SymbolType.CALL):
                continue  # Handled via edges
            chunk = self._create_symbol_chunk(sym, file_content_map)
            chunks.append(chunk)

        # 2. File Summary Chunks
        file_symbols = {}
        for s in symbols:
            file_symbols.setdefault(s.file_path, []).append(s)
        for fpath, syms in file_symbols.items():
            chunks.append(self._create_file_summary_chunk(fpath, syms, file_content_map.get(fpath, "")))

        # 3. Config Chunks (Heuristic)
        for fpath, content in file_content_map.items():
            if self._is_config_file(fpath):
                chunks.append(self._create_config_chunk(fpath, content))

        return chunks

    def _create_symbol_chunk(self, sym: Symbol, file_map: Dict[str, str]) -> RetrievalChunk:
        # Content for Embedding: Signature + Docstring + Code (truncated)
        # We want the *signature* and *intent* to dominate the vector.
        embed_text = f"# {sym.signature}\n"
        if sym.docstring:
            embed_text += f"# Doc: {sym.docstring}\n"
        embed_text += f"# File: {sym.file_path}\n"
        embed_text += sym.code[:2000]  # Limit code size for embedding

        # Metadata for Filtering
        meta = {
            "repo": self.repo_name,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "file_path": sym.file_path,
            "symbol_name": sym.name,
            "symbol_type": sym.type.value,
            "language": self._detect_lang(sym.file_path),
            "framework_version": self.framework_version,
            "parent": sym.parent or "",
            "decorators": sym.decorators,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            # Enrichment fields (filled later)
            "intent": sym.intent,
            "pattern": sym.pattern,
            "complexity": sym.complexity,
            "side_effects": sym.side_effects,
            "tags": sym.tags,
        }

        chunk_id = f"{self.repo_name}#{sym.file_path}#{sym.name}#{sym.start_line}"

        # Graph Edges
        edges = []
        for imp in sym.imports:
            edges.append((chunk_id, "IMPORTS", self._resolve_import_id(imp, sym.file_path)))
        for call in sym.calls:
            edges.append((chunk_id, "CALLS", self._resolve_call_id(call, sym.file_path)))
        if sym.parent:
            edges.append((chunk_id, "BELONGS_TO", f"{self.repo_name}#{sym.file_path}#{sym.parent}"))

        return RetrievalChunk(id=chunk_id, content=embed_text, metadata=meta, graph_edges=edges)

    def _create_file_summary_chunk(self, file_path: str, symbols: List[Symbol], content: str) -> RetrievalChunk:
        imports = [s.name for s in symbols if s.type == SymbolType.IMPORT]
        exports = [
            s.name
            for s in symbols
            if s.type in (SymbolType.FUNCTION, SymbolType.CLASS, SymbolType.INTERFACE) and not s.parent
        ]

        summary = f"# File: {file_path}\n"
        summary += f"# Imports: {', '.join(imports[:20])}\n"
        summary += f"# Exports: {', '.join(exports[:20])}\n"
        summary += f"# Symbol Count: {len(symbols)}\n"

        meta = {
            "repo": self.repo_name,
            "file_path": file_path,
            "chunk_type": "file_summary",
            "language": self._detect_lang(file_path),
            "framework_version": self.framework_version,
            "symbols": [s.name for s in exports],
            "imports": imports,
        }
        chunk_id = f"{self.repo_name}#{file_path}#__file_summary__"
        return RetrievalChunk(id=chunk_id, content=summary, metadata=meta, graph_edges=[])

    def _create_config_chunk(self, file_path: str, content: str) -> RetrievalChunk:
        meta = {
            "repo": self.repo_name,
            "file_path": file_path,
            "chunk_type": "config",
            "language": self._detect_lang(file_path),
            "framework_version": self.framework_version,
        }
        chunk_id = f"{self.repo_name}#{file_path}#__config__"
        return RetrievalChunk(id=chunk_id, content=content[:4000], metadata=meta, graph_edges=[])

    def _is_config_file(self, path: str) -> bool:
        name = Path(path).name
        return name in (
            "package.json",
            "tsconfig.json",
            "prisma.schema",
            "dockerfile",
            "docker-compose.yml",
            ".github/workflows/",
            "pyproject.toml",
            "cargo.toml",
            "go.mod",
            "pom.xml",
        )

    def _detect_lang(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        return {".ts": "typescript", ".tsx": "tsx", ".py": "python", ".go": "go", ".rs": "rust"}.get(ext, "text")

    def _resolve_import_id(self, imp: str, from_file: str) -> str:
        # Heuristic: We don't know target file yet. Use placeholder resolved in Graph Build step.
        return f"import:{imp}"

    def _resolve_call_id(self, call: str, from_file: str) -> str:
        return f"call:{call}"
```

---

## 4. `specs/04_knowledge/ingestion/ENRICHMENT.py`

````python
# specs/04_knowledge/ingestion/ENRICHMENT.py
"""
LLM-based Enrichment of Code Chunks.
Runs as a BATCH job (async, concurrent) after parsing.
Adds semantic metadata: Intent, Pattern, Complexity, Issues.
"""

from __future__ import annotations
import asyncio
import json
from typing: List, Dict, Any
from pydantic import BaseModel, Field
from .CHUNKING_STRATEGIES import RetrievalChunk
from kernel.llm.client import LiteLLMClient # Our LLM Client
from kernel.config import settings

# --- Enrichment Schema ---

class EnrichedMetadata(BaseModel):
    intent: str = Field(..., description="One sentence: What does this code do?")
    pattern: str = Field(..., description="Design Pattern: Repository, Factory, Hook, HOC, Middleware, Controller, Service, Utility, Config, Test, Migration, Script, Other")
    complexity: str = Field(..., description="O(1), O(n), DB Query, External API, Heavy Compute, Recursive")
    side_effects: List[str] = Field(default_factory=list, description="DB Write, Cache Invalidation, Email Send, File IO, Network Request, State Mutation, None")
    dependencies_explained: List[str] = Field(default_factory=list, description="Human readable: 'UserRepository: DB Access', 'Redis: Cache'")
    usage_example: Optional[str] = Field(None, description="Minimal usage snippet if inferable")
    potential_issues: List[str] = Field(default_factory=list, description="N+1, No Timeout, SQL Injection, Race Condition, Memory Leak, None")
    tags: List[str] = Field(default_factory=list, description="auth, database, cache, api, ui, security, performance, testing")

ENRICHMENT_PROMPT = """
You are a Senior Software Architect analyzing a code snippet for a Knowledge Base.
Analyze the following code and return STRICT JSON matching the schema.

CODE:
```{language}
{code}
```

CONTEXT:
- File: {file_path}
- Symbol: {symbol_name} ({symbol_type})
- Repo: {repo_name} (Stack: {framework_version})

RETURN JSON:
{{
  "intent": "string",
  "pattern": "Repository | Factory | Builder | Hook | HOC | Middleware | Controller | Service | Utility | Config | Test | Migration | Script | Other",
  "complexity": "O(1) | O(n) | DB Query | External API | Heavy Compute | Recursive",
  "side_effects": ["DB Write", "Cache Invalidation", "Email Send", "File IO", "Network Request", "State Mutation", "None"],
  "dependencies_explained": ["DepName: Purpose"],
  "usage_example": "code snippet or null",
  "potential_issues": ["N+1 problem", "No timeout on HTTP", "SQL Injection risk", "Race condition", "Memory leak", "None"],
  "tags": ["auth", "database", "cache", "api", "ui", "security", "performance", "testing"]
}}
"""

class EnrichmentPipeline:
    def __init__(self, llm_client: LiteLLMClient, batch_size: int = 20, concurrency: int = 5):
        self.llm = llm_client
        self.batch_size = batch_size
        self.semaphore = asyncio.Semaphore(concurrency)
        self.model = settings.llm.enrichment_model # e.g. "router/enricher" -> Haiku/4o-mini

    async def enrich_chunks(self, chunks: List[RetrievalChunk]) -> List[RetrievalChunk]:
        """Process chunks in batches."""
        # Filter chunks that need enrichment (skip config/file_summary if desired)
        to_enrich = [c for c in chunks if c.metadata.get("chunk_type") == "symbol"]
        
        for i in range(0, len(to_enrich), self.batch_size):
            batch = to_enrich[i:i+self.batch_size]
            await self._enrich_batch(batch)
            
        return chunks

    async def _enrich_batch(self, batch: List[RetrievalChunk]):
        tasks = [self._enrich_one(chunk) for chunk in batch]
        await asyncio.gather(*tasks)

    async def _enrich_one(self, chunk: RetrievalChunk):
        async with self.semaphore:
            meta = chunk.metadata
            prompt = ENRICHMENT_PROMPT.format(
                language=meta.get("language", "text"),
                code=chunk.content[:3000], # Limit context
                file_path=meta.get("file_path", ""),
                symbol_name=meta.get("symbol_name", ""),
                symbol_type=meta.get("symbol_type", ""),
                repo_name=meta.get("repo", ""),
                framework_version=meta.get("framework_version", "")
            )
            
            try:
                # Use structured output
                result = await self.llm.achat(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.model,
                    response_model=EnrichedMetadata,
                    temperature=0.0
                )
                # Update chunk metadata
                chunk.metadata.update(result.model_dump())
                chunk.content = self._rebuild_content(chunk) # Rebuild embed text with new metadata
            except Exception as e:
                print(f"[Enrichment] Failed for {chunk.id}: {e}")
                # Set defaults
                chunk.metadata.update(EnrichedMetadata(intent="Unknown", pattern="Other", complexity="Unknown", side_effects=["None"]).model_dump())

    def _rebuild_content(self, chunk: RetrievalChunk) -> str:
        """Rebuild embedding text with enriched metadata for better retrieval."""
        m = chunk.metadata
        header = f"# {m.get('symbol_name', '')} ({m.get('symbol_type', '')})\n"
        header += f"# Intent: {m.get('intent', '')}\n"
        header += f"# Pattern: {m.get('pattern', '')}\n"
        header += f"# Tags: {', '.join(m.get('tags', []))}\n"
        header += f"# File: {m.get('file_path', '')}\n"
        # Keep original code but maybe trimmed
        code_part = chunk.content.split("\n# File:")[0] if "# File:" in chunk.content else chunk.content
        return header + code_part
````

---

## 5. `specs/04_knowledge/storage/VECTOR_DB_SCHEMA.md`

````markdown
# Qdrant Vector DB Schema

**Collection:** `code_chunks` (Primary)
**Sharding:** By `repo` (tenant) or `language` (performance).

## 1. Vector Configuration
```yaml
vectors:
  size: 768 # nomic-embed-text / text-embedding-3-small
  distance: Cosine
  hnsw_config:
    m: 16
    ef_construct: 100
    full_scan_threshold: 10000
quantization:
  scalar:
    type: int8
    always_ram: true
```

## 2. Payload Indexes (Critical for Filtering Speed)
```json
[
  {"field_name": "repo", "field_schema": "keyword"},
  {"field_name": "language", "field_schema": "keyword"},
  {"field_name": "symbol_type", "field_schema": "keyword"},
  {"field_name": "framework_version", "field_schema": "keyword"},
  {"field_name": "pattern", "field_schema": "keyword"},
  {"field_name": "tags", "field_schema": "keyword"},
  {"field_name": "file_path", "field_schema": "text"}, // For prefix filtering
  {"field_name": "symbol_name", "field_schema": "keyword"},
  {"field_name": "chunk_type", "field_schema": "keyword"} // symbol, file_summary, config
]
```

## 3. Payload Structure (Per Point)
```json
{
  "id": "vercel_next.js#packages/next/server/app/render.ts#renderToReadableStream#145",
  "vector": [0.1, -0.3, ...],
  "payload": {
    "content": "# renderToReadableStream (function)\n# Intent: Streams React component output...\n# Pattern: Adapter\n# Tags: streaming, react, ssr\n# File: packages/next/server/app/render.ts\n\nasync function renderToReadableStream(...) { ... }",
    "repo": "vercel_next.js",
    "repo_url": "https://github.com/vercel/next.js",
    "branch": "canary",
    "file_path": "packages/next/server/app/render.ts",
    "symbol_name": "renderToReadableStream",
    "symbol_type": "function",
    "language": "typescript",
    "framework_version": "nextjs@14.2.0",
    "chunk_type": "symbol",
    "start_line": 145,
    "end_line": 210,
    "parent": "",
    "decorators": [],
    "intent": "Streams React component output to a readable web stream for SSR.",
    "pattern": "Adapter",
    "complexity": "O(n) - proportional to component tree size",
    "side_effects": ["Network Request (streaming)"],
    "dependencies_explained": ["React DOM Server: Core rendering", "Web Streams API: Standard streaming"],
    "usage_example": "const stream = await renderToReadableStream(jsx, { ... })",
    "potential_issues": ["Backpressure handling", "Client hydration mismatch"],
    "tags": ["streaming", "ssr", "react", "server-components"]
  }
}
```

## 4. Additional Collections
- `docs_chunks`: Documentation (Markdown split by headers). Payload: `header_path`, `url`.
- `error_fixes`: Mined from Fix Loop. Payload: `error_signature`, `fix_diff`, `root_cause`.
- `patterns`: Curated Golden Examples. Payload: `pattern_name`, `quality_score`.
````

---

## 6. `specs/04_knowledge/storage/GRAPH_DB_SCHEMA.md`

````markdown
# Kuzu / Neo4j Graph DB Schema

**Goal:** Answer structural questions Vector Search cannot:
- "Who calls `getUser`?"
- "What does `UserService` depend on?"
- "Show me the call chain from `API Route` to `Database`."

## 1. Node Labels & Properties

| Label | Properties | Description |
| :--- | :--- | :--- |
| `Repo` | `name`, `url`, `branch`, `framework_version` | Root |
| `File` | `path`, `language`, `size`, `hash` | Source file |
| `Symbol` | `name`, `type`, `signature`, `start_line`, `end_line`, `intent`, `pattern`, `complexity`, `tags[]` | Function, Class, Interface, etc. |
| `Config` | `path`, `content_hash`, `type` (package_json, tsconfig, dockerfile) | Config files |
| `Import` | `source`, `specifier`, `is_default`, `is_namespace` | Import statement details |

## 2. Relationship Types

| Type | Source | Target | Properties | Description |
| :--- | :--- | :--- | :--- | :--- |
| `CONTAINS` | `Repo` | `File` | | Repo owns file |
| `CONTAINS` | `File` | `Symbol` | | File defines symbol |
| `CONTAINS` | `File` | `Config` | | File is config |
| `IMPORTS` | `Symbol` | `Symbol` | `import_type` (default/named/namespace) | Symbol A imports Symbol B |
| `IMPORTS_FILE` | `Symbol` | `File` | `specifier` | Symbol imports whole file (side-effect) |
| `CALLS` | `Symbol` | `Symbol` | `call_type` (static/dynamic), `line` | Symbol A calls Symbol B |
| `INHERITS` | `Symbol` | `Symbol` | | Class A extends Class B |
| `IMPLEMENTS` | `Symbol` | `Symbol` | | Class implements Interface |
| `DECORATES` | `Symbol` | `Symbol` | `decorator_name` | Decorator usage |
| `USES_TYPE` | `Symbol` | `Symbol` | `context` (param/return/generic) | Type usage |

## 3. Key Cypher Queries (Kuzu Syntax)

```cypher
// 1. Find Callers of a function (Reverse Call Graph)
MATCH (caller:Symbol)-[:CALLS]-> (callee:Symbol {name: "getUser", file_path: "src/services/user.ts"})
RETURN caller.name, caller.file_path, caller.intent LIMIT 20

// 2. Get Dependencies of a File (Imports -> External Symbols)
MATCH (f:File {path: "src/api/users.ts"})-[:CONTAINS]->(s:Symbol)-[:IMPORTS]->(dep:Symbol)
RETURN DISTINCT dep.name, dep.file_path, dep.intent, dep.pattern

// 3. Architecture View: Module Dependencies
MATCH (f1:File)-[:CONTAINS]->(s1:Symbol)-[:IMPORTS|CALLS]->(s2:Symbol)<-[:CONTAINS]-(f2:File)
WHERE f1.path STARTS WITH "src/modules/" AND f2.path STARTS WITH "src/modules/"
AND f1 <> f2
RETURN f1.path AS from_module, f2.path AS to_module, count(*) AS coupling
ORDER BY coupling DESC

// 4. Find Implementation of Interface
MATCH (impl:Symbol)-[:IMPLEMENTS]->(iface:Symbol {name: "UserRepository"})
RETURN impl.name, impl.file_path, impl.intent

// 5. Impact Analysis: What breaks if I change `User` model?
MATCH (changed:Symbol {name: "User", type: "class"})<-[:USES_TYPE|CALLS|INHERITS*]-(affected:Symbol)
RETURN DISTINCT affected.name, affected.file_path, affected.type
```
````

---

## 7. `specs/04_knowledge/storage/QDRANT_CLIENT.py`

```python
# specs/04_knowledge/storage/QDRANT_CLIENT.py
"""
Async Qdrant Client Wrapper.
Handles: Upsert (Batch), Search (Hybrid), Delete, Scroll.
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional, Literal
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    SearchParams,
    Prefetch,
    FusionQuery,
    Fusion,
    SparseVector,
)
from ..ingestion.CHUNKING_STRATEGIES import RetrievalChunk
from kernel.config import settings


class QdrantKB:
    def __init__(self, url: str, api_key: str = None, collection: str = "code_chunks"):
        self.client = AsyncQdrantClient(url=url, api_key=api_key, timeout=60)
        self.collection = collection

    async def ensure_collection(self, vector_size: int = 768):
        from qdrant_client.models import VectorParams, Distance, ScalarQuantization, ScalarQuantizationConfig

        exists = await self.client.collection_exists(self.collection)
        if not exists:
            await self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                quantization_config=ScalarQuantization(scalar=ScalarQuantizationConfig(type="int8", always_ram=True)),
            )
            # Create Payload Indexes (Async helper)
            await self._create_indexes()

    async def _create_indexes(self):
        indexes = ["repo", "language", "symbol_type", "framework_version", "pattern", "tags", "chunk_type"]
        for field in indexes:
            try:
                await self.client.create_payload_index(
                    collection_name=self.collection, field_name=field, field_schema="keyword"
                )
            except:
                pass  # Ignore if exists

    async def upsert_chunks(self, chunks: List[RetrievalChunk], vectors: List[List[float]]):
        """Batch Upsert. Chunks and Vectors must align."""
        points = []
        for chunk, vec in zip(chunks, vectors):
            # Qdrant payload cannot have nested lists of objects easily, flatten tags.
            payload = chunk.metadata.copy()
            payload["content"] = chunk.content  # Store content for retrieval
            # Ensure tags is list of strings
            if isinstance(payload.get("tags"), list):
                payload["tags"] = [str(t) for t in payload["tags"]]

            points.append(PointStruct(id=chunk.id, vector=vec, payload=payload))

        # Batch upsert (Qdrant handles batching internally, but chunk client-side for memory)
        batch_size = 100
        for i in range(0, len(points), batch_size):
            await self.client.upsert(collection_name=self.collection, points=points[i : i + batch_size], wait=True)

    async def hybrid_search(
        self,
        query_vector: List[float],
        query_text: str,  # For BM25 (if using Qdrant Hybrid)
        filters: Dict[str, Any],
        limit: int = 10,
        score_threshold: float = 0.65,
    ) -> List[Dict]:
        """
        Hybrid Search: Vector + BM25 (via Qdrant's `prefetch` + `fusion`).
        Requires Qdrant 1.8+ with BM25 index on `content`.
        """
        qdrant_filter = self._build_filter(filters)

        # 1. Vector Search (Prefetch)
        prefetch = Prefetch(
            query=query_vector,
            filter=qdrant_filter,
            limit=limit * 3,  # Fetch more for fusion
            using="default",  # Dense vector name
        )

        # 2. BM25 Search (Prefetch) - Requires `content` field indexed as `text` with BM25
        # Note: Qdrant BM25 is experimental/beta. Alternative: Use separate BM25 index (Tantivy/Whoosh) or rely on Vector only.
        # Here we show Fusion approach.

        try:
            results = await self.client.query_points(
                collection_name=self.collection,
                prefetch=[prefetch],
                query=FusionQuery(fusion=Fusion.RRF),  # Reciprocal Rank Fusion
                limit=limit,
                with_payload=True,
                score_threshold=score_threshold,
            )
            return [{"id": p.id, "score": p.score, "payload": p.payload} for p in results.points]
        except Exception as e:
            # Fallback: Pure Vector Search
            results = await self.client.search(
                collection_name=self.collection,
                query_vector=query_vector,
                query_filter=qdrant_filter,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
            )
            return [{"id": p.id, "score": p.score, "payload": p.payload} for p in results]

    async def search_by_symbol(
        self, symbol_name: str, file_path: str = "", filters: Dict = None, limit: int = 5
    ) -> List[Dict]:
        """Exact/Partial match on symbol_name + file_path."""
        conds = [FieldCondition(key="symbol_name", match=MatchValue(value=symbol_name))]
        if file_path:
            conds.append(FieldCondition(key="file_path", match=MatchValue(value=file_path)))
        if filters:
            for k, v in filters.items():
                if isinstance(v, list):
                    conds.append(FieldCondition(key=k, match=MatchAny(any=v)))
                else:
                    conds.append(FieldCondition(key=k, match=MatchValue(value=v)))

        # Use scroll for exact filter match (no vector)
        records, _ = await self.client.scroll(
            collection_name=self.collection, scroll_filter=Filter(must=conds), limit=limit, with_payload=True
        )
        return [{"id": r.id, "score": 1.0, "payload": r.payload} for r in records]

    async def delete_repo(self, repo_name: str):
        await self.client.delete(
            collection_name=self.collection,
            points_selector=Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo_name))]),
        )

    def _build_filter(self, filters: Dict) -> Optional[Filter]:
        if not filters:
            return None
        must = []
        for k, v in filters.items():
            if isinstance(v, list):
                must.append(FieldCondition(key=k, match=MatchAny(any=[str(x) for x in v])))
            else:
                must.append(FieldCondition(key=k, match=MatchValue(value=v)))
        return Filter(must=must) if must else None
```

---

## 8. `specs/04_knowledge/storage/KUZU_CLIENT.py`

```python
# specs/04_knowledge/storage/KUZU_CLIENT.py
"""
Kuzu Graph DB Client (Embedded OLAP Graph).
Used for Code Graph: Call Graph, Import Graph, Inheritance.
"""

import kuzu
from typing: List, Dict, Any, Tuple
from ..ingestion.CHUNKING_STRATEGIES import RetrievalChunk

class KuzuGraph:
    def __init__(self, db_path: str = "./kuzu_db"):
        self.db = kuzu.Database(db_path)
        self.conn = kuzu.Connection(self.db)
        self._init_schema()

    def _init_schema(self):
        # Node Tables
        self.conn.execute("CREATE NODE TABLE IF NOT EXISTS Repo (name STRING, url STRING, branch STRING, framework_version STRING, PRIMARY KEY (name))")
        self.conn.execute("CREATE NODE TABLE IF NOT EXISTS File (path STRING, language STRING, hash STRING, PRIMARY KEY (path))")
        self.conn.execute("""
            CREATE NODE TABLE IF NOT EXISTS Symbol (
                id STRING, name STRING, type STRING, signature STRING, 
                start_line INT64, end_line INT64, intent STRING, pattern STRING, 
                complexity STRING, tags STRING, PRIMARY KEY (id)
            )
        """)
        self.conn.execute("CREATE NODE TABLE IF NOT EXISTS Config (path STRING, type STRING, hash STRING, PRIMARY KEY (path))")
        
        # Rel Tables
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS CONTAINS (FROM Repo TO File, FROM File TO Symbol, FROM File TO Config)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS IMPORTS (FROM Symbol TO Symbol, import_type STRING)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS CALLS (FROM Symbol TO Symbol, call_type STRING, line INT64)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS INHERITS (FROM Symbol TO Symbol)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS IMPLEMENTS (FROM Symbol TO Symbol)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS USES_TYPE (FROM Symbol TO Symbol, context STRING)")

    def upsert_chunks(self, chunks: List[RetrievalChunk]):
        """Batch upsert nodes and edges from chunks."""
        # 1. Collect unique nodes
        repos = {}
        files = {}
        symbols = {}
        configs = {}
        edges = [] # (src_id, rel, dst_id, props)
        
        for chunk in chunks:
            meta = chunk.metadata
            repo_name = meta["repo"]
            file_path = meta["file_path"]
            
            # Repo
            if repo_name not in repos:
                repos[repo_name] = {"name": repo_name, "url": meta.get("repo_url"), "branch": meta.get("branch"), "framework_version": meta.get("framework_version")}
            
            # File
            if file_path not in files:
                files[file_path] = {"path": file_path, "language": meta.get("language"), "hash": meta.get("content_hash", "")}
            
            # Symbol / Config
            if meta.get("chunk_type") == "symbol":
                sym_id = chunk.id
                if sym_id not in symbols:
                    symbols[sym_id] = {
                        "id": sym_id, "name": meta.get("symbol_name"), "type": meta.get("symbol_type"),
                        "signature": meta.get("signature", "")[:500], "start_line": meta.get("start_line"),
                        "end_line": meta.get("end_line"), "intent": meta.get("intent", ""),
                        "pattern": meta.get("pattern", ""), "complexity": meta.get("complexity", ""),
                        "tags": ",".join(meta.get("tags", []))
                    }
            elif meta.get("chunk_type") == "config":
                if file_path not in configs:
                    configs[file_path] = {"path": file_path, "type": Path(file_path).name, "hash": meta.get("content_hash", "")}
            
            # Edges from chunk.graph_edges
            edges.extend(chunk.graph_edges)
        
        # 2. Execute COPY FROM or INSERT (Kuzu supports COPY FROM CSV/Arrow for speed)
        # For spec, we use individual inserts (slow but clear). Production: Use Pandas/Arrow -> COPY.
        
        for repo in repos.values():
            self.conn.execute("MERGE (r:Repo {name: $name, url: $url, branch: $branch, framework_version: $fw})", repo)
        
        for file in files.values():
            self.conn.execute("MERGE (f:File {path: $path, language: $lang, hash: $hash})", file)
            self.conn.execute("MATCH (r:Repo {name: $repo}), (f:File {path: $path}) MERGE (r)-[:CONTAINS]->(f)", 
                              {"repo": repo_name, "path": file["path"]}) # Need repo_name context
            
        for sym in symbols.values():
            self.conn.execute("""
                MERGE (s:Symbol {id: $id}) 
                SET s.name=$name, s.type=$type, s.signature=$sig, s.start_line=$sl, s.end_line=$el, 
                    s.intent=$intent, s.pattern=$pattern, s.complexity=$comp, s.tags=$tags
            """, sym)
            # Link File -> Symbol
            self.conn.execute("MATCH (f:File {path: $fpath}), (s:Symbol {id: $sid}) MERGE (f)-[:CONTAINS]->(s)",
                              {"fpath": meta["file_path"], "sid": sym["id"]}) # Need file_path context
            
        for config in configs.values():
            self.conn.execute("MERGE (c:Config {path: $path, type: $type, hash: $hash})", config)
            self.conn.execute("MATCH (f:File {path: $path}), (c:Config {path: $path}) MERGE (f)-[:CONTAINS]->(c)", config)
            
        # 3. Edges (Batch via COPY ideally)
        for src, rel, dst, *props in edges:
            prop_dict = props[0] if props else {}
            if rel == "IMPORTS":
                self.conn.execute("MATCH (a:Symbol {id: $src}), (b:Symbol {id: $dst}) MERGE (a)-[:IMPORTS {import_type: $it}]->(b)", 
                                  {"src": src, "dst": dst, "it": prop_dict.get("import_type", "named")})
            elif rel == "CALLS":
                self.conn.execute("MATCH (a:Symbol {id: $src}), (b:Symbol {id: $dst}) MERGE (a)-[:CALLS {call_type: $ct, line: $ln}]->(b)",
                                  {"src": src, "dst": dst, "ct": prop_dict.get("call_type", "static"), "ln": prop_dict.get("line", 0)})
            # ... INHERITS, IMPLEMENTS, USES_TYPE

    def query_callers(self, symbol_name: str, file_path: str = "", limit: int = 20) -> List[Dict]:
        where = "callee.name = $name"
        params = {"name": symbol_name, "limit": limit}
        if file_path:
            where += " AND callee.file_path = $fpath"
            params["fpath"] = file_path
            
        query = f"""
        MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol)
        WHERE {where}
        RETURN caller.id, caller.name, caller.file_path, caller.intent, caller.pattern, caller.signature
        LIMIT $limit
        """
        return self.conn.execute(query, params).get_as_df().to_dict("records")

    def query_dependencies(self, file_path: str, depth: int = 1) -> List[Dict]:
        query = f"""
        MATCH (f:File {{path: $path}})-[:CONTAINS]->(s:Symbol)-[:IMPORTS|CALLS*1..{depth}]->(dep:Symbol)
        RETURN DISTINCT dep.id, dep.name, dep.file_path, dep.intent, dep.pattern, dep.type
        LIMIT 50
        """
        return self.conn.execute(query, {"path": file_path}).get_as_df().to_dict("records")

    def close(self):
        self.conn.close()
        self.db.close()
```

---

## 9. `specs/04_knowledge/retrieval/RETRIEVAL_STRATEGIES.py`

```python
# specs/04_knowledge/retrieval/RETRIEVAL_STRATEGIES.py
"""
High-Level Retrieval Strategies for different Agent Nodes.
Orchestrates Vector Search, Graph Traversal, Reranking.
"""

from __future__ import annotations
from typing: List, Dict, Any, Optional, Literal
from pydantic import BaseModel
from kernel.state import AgentState
from ..storage.QDRANT_CLIENT import QdrantKB
from ..storage.KUZU_CLIENT import KuzuGraph
from kernel.llm.client import LiteLLMClient

class RetrievalResult(BaseModel):
    chunks: List[Dict] # Unified format: {id, content, metadata, score, source: "vector|graph"}
    strategy: str
    query: str

class RetrievalEngine:
    def __init__(self, vector_db: QdrantKB, graph_db: KuzuGraph, llm: LiteLLMClient):
        self.vector = vector_db
        self.graph = graph_db
        self.llm = llm

    # --- Strategy 1: Coder / Fixer (Hybrid Semantic + Keyword) ---
    
    async def retrieve_for_coding(
        self, 
        state: AgentState, 
        query: str, 
        current_file: str = "", 
        top_k: int = 8
    ) -> RetrievalResult:
        """
        Used by Coder/Fixer nodes.
        Context: Current task, current file, tech stack.
        """
        # 1. Build Filters from State
        filters = self._build_context_filters(state)
        
        # 2. Embed Query
        query_vec = await self._embed(query)
        
        # 3. Vector Search (Hybrid)
        vector_results = await self.vector.hybrid_search(
            query_vector=query_vec,
            query_text=query,
            filters=filters,
            limit=top_k * 2
        )
        
        # 4. Graph Augmentation (If current_file known)
        graph_results = []
        if current_file:
            # Get symbols in current file
            file_symbols = await self.vector.search_by_symbol("", current_file, filters, limit=5)
            for fs in file_symbols:
                sym_name = fs["payload"].get("symbol_name")
                if sym_name:
                    callers = self.graph.query_callers(sym_name, current_file, limit=3)
                    graph_results.extend(callers)
        
        # 5. Merge & Deduplicate
        merged = self._merge_results(vector_results, graph_results)
        
        # 6. Rerank (Cross-Encoder or LLM-based)
        reranked = await self._rerank(query, merged, top_k)
        
        return RetrievalResult(chunks=reranked, strategy="hybrid_coding", query=query)

    # --- Strategy 2: Planner (Architecture & Patterns) ---
    
    async def retrieve_for_planning(
        self, 
        state: AgentState, 
        query: str, 
        top_k: int = 10
    ) -> RetrievalResult:
        """
        Used by Planner.
        Focus: Architecture patterns, configs, high-level modules.
        """
        filters = self._build_context_filters(state)
        filters["chunk_type"] = ["file_summary", "config", "symbol"]
        filters["symbol_type"] = ["class", "interface", "function"] # High level
        
        query_vec = await self._embed(query)
        results = await self.vector.hybrid_search(query_vec, query, filters, limit=top_k)
        
        # Add Graph: Module Dependencies
        # (Requires knowing entry points, skip for brevity)
        
        reranked = await self._rerank(query, results, top_k)
        return RetrievalResult(chunks=reranked, strategy="planning", query=query)

    # --- Strategy 3: Fixer (Error Signature Matching) ---
    
    async def retrieve_for_fixing(
        self, 
        state: AgentState, 
        error_log: str, 
        failed_files: List[str],
        top_k: int = 5
    ) -> RetrievalResult:
        """
        Used by Fixer.
        Query = Error message + Stack trace.
        Filter = Files to fix + Language.
        """
        filters = self._build_context_filters(state)
        filters["file_path"] = failed_files # Qdrant Filter: MatchAny on file_path? 
        # Note: Qdrant doesn't support `MatchAny` on `file_path` easily if not keyword. 
        # Better: Search by error signature in `error_fixes` collection.
        
        # 1. Search Error Fixes Collection (Specialized)
        error_vec = await self._embed(error_log)
        fix_results = await self.vector.hybrid_search(
            error_vec, error_log, 
            filters={"chunk_type": "error_fix"}, 
            limit=top_k
        )
        
        # 2. Get Context of Failed Files (File Summaries)
        file_context = []
        for f in failed_files:
            res = await self.vector.search_by_symbol("", f, filters, limit=1)
            file_context.extend(res)
            
        merged = fix_results + file_context
        reranked = await self._rerank(error_log, merged, top_k)
        return RetrievalResult(chunks=reranked, strategy="fixing", query=error_log)

    # --- Helpers ---
    
    def _build_context_filters(self, state: AgentState) -> Dict[str, Any]:
        vertical_manifest = state.get("vertical_manifest", {})
        tech_stack = vertical_manifest.get("tech_stack", {})
        
        filters = {
            "language": tech_stack.get("language", "typescript"),
            "framework_version": tech_stack.get("framework_version", ""),
        }
        # Add vertical specific tags
        if "tags" in vertical_manifest:
            filters["tags"] = vertical_manifest["tags"]
        return {k: v for k, v in filters.items() if v}

    async def _embed(self, text: str) -> List[float]:
        # Use fast local embedder or API
        # return await self.llm.embed(text) # Assuming client has embed method
        return [0.0] * 768 # Placeholder

    def _merge_results(self, *result_lists: List[Dict]) -> List[Dict]:
        seen = set()
        merged = []
        for rlist in result_lists:
            for r in rlist:
                pid = r["id"]
                if pid not in seen:
                    seen.add(pid)
                    merged.append(r)
        return merged

    async def _rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        if not candidates: return []
        
        # Option A: Cross-Encoder (Fast, Local) - Best for production
        # scores = cross_encoder.predict([(query, c['payload']['content']) for c in candidates])
        
        # Option B: LLM Rerank (Slow, Accurate) - Use for Planner/Fixer
        # Prompt: "Rate relevance 0-10 for query: ..."
        
        # Option C: Heuristic (Fast, No Model)
        # Boost: exact symbol match, same file, high pattern match.
        for c in candidates:
            score = c.get("score", 0.5)
            payload = c.get("payload", {})
            # Boost exact symbol name match in query
            if payload.get("symbol_name", "").lower() in query.lower():
                score += 0.3
            # Boost same file
            # Boost pattern match (e.g. query "repository" -> pattern "Repository")
            if payload.get("pattern", "").lower() in query.lower():
                score += 0.2
            c["rerank_score"] = min(score, 1.0)
        
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
        return candidates[:top_k]
```

---

## 10. `specs/04_knowledge/ingestion/INGESTION_PIPELINE.py`

```python
# specs/04_knowledge/ingestion/INGESTION_PIPELINE.py
"""
Main Ingestion Orchestrator.
Runs as: CLI Tool, GitHub Action, or Scheduled Service.
"""

from __future__ import annotations
import asyncio
import hashlib
import os
import tempfile
import shutil
from pathlib import Path
from typing: List, Dict, Any, Optional
from dataclasses import dataclass

from kernel.config import settings
from .PARSERS import MultiLanguageParser
from .CHUNKING_STRATEGIES import ChunkingStrategy, RetrievalChunk
from .ENRICHMENT import EnrichmentPipeline
from ..storage.QDRANT_CLIENT import QdrantKB
from ..storage.KUZU_CLIENT import KuzuGraph
from kernel.llm.client import LiteLLMClient

@dataclass
class IngestionConfig:
    repo_url: str
    branch: str = "main"
    repo_name: str = "" # Auto from URL
    framework_version: str = ""
    vertical_tags: List[str] = None
    include_patterns: List[str] = ["**/*.ts", "**/*.tsx", "**/*.py", "**/*.go", "**/*.rs", "**/*.json", "**/*.yml", "**/*.md"]
    exclude_patterns: List[str] = ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.git/**", "**/__pycache__/**", "**/*.test.ts", "**/*.spec.ts"]
    batch_size: int = 100
    enrich: bool = True

class IngestionPipeline:
    def __init__(self, config: IngestionConfig):
        self.config = config
        self.parser = MultiLanguageParser()
        self.chunker = ChunkingStrategy(
            repo_name=config.repo_name or self._extract_repo_name(config.repo_url),
            repo_url=config.repo_url,
            branch=config.branch,
            framework_version=config.framework_version
        )
        self.vector_db = QdrantKB(settings.vector_db.qdrant_url, settings.vector_db.api_key)
        self.graph_db = KuzuGraph(settings.knowledge.graph_db_path)
        self.llm = LiteLLMClient()
        self.enricher = EnrichmentPipeline(self.llm) if config.enrich else None
        
        self._temp_dir: Optional[Path] = None

    def _extract_repo_name(self, url: str) -> str:
        return url.split("/")[-1].replace(".git", "").replace(".", "_")

    async def run(self) -> Dict[str, Any]:
        """Full Pipeline: Clone -> Parse -> Chunk -> Enrich -> Embed -> Store."""
        stats = {"files": 0, "symbols": 0, "chunks": 0, "vectors": 0, "errors": 0}
        
        try:
            # 1. Clone / Checkout
            repo_path = await self._prepare_repo()
            
            # 2. Scan Files
            files = self._scan_files(repo_path)
            stats["files"] = len(files)
            
            # 3. Parse & Chunk (Streaming to avoid memory issues)
            all_chunks = []
            file_content_map = {}
            
            for file_path in files:
                try:
                    content = file_path.read_bytes()
                    file_content_map[str(file_path.relative_to(repo_path))] = content.decode('utf-8', errors='ignore')
                    symbols = self.parser.parse_file(file_path.relative_to(repo_path), content)
                    stats["symbols"] += len(symbols)
                except Exception as e:
                    stats["errors"] += 1
                    print(f"[Ingest] Parse error {file_path}: {e}")
            
            # 4. Chunking
            chunks = self.chunker.create_chunks(symbols, file_content_map)
            stats["chunks"] = len(chunks)
            
            # 5. Enrichment (LLM)
            if self.enricher:
                chunks = await self.enricher.enrich_chunks(chunks)
            
            # 6. Embedding
            vectors = await self._embed_chunks(chunks)
            stats["vectors"] = len(vectors)
            
            # 7. Store
            await self.vector_db.upsert_chunks(chunks, vectors)
            self.graph_db.upsert_chunks(chunks)
            
            # 8. Store Raw MD (Debug/Backup)
            await self._store_raw_chunks(chunks)
            
        finally:
            self._cleanup()
            
        return stats

    async def _prepare_repo(self) -> Path:
        if self.config.repo_url.startswith("http"):
            self._temp_dir = Path(tempfile.mkdtemp(prefix="autogen_ingest_"))
            # Use git clone --depth=1 --branch
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth=1", "--branch", self.config.branch, 
                self.config.repo_url, str(self._temp_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Git clone failed for {self.config.repo_url}")
            return self._temp_dir
        else:
            # Local path
            return Path(self.config.repo_url)

    def _scan_files(self, root: Path) -> List[Path]:
        import pathspec
        # Compile gitignore-style patterns
        spec = pathspec.PathSpec.from_lines('gitwildmatch', self.config.exclude_patterns)
        include_spec = pathspec.PathSpec.from_lines('gitwildmatch', self.config.include_patterns)
        
        files = []
        for f in root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                if not spec.match_file(rel) and include_spec.match_file(rel):
                    files.append(f)
        return files

    async def _embed_chunks(self, chunks: List[RetrievalChunk]) -> List[List[float]]:
        # Batch embedding via API (OpenAI/Cohere/Local)
        texts = [c.content for c in chunks]
        # return await self.llm.embed_batch(texts) 
        return [[0.0]*768] * len(texts) # Placeholder

    async def _store_raw_chunks(self, chunks: List[RetrievalChunk]):
        # Save to S3/MinIO or local FS for debugging
        pass

    def _cleanup(self):
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self.graph_db.close()
```

---

## 11. `specs/04_knowledge/cli/INGEST_CLI.py`

```python
# specs/04_knowledge/cli/INGEST_CLI.py
"""
CLI Entry Point for Ingestion.
Usage:
  autogen-knowledge ingest --repo https://github.com/vercel/next.js --branch canary --tags nextjs,react,streaming
  autogen-knowledge ingest --local ./my-project --framework-version "nextjs@14.2.0"
  autogen-knowledge reindex --vertical saas_web
"""

import asyncio
import click
from typing: List, Optional
from ..ingestion.INGESTION_PIPELINE import IngestionPipeline, IngestionConfig
from kernel.config import settings

@click.group()
def cli():
    pass

@cli.command()
@click.option("--repo", required=True, help="Git URL or Local Path")
@click.option("--branch", default="main")
@click.option("--name", default="", help="Repo name (auto from URL)")
@click.option("--framework", default="", help="Framework version tag (e.g. nextjs@14.2.0)")
@click.option("--tags", default="", help="Comma-separated tags for filtering")
@click.option("--no-enrich", is_flag=True, help="Skip LLM Enrichment (Fast mode)")
@click.option("--include", multiple=True, help="Glob patterns to include")
@click.option("--exclude", multiple=True, help="Glob patterns to exclude")
def ingest(repo, branch, name, framework, tags, no_enrich, include, exclude):
    """Ingest a repository into Knowledge Base."""
    config = IngestionConfig(
        repo_url=repo,
        branch=branch,
        repo_name=name,
        framework_version=framework,
        vertical_tags=tags.split(",") if tags else [],
        include_patterns=list(include) or None,
        exclude_patterns=list(exclude) or None,
        enrich=not no_enrich
    )
    
    click.echo(f"Starting ingestion for: {repo} ({branch})")
    pipeline = IngestionPipeline(config)
    stats = asyncio.run(pipeline.run())
    click.echo(f"✅ Done. Stats: {stats}")

@cli.command()
@click.option("--vertical", required=True, help="Vertical ID to re-index from its configured sources")
def reindex(vertical):
    """Re-index all sources defined in a Vertical's manifest."""
    # Load Vertical Manifest -> get sources -> run pipeline for each
    click.echo(f"Re-indexing vertical: {vertical} (Not implemented in spec)")

@cli.command()
@click.option("--repo", required=True)
def delete(repo):
    """Delete all data for a repository."""
    # vector_db.delete_repo(repo_name)
    click.echo(f"Deleting repo: {repo} (Not implemented in spec)")

if __name__ == "__main__":
    cli()
```

---

## 12. `specs/04_knowledge/retrieval/RERANKING.py` (Advanced)

```python
# specs/04_knowledge/retrieval/RERANKING.py
"""
Cross-Encoder Reranking & LLM Judge Reranking.
"""

from __future__ import annotations
from typing: List, Dict, Any
import numpy as np

class Reranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        # Lazy load heavy model
        self._model = None
        self.model_name = model_name

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name, max_length=512)

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        self._load()
        pairs = [(query, c["payload"].get("content", "")[:500]) for c in candidates]
        scores = self._model.predict(pairs, show_progress_bar=False)
        
        for c, score in zip(candidates, scores):
            c["rerank_score"] = float(score)
        
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
        return candidates[:top_k]

class LLMReranker:
    """LLM-as-a-Judge Reranker (High accuracy, High latency). Use for Planner/Fixer."""
    def __init__(self, llm_client, model: str = "router/reranker"):
        self.llm = llm_client
        self.model = model

    async def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        if not candidates: return []
        
        # Batch prompt for efficiency
        prompt = f"""Rate the relevance of each code snippet to the QUERY on a scale 0-10.
QUERY: {query}

SNIPPETS:
"""
        for i, c in enumerate(candidates):
            content = c["payload"].get("content", "")[:800]
            prompt += f"\n--- SNIPPET {i+1} (ID: {c['id']}) ---\n{content}\n"

        prompt += """
\nReturn JSON array: [{"id": "snippet_id", "score": 0-10, "reason": "..."}, ...]
"""
        try:
            # Use structured output for list of scores
            from pydantic import BaseModel
            class Score(BaseModel):
                id: str
                score: float
                reason: str
            
            class Scores(BaseModel):
                scores: List[Score]
            
            result = await self.llm.achat(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                response_model=Scores,
                temperature=0.0
            )
            
            score_map = {s.id: s.score for s in result.scores}
            for c in candidates:
                c["rerank_score"] = score_map.get(c["id"], 0.0)
            
            candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
            return candidates[:top_k]
        except Exception as e:
            print(f"[LLM Rerank] Failed: {e}. Falling back to vector scores.")
            return candidates[:top_k]
```

---

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №4)

> **CONTEXT FOR NEXT STEP (Vertical Spec Implementation):**
> Ты реализуешь `specs/04_knowledge/`.
> 1. Создай `kernel/knowledge/` пакет: `ingestion/` (pipeline, parsers, chunking, enrichment), `storage/` (qdrant_client, kuzu_client), `retrieval/` (engine, strategies, reranking).
> 2. Настрой `Dockerfile.knowledge` для запуска инжеста (нужен `tree-sitter` libs, `kuzu`, `qdrant-client`, `sentence-transformers`).
> 3. Реализуй `MultiLanguageParser` с Tree-sitter queries для TS, Python, Go (минимальный набор).
> 4. Реализуй `ChunkingStrategy` (Symbol + File Summary + Config).
> 5. Реализуй `EnrichmentPipeline` с батчингом и `LiteLLMClient`.
> 4. Реализуй `QdrantKB` (Hybrid Search, Payload Indexes) и `KuzuGraph` (Schema + Cypher Queries).
> 5. Реализуй `RetrievalEngine` с 3 стратегиями: `coding`, `planning`, `fixing`.
> 6. **Интеграционный тест (`tests/knowledge/test_ingestion_retrieval.py`):**
>    *   Запуск `IngestionPipeline` на маленьком тестовом репо (фикстура в `tests/fixtures/sample_repo`).
>    *   Проверка: В Qdrant появились поинты с payload `intent`, `pattern`, `tags`.
>    *   Проверка: В Kuzu есть ноды `Symbol`, `File` и ребра `CALLS`, `IMPORTS`.
>    *   Тест Retrieval: Запрос "How to stream response in Next.js?" -> возвращает чанк про `renderToReadableStream` с высоким скором.
>    *   Тест Graph: `query_callers("getUser")` -> возвращает список вызовов.

---

### ✅ Чек-лист готовности Knowledge Spec (Definition of Done для Ответа №4)

- [ ] `MultiLanguageParser` извлекает символы (func, class, interface) для TS, Python, Go.
- [ ] `ChunkingStrategy` создает 3 типа чанков: Symbol, File Summary, Config.
- [ ] `EnrichmentPipeline` батчами обогащает чанки через LLM (Intent, Pattern, Tags).
- [ ] `QdrantKB` создает коллекцию с правильными HNSW/Quantization/Indexes.
- [ ] `KuzuGraph` создает схему и загружает ноды/ребра без дубликатов (MERGE).
- [ ] `RetrievalEngine` реализует 3 стратегии с правильными фильтрами по `vertical_manifest`.
- [ ] Reranking (Cross-Encoder или LLM) улучшает Precision@K.
- [ ] CLI `ingest` успешно обрабатывает тестовый репозиторий.

---

---

# Ответ №5: Vertical Spec — SaaS Web Implementation (Next.js 14+ / React / tRPC / Prisma)

Это **первая вертикаль**, валидирующая всю платформу. Она должна генерировать **Production-Ready** SaaS приложения с авторизацией, биллингом, админкой и CI/CD «из коробки».

Сохрани в структуру:
```text
specs/05_vertical_saas_web/
├── MANIFEST.yaml
├── VERTICAL_IMPL.py
├── skills/
│   ├── CATALOG.md
│   ├── init_nextjs_app_router/          (из Ответа №3)
│   ├── init_prisma_postgres/
│   ├── add_trpc_router/
│   ├── add_nextauth_credentials/
│   ├── add_stripe_billing/
│   ├── add_shadcn_ui/
│   ├── add_dockerfile_prod/
│   ├── add_github_actions_ci/
│   ├── add_playwright_e2e/
│   └── add_admin_dashboard/
├── prompts/
│   ├── PLANNER.j2
│   ├── CODER.j2
│   ├── FIXER.j2
│   ├── REVIEWER.j2
│   └── DOCUMENTER.j2
├── verification/
│   ├── GATES.yaml
│   └── PARSERS.py
└── evals/
    ├── DATASET.jsonl
    └── RUN_EVALS.py
```

---

## 1. `specs/05_vertical_saas_web/MANIFEST.yaml`

```yaml
# specs/05_vertical_saas_web/MANIFEST.yaml
id: saas_web
name: "Modern SaaS Web Application"
version: "2.1.0"
description: |
  Production-grade SaaS starter: Next.js 14 (App Router), React 18, TypeScript, Tailwind, shadcn/ui.
  Backend: tRPC v11 (Type-safe API), Prisma ORM (PostgreSQL), NextAuth v5 (Auth).
  Billing: Stripe Subscriptions + Portal. Deploy: Docker, GitHub Actions, Vercel/AWS.
  Quality: Strict TS, ESLint, Prettier, Husky, Jest/Vitest, Playwright, Semgrep.

# --- Runtime Environment ---
runtime:
  docker_image: "ghcr.io/autogen/sandbox-saas-web:v2.1.0" # Built from DOCKERFILE.sandbox.saas_web
  cpu: 2
  memory_mb: 4096
  env_vars:
    NODE_ENV: "development"
    NEXT_TELEMETRY_DISABLED: "1"
  ports: [3000, 5432, 6379] # Next.js, Postgres (if local), Redis (if local)

# --- Tech Stack (Used for RAG Filters & Prompt Context) ---
tech_stack:
  language: "typescript"
  framework: "nextjs@14.2"
  runtime: "node@20"
  package_manager: "pnpm@9"
  ui: "tailwind@3.4,shadcn-ui@latest"
  orm: "prisma@5"
  api: "trpc@11"
  auth: "next-auth@5"
  db: "postgresql@16"
  validation: "zod@3"
  testing: "vitest@1,playwright@1"
  linting: "eslint@8,typescript-eslint@7,prettier@3"
  ci: "github-actions"

# --- Skills Registry (Loaded from skills/ dir) ---
skills_dir: "skills"
# Skills are auto-discovered. Key skills listed for Planner context:
key_skills:
  - init_nextjs_app_router
  - init_prisma_postgres
  - add_trpc_router
  - add_nextauth_credentials
  - add_stripe_billing
  - add_shadcn_ui
  - add_dockerfile_prod
  - add_github_actions_ci
  - add_playwright_e2e
  - add_admin_dashboard

# --- Planner Configuration ---
planner:
  system_prompt: "prompts/PLANNER.j2"
  few_shot_examples: "prompts/examples/planner_saas.jsonl"
  max_tasks: 25
  task_granularity: "feature" # One task = one vertical slice (e.g. "User Settings Page")
  default_constraints:
    - "Follow Next.js 14 App Router conventions (Server Components by default)"
    - "Use tRPC for all client-server communication (no raw fetch)"
    - "Database access ONLY via Prisma in tRPC procedures"
    - "UI via shadcn/ui + Tailwind. No custom CSS unless necessary."
    - "Auth via NextAuth (Credentials + OAuth). Protect routes via middleware."
    - "Strict TypeScript: no 'any', strict null checks."
    - "All mutations validated by Zod schemas shared via tRPC."

# --- Verification Gates (The Quality Contract) ---
verification:
  gates_config: "verification/GATES.yaml"
  parsers_module: "verification.PARSERS"

# --- Prompt Templates ---
prompts:
  planner: "prompts/PLANNER.j2"
  coder: "prompts/CODER.j2"
  fixer: "prompts/FIXER.j2"
  reviewer: "prompts/REVIEWER.j2"
  documenter: "prompts/DOCUMENTER.j2"

# --- RAG Collections (Filter in Knowledge Base) ---
rag_collections:
  - "nextjs_docs_v14"
  - "trpc_v11_patterns"
  - "prisma_orm_v5"
  - "nextauth_v5"
  - "shadcn_ui_components"
  - "stripe_integration"
  - "internal_saas_patterns" # Your private best practices
  - "security_patterns_owasp"

# --- Compatibility ---
compatible_kernels: [">=1.0.0"]
```

---

## 2. `specs/05_vertical_saas_web/VERTICAL_IMPL.py`

```python
# specs/05_vertical_saas_web/VERTICAL_IMPL.py
"""
Concrete Vertical Implementation for SaaS Web.
Implements IVertical protocol with SaaS-specific logic.
"""

from __future__ import annotations
from typing: Dict, List, Any, Optional
from pathlib import Path

from kernel.protocols import IVertical, VerticalManifest, SkillDef, IVerificationGate, IVertical, ISkillExecutor
from kernel.state import AgentState, Task, VerificationGateResult, VerificationGateStatus, FileChange
from kernel.tools.registry import ToolRegistry
from kernel.skills.registry import SkillRegistry
from kernel.skills.engine import TemplateEngine
from .verification.GATES import SAAS_GATES, SaaSGateParser
from .prompts.compiler import PromptCompiler

class SaaSWebVertical(IVertical):
    def __init__(self, manifest: VerticalManifest, tool_registry: ToolRegistry, skill_registry: SkillRegistry):
        self.manifest = manifest
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry
        self.prompt_compiler = PromptCompiler(Path(manifest.prompts_dir))
        # Gate Instances
        self.gates = {g.id: g for g in SAAS_GATES}
        self.gate_parser = SaaSGateParser()

    @property
    def skills(self) -> Dict[str, SkillDef]:
        return self.skill_registry._skills

    async def initialize_state(self, request: "GenerateRequest") -> Dict[str, Any]:
        """Called by Kernel `initialize_node`."""
        # 1. Create Sandbox (handled by Kernel, but we can specify env)
        # 2. Return initial state delta
        return {
            "vertical_manifest": self.manifest.model_dump(),
            "available_skills": {k: v.model_dump() for k, v in self.skills.items()},
            "tech_stack_hints": self.manifest.tech_stack,
            "sandbox_env": self.manifest.runtime.env_vars,
            "metadata": {
                "project_structure": {
                    "src/app/(auth)": "Auth pages (login, register)",
                    "src/app/(dashboard)": "Protected dashboard layout",
                    "src/app/api/trpc": "tRPC HTTP handler",
                    "src/server/api/routers": "tRPC Routers (Procedures)",
                    "src/server/db": "Prisma Client & Schema",
                    "src/lib/auth": "NextAuth Config",
                    "src/components/ui": "shadcn/ui Components",
                }
            }
        }

    def get_planner_prompt(self, state: AgentState) -> str:
        return self.prompt_compiler.render("PLANNER.j2", {
            "state": state,
            "manifest": self.manifest,
            "skills": self.skills,
            "tech_stack": self.manifest.tech_stack,
        })

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        # Retrieve RAG context for this specific task
        rag_tool = self.tool_registry.get("rag")
        # In real impl, we'd call rag_tool.execute here or have it pre-fetched in state
        # For prompt template, we pass the retrieved context from state
        return self.prompt_compiler.render("CODER.j2", {
            "state": state,
            "task": task,
            "skills": self.skills,
            "retrieved_context": state.get("rag_context", ""),
            "file_structure": state.get("metadata", {}).get("project_structure", {}),
        })

    def get_fixer_prompt(self, state: AgentState, task: Task) -> str:
        return self.prompt_compiler.render("FIXER.j2", {
            "state": state,
            "task": task,
            "failed_gates": state.get("current_gate_results", []),
            "files_to_fix": state.get("metadata", {}).get("files_to_fix", []),
        })

    def get_verification_gates(self, state: AgentState, task: Optional[Task]) -> List[IVerificationGate]:
        """Select gates based on task type or run all project gates at end."""
        if task is None:
            # Final Project Verification (Run ALL gates)
            return list(self.gates.values())
        
        # Task-Level Gates (Fast feedback)
        skill = self.skills.get(task.skill_id) if task.skill_id else None
        tags = skill.tags if skill else []
        
        # Mapping: Skill Tags -> Required Gates
        gate_map = {
            "typescript": ["lint_ts", "typecheck_ts"],
            "prisma": ["prisma_validate", "prisma_generate"],
            "trpc": ["typecheck_ts"], # tRPC types checked by TS
            "ui": ["lint_ts"], # Component linting
            "auth": ["typecheck_ts", "lint_ts"],
            "billing": ["typecheck_ts", "lint_ts"],
            "docker": ["docker_build"],
            "ci": ["yaml_lint"],
        }
        
        required_gate_ids = set()
        for tag in tags:
            required_gate_ids.update(gate_map.get(tag, []))
        
        # Always run lint/typecheck on any code change
        required_gate_ids.update(["lint_ts", "typecheck_ts"])
        
        return [self.gates[gid] for gid in required_gate_ids if gid in self.gates]

    def get_skill_executor(self, skill_id: str) -> ISkillExecutor:
        return self.skill_registry.get_executor(skill_id)

    async def on_task_complete(self, state: AgentState, task: Task):
        """Hook: Update derived artifacts after task."""
        # Example: If Prisma schema changed -> Regenerate tRPC types? 
        # Handled by `prisma generate` in skill post_scripts usually.
        # But if we added a tRPC router, we might want to update root router file.
        if task.skill_id == "add_trpc_router":
            await self._update_root_router(state, task)

    async def _update_root_router(self, state: AgentState, task: Task):
        """Append new router import to root app router."""
        router_name = task.inputs.get("router_name")
        if not router_name: return
        
        fs_tool = self.tool_registry.get("filesystem")
        sandbox_id = state["sandbox_id"]
        workspace = state["workspace_path"]
        file_path = "src/server/api/root.ts"
        
        read_res = await fs_tool.execute(sandbox_id, {"action": "read", "path": file_path}, {})
        if not read_res.success: return
        
        content = read_res.data["content"]
        import_line = f"import {{ {router_name}Router }} from \"./routers/{router_name}\";"
        router_entry = f"  {router_name}: {router_name}Router,"
        
        # Simple string manipulation (Tree-sitter better for production)
        if import_line not in content:
            # Find last import
            lines = content.splitlines()
            insert_idx = 0
            for i, line in enumerate(lines):
                if line.startswith("import ") and "routers/" in line:
                    insert_idx = i + 1
            lines.insert(insert_idx, import_line)
            content = "\n".join(lines)
        
        if router_entry not in content:
            # Find merge call
            content = content.replace("mergeRouters({", f"mergeRouters({{\n  {router_entry}")
            
        await fs_tool.execute(sandbox_id, {
            "action": "write", "path": file_path, "content": content, "mode": "update"
        }, {})

    async def finalize(self, state: AgentState) -> AgentState:
        """Generate final docs: README, ARCHITECTURE, API.md, CHANGELOG."""
        doc_tool = self.tool_registry.get("shell") # Or dedicated documenter agent
        # Documenter Node handles this, but Vertical can prep data.
        state["metadata"]["final_docs"] = {
            "readme_sections": ["Features", "Tech Stack", "Getting Started", "Deployment", "Env Variables"],
            "architecture_md": "Generated by Documenter Node",
            "api_md": "Generated from tRPC OpenAPI export",
        }
        return state
```

---

## 3. `specs/05_vertical_saas_web/skills/CATALOG.md`

````markdown
# SaaS Web Vertical — Skill Catalog

**Total Skills:** 15
**Categories:** Scaffold (4), Config (3), Feature (5), Infra (3)

| Skill ID | Category | Depends On | Description | Key Outputs |
| :--- | :--- | :--- | :--- | :--- |
| **init_nextjs_app_router** | Scaffold | — | Next.js 14 App Router, TS, Tailwind, ESLint, Prettier, Husky, pnpm | `package.json`, `tsconfig.json`, `tailwind.config.ts`, `.eslintrc`, `.prettierrc`, `next.config.mjs`, `src/app/layout.tsx`, `src/app/globals.css` |
| **init_prisma_postgres** | Scaffold | `init_nextjs` | Prisma Schema (`User`, `Account`, `Session`, `VerificationToken` for NextAuth), `DATABASE_URL`, `prisma generate` | `prisma/schema.prisma`, `.env.example`, `src/server/db/client.ts` |
| **add_trpc_router** | Feature | `init_prisma` | tRPC v11 Setup: `initTRPC`, `superjson`, `Zod`, Context, Router merge, React Query Provider | `src/trpc/*`, `src/server/api/root.ts`, `src/server/api/trpc.ts`, `src/utils/trpc.tsx` |
| **add_nextauth_credentials** | Feature | `init_prisma`, `add_trpc` | NextAuth v5 (Auth.js): Credentials + GitHub/GitHub OAuth, JWT Strategy, Middleware Protection, `auth.ts` | `src/lib/auth.ts`, `src/middleware.ts`, `src/app/(auth)/login`, `src/app/api/auth/[...nextauth]/route.ts` |
| **add_stripe_billing** | Feature | `add_nextauth`, `add_trpc` | Stripe Subscriptions: Products/Prices, Checkout Portal, Webhooks (`stripe-webhook`), Prisma `Subscription` model | `src/server/api/routers/billing.ts`, `src/app/api/webhooks/stripe/route.ts`, `prisma/schema.prisma` (updates) |
| **add_shadcn_ui** | Config | `init_nextjs` | `pnpm dlx shadcn-ui@latest add button input form dialog toast...` + `cn` utility, `TailwindAnimate` | `components.json`, `src/components/ui/*`, `src/lib/utils.ts` |
| **add_admin_dashboard** | Feature | `add_nextauth`, `add_shadcn`, `add_trpc` | Admin Panel: User Table (TanStack Table), Impersonation, Role Management, Stats Cards | `src/app/(dashboard)/admin/*`, `src/server/api/routers/admin.ts` |
| **add_dockerfile_prod** | Infra | `init_nextjs` | Multi-stage Dockerfile: Base -> Builder -> Runner. Standalone output. Non-root user. Healthcheck. | `Dockerfile`, `.dockerignore`, `docker-compose.yml` (app, db, redis) |
| **add_github_actions_ci** | Infra | `init_nextjs` | CI Pipeline: Lint, Typecheck, Test (Unit+E2E), Build, Docker Build/Push, Deploy Preview. | `.github/workflows/ci.yml`, `.github/workflows/deploy.yml` |
| **add_playwright_e2e** | Config | `add_nextauth` | Playwright Config: Auth setup (storageState), Test Utils, CI Reporter, Critical Path Tests. | `playwright.config.ts`, `tests/e2e/*.spec.ts`, `tests/auth.setup.ts` |
| **add_sentry_monitoring** | Infra | `add_dockerfile` | Sentry Init (Client/Server/Edge), Source Maps Upload, Performance Tracing. | `sentry.client.config.ts`, `sentry.server.config.ts`, `sentry.edge.config.ts`, `next.config.mjs` (updates) |
| **add_tailwind_content_paths** | Config | `add_shadcn` | Auto-configures `content` in `tailwind.config.ts` for all `src/**` + shadcn paths. | `tailwind.config.ts` (update) |
| **add_zod_env_validation** | Config | `init_nextjs` | `src/env.mjs` using `@t3-oss/env-nextjs` for type-safe `process.env`. | `src/env.mjs`, `.env.example` (update) |
| **add_api_rate_limiting** | Feature | `add_trpc` | tRPC Middleware: Upstash Redis / In-Memory Rate Limiter per User/IP. | `src/server/api/trpc.ts` (middleware), `src/lib/rate-limit.ts` |
| **add_feature_flags** | Feature | `add_trpc` | LaunchDarkly / Unleash / Custom Provider integration via tRPC Context. | `src/lib/flags.ts`, `src/server/api/trpc.ts` (context) |

---

### Skill Dependency Graph (Mermaid)

```mermaid
graph TD
    A[init_nextjs_app_router] --> B[init_prisma_postgres]
    A --> C[add_shadcn_ui]
    A --> D[add_zod_env_validation]
    A --> E[add_tailwind_content_paths]
    
    B --> F[add_trpc_router]
    F --> G[add_nextauth_credentials]
    F --> H[add_stripe_billing]
    F --> I[add_admin_dashboard]
    F --> J[add_api_rate_limiting]
    F --> K[add_feature_flags]
    
    G --> I
    C --> I
    C --> H
    
    A --> L[add_dockerfile_prod]
    L --> M[add_github_actions_ci]
    M --> N[add_playwright_e2e]
    L --> O[add_sentry_monitoring]
```
````

---

## 4. `specs/05_vertical_saas_web/prompts/PLANNER.j2`

```jinja
{# specs/05_vertical_saas_web/prompts/PLANNER.j2 #}
{# SYSTEM PROMPT FOR PLANNER AGENT #}

<system>
You are a **Principal Software Architect** specializing in **Next.js 14 / tRPC / Prisma / Stripe** SaaS systems.
Your job: Decompose a Product Requirements Document (PRD) into a **DAG of Atomic Tasks** executable by the Skill System.

# CORE PRINCIPLES
1.  **Vertical Slices:** Each task = One User-Facing Feature (e.g. "User Settings Page"), NOT "Create Button Component".
2.  **Skill-First:** Map every task to an EXISTING SKILL ID from the catalog. If no skill exists, use `freeform_coding` (rare).
3.  **Dependency Order:** Database/Infra -> API/Backend -> UI/Frontend -> Tests/Docs.
4.  **Type-Safety First:** Shared Zod schemas (tRPC input/output) are the source of truth.
5.  **Security by Default:** Auth checks in tRPC procedures (`protectedProcedure`), Rate Limiting, Input Validation.

# TECH STACK CONSTRAINTS (MANDATORY)
- **Framework:** Next.js 14.2+ (App Router, Server Components Default)
- **Language:** TypeScript 5.4+ (Strict Mode, `noUncheckedIndexedAccess`)
- **API:** tRPC v11 (Procedures: `query`, `mutation`, `subscription`)
- **Database:** Prisma 5 + PostgreSQL (Migrations only, `prisma db push` forbidden in prod)
- **Auth:** NextAuth v5 (Auth.js) - Credentials + OAuth. Middleware protection.
- **UI:** Tailwind CSS + shadcn/ui (Radix UI primitives). `cn` utility for classes.
- **Validation:** Zod v3 (Shared between Client/Server via tRPC).
- **State:** React Query (via tRPC React hooks). Server State > Client State.
- **Styling:** Tailwind only. No CSS Modules, No Styled Components.
- **Testing:** Vitest (Unit), Playwright (E2E). `data-testid` for selectors.

# AVAILABLE SKILLS (Reference)
{% for skill_id, skill in skills.items() %}
- **{{ skill_id }}** (Tags: {{ skill.tags | join(', ') }}): {{ skill.description }}
  Inputs: {{ skill.inputs_schema | to_json }}
{% endfor %}

# OUTPUT FORMAT
Return a **JSON Object** matching `PlannerOutput` schema:
{
  "spec_markdown": "string (Detailed SPEC.md)",
  "architecture_markdown": "string (ARCHITECTURE.md with Mermaid diagrams)",
  "task_graph": [
    {
      "id": "task_1",
      "name": "Initialize Next.js Project",
      "description": "Scaffold Next.js 14 with TS, Tailwind, ESLint",
      "skill_id": "init_nextjs_app_router",
      "inputs": {"project_name": "my-saas", "package_manager": "pnpm"},
      "depends_on": []
    },
    ...
  ]
}

# EXAMPLES (Few-Shot)
{{ few_shot_examples }}
</system>

<user>
**USER REQUIREMENTS:**
{{ user_prompt }}

**TECH STACK HINTS:**
{{ tech_stack_hints | to_json }}

**CONSTRAINTS:**
{% for c in constraints %}- {{ c }}
{% endfor %}

**CONTEXT FILES:**
{% for f in context_files %}- {{ f.name }}: {{ f.content[:500] }}...
{% endfor %}

Generate the **PlannerOutput JSON** now.
</user>
```

---

## 5. `specs/05_vertical_saas_web/prompts/CODER.j2`

````jinja
{# specs/05_vertical_saas_web/prompts/CODER.j2 #}

<system>
You are a **Senior Full-Stack Engineer** implementing a single task in a Next.js/tRPC/Prisma SaaS codebase.

# MODE OF OPERATION
1.  **READ FIRST:** Use `filesystem.read`, `lsp.goto_definition`, `rag.retrieve` to understand existing code.
2.  **SKILL OR CODE:** 
    - If `task.skill_id` exists -> You are **parameterizing a template**. The Skill Executor handles rendering. You only need to provide the *exact inputs* for the skill if they require logic (e.g., complex Zod schema). Usually, the Planner provides inputs. **You just confirm.**
    - If `task.skill_id` is `freeform_coding` -> **Write the code** using `filesystem.write`/`edit`.
3.  **FOLLOW PATTERNS:** Mimic the retrieved examples (`retrieved_context`) exactly.

# CODING STANDARDS (NON-NEGOTIABLE)
- **Server Components First:** `async function Page() { const data = await db... }`. No `useEffect` for data fetching.
- **tRPC Procedures:** 
  ```typescript
  // GOOD
  export const userRouter = createTRPCRouter({
    getAll: protectedProcedure.query(async ({ ctx }) => ctx.db.user.findMany()),
    update: protectedProcedure.input(z.object({id: z.string(), name: z.string()})).mutation(...)
  })
  ```
- **Prisma:** Use `ctx.db` (transactional). No raw SQL unless necessary. `select` / `include` explicitly.
- **Forms:** React Hook Form + Zod Resolver + `shadcn/ui` Form components.
- **Errors:** `TRPCError` with codes (`NOT_FOUND`, `UNAUTHORIZED`, `BAD_REQUEST`).
- **Imports:** `@/` alias. Group: External -> Internal -> Relative.
- **Naming:** `PascalCase` Components, `camelCase` vars, `kebab-case` files.

# RETRIEVED CONTEXT (Best Practices & Examples)
{{ retrieved_context }}

# CURRENT TASK
**Task ID:** {{ task.id }}
**Skill:** {{ task.skill_id | default("freeform_coding") }}
**Description:** {{ task.description }}
**Inputs:** {{ task.inputs | to_json }}

# WORKSPACE STATE
**Current File Structure:**
{{ file_structure | to_json }}

**Open Files (Context):**
{% for f in open_files %}
--- {{ f.path }} ---
{{ f.content }}
{% endfor %}
</system>

<user>
Execute the task. 
If Skill: Confirm inputs are correct or provide corrected inputs JSON.
If Freeform: Output **FileChanges** (list of {path, content, action}) via `filesystem.write` tool calls.
</user>
````

---

## 6. `specs/05_vertical_saas_web/verification/GATES.yaml`

```yaml
# specs/05_vertical_saas_web/verification/GATES.yaml
# Definition of all Verification Gates for this Vertical.

gates:
  # --- FAST: Run on every Task ---
  - id: lint_ts
    name: "TypeScript ESLint"
    description: "Lint all TS/TSX files with strict rules."
    command: "pnpm run lint"
    runs_on_every_task: true
    timeout_sec: 60
    parser: "parse_eslint"
    severity: "error" # Fail on any error

  - id: typecheck_ts
    name: "TypeScript Type Check"
    description: "Run `tsc --noEmit` for full type safety."
    command: "pnpm run typecheck"
    runs_on_every_task: true
    timeout_sec: 120
    parser: "parse_tsc"
    severity: "error"

  - id: prisma_validate
    name: "Prisma Schema Validation"
    description: "Validate Prisma syntax and format."
    command: "pnpm prisma validate"
    runs_on_every_task: false # Only on Prisma tasks
    tags: ["prisma"]
    parser: "parse_prisma"
    severity: "error"

  - id: prisma_generate
    name: "Prisma Client Generation"
    description: "Generate Prisma Client after schema changes."
    command: "pnpm prisma generate"
    runs_on_every_task: false
    tags: ["prisma"]
    severity: "error"

  # --- MEDIUM: Run on Feature Completion / Pre-Merge ---
  - id: test_unit
    name: "Unit & Integration Tests (Vitest)"
    description: "Run unit tests for utils, hooks, procedures."
    command: "pnpm run test:unit -- --passWithNoTests"
    runs_on_every_task: false
    tags: ["test"]
    timeout_sec: 180
    parser: "parse_vitest"
    severity: "error"

  - id: test_contract
    name: "tRPC Contract Tests"
    description: "Validate tRPC router types & procedures."
    command: "pnpm run test:contract"
    runs_on_every_task: false
    tags: ["trpc"]
    severity: "error"

  - id: build_nextjs
    name: "Next.js Production Build"
    description: "Full `next build` verification (compiles, generates routes)."
    command: "pnpm run build"
    runs_on_every_task: false # Run at end or on docker tasks
    timeout_sec: 300
    parser: "parse_nextjs_build"
    severity: "error"

  - id: docker_build
    name: "Docker Image Build"
    description: "Build production Docker image (multi-stage)."
    command: "docker build -t saas-web:test ."
    runs_on_every_task: false
    tags: ["docker"]
    timeout_sec: 300
    severity: "error"

  # --- SLOW: Final Verification Only ---
  - id: test_e2e
    name: "End-to-End Tests (Playwright)"
    description: "Critical user journeys: Signup, Login, Checkout, Admin."
    command: "pnpm run test:e2e -- --project=chromium"
    runs_on_every_task: false
    timeout_sec: 600
    parser: "parse_playwright"
    severity: "error" # Could be 'warning' for flaky tests

  - id: security_audit
    name: "Security Audit (Semgrep + npm audit)"
    description: "Static Analysis (Semgrep OWASP) + Dependency Audit."
    command: |
      pnpm audit --prod --audit-level=high && 
      semgrep scan --config=p/owasp-top-ten --config=p/secrets .
    runs_on_every_task: false
    timeout_sec: 180
    parser: "parse_semgrep"
    severity: "warning" # Don't block on low-sev findings

  - id: bundle_analyze
    name: "Bundle Size Analysis"
    description: "Check for regressions in JS bundle size."
    command: "ANALYZE=true pnpm run build && next-bundle-analyzer"
    runs_on_every_task: false
    tags: ["performance"]
    severity: "info"
```

---

## 7. `specs/05_vertical_saas_web/verification/PARSERS.py`

```python
# specs/05_vertical_saas_web/verification/PARSERS.py
"""
Parsers for Verification Gate Outputs.
Extracts: Structured Issues, File Paths, Line Numbers for Fixer Agent.
"""

import re
import json
from typing: List, Dict, Any
from kernel.state import VerificationGateResult, VerificationGateStatus

class SaaSGateParser:
    """Parse stdout/stderr from SaaS Web Gates into structured issues."""

    @staticmethod
    def parse_eslint(result: VerificationGateResult) -> VerificationGateResult:
        """ESLint JSON Output (`eslint -f json`)."""
        if not result.stdout: return result
        try:
            data = json.loads(result.stdout)
            issues = []
            files = set()
            for file_report in data:
                filepath = file_report.get("filePath", "")
                for msg in file_report.get("messages", []):
                    if msg.get("severity") == 2: # Error
                        issues.append({
                            "file": filepath,
                            "line": msg.get("line"),
                            "column": msg.get("column"),
                            "rule": msg.get("ruleId"),
                            "message": msg.get("message"),
                            "severity": "error"
                        })
                        files.add(filepath)
            result.issues = issues
            result.files_to_fix = list(files)
        except json.JSONDecodeError:
            pass # Fallback to stderr parsing
        return result

    @staticmethod
    def parse_tsc(result: VerificationGateResult) -> VerificationGateResult:
        """TypeScript Compiler Output (`tsc --noEmit --pretty false`)."""
        # Regex for: file(line,col): error TSXXXX: message
        pattern = re.compile(r"^(.*?)\:(\d+)\,\d+\:\s+(error|warning)\s+TS(\d+)\:\s+(.*)$", re.MULTILINE)
        issues = []
        files = set()
        for match in pattern.finditer(result.stdout + "\n" + result.stderr):
            file, line, sev, code, msg = match.groups()
            if sev == "error":
                issues.append({
                    "file": file.strip(), "line": int(line), "code": f"TS{code}",
                    "message": msg.strip(), "severity": sev
                })
                files.add(file.strip())
        result.issues = issues
        result.files_to_fix = list(files)
        return result

    @staticmethod
    def parse_vitest(result: VerificationGateResult) -> VerificationGateResult:
        """Vitest JSON Reporter (`vitest run --reporter=json`)."""
        if not result.stdout: return result
        try:
            data = json.loads(result.stdout)
            issues = []
            files = set()
            for suite in data.get("testResults", []):
                for test in suite.get("tests", []):
                    if test.get("status") == "fail":
                        for err in test.get("errors", []):
                            # Stack trace parsing is complex, link to test file
                            issues.append({
                                "file": suite.get("name", "unknown"),
                                "test": test.get("name"),
                                "message": err.get("message", "")[:500],
                                "stack": err.get("stack", "")[:1000],
                                "severity": "error"
                            })
                            files.add(suite.get("name", ""))
            result.issues = issues
            result.files_to_fix = list(files)
        except: pass
        return result

    @staticmethod
    def parse_nextjs_build(result: VerificationGateResult) -> VerificationGateResult:
        """Next.js Build Output. Look for 'Error:' 'Type error:' 'Module not found'."""
        # Next.js output is complex. Best: Use `--json` flag if available (experimental) or parse stderr.
        # Simplified: Find file paths in error logs.
        files = set()
        issues = []
        # Common patterns
        patterns = [
            r"(.*?)\:(\d+)\:(\d+)\s+Type error:",
            r"Module not found: Can't resolve '(.+?)' in '(.+?)'",
            r"Failed to compile\.\s+(.*?)\n",
        ]
        text = result.stdout + "\n" + result.stderr
        for pat in patterns:
            for m in re.finditer(pat, text):
                # Extract file/line heuristic
                pass # Implementation detail
        result.files_to_fix = list(files)
        return result

    @staticmethod
    def parse_playwright(result: VerificationGateResult) -> VerificationGateResult:
        """Playwright JSON Output (`--reporter=json`)."""
        if not result.stdout: return result
        try:
            # Playwright outputs one JSON per line
            for line in result.stdout.strip().split('\n'):
                data = json.loads(line)
                if data.get("type") == "test" and data.get("result", {}).get("status") == "failed":
                    result.files_to_fix.add(data.get("location", {}).get("file", ""))
        except: pass
        return result

    @staticmethod
    def parse_semgrep(result: VerificationGateResult) -> VerificationGateResult:
        """Semgrep SARIF or JSON (`--json`)."""
        if not result.stdout: return result
        try:
            data = json.loads(result.stdout)
            for res in data.get("results", []):
                if res.get("extra", {}).get("severity") == "ERROR":
                    result.files_to_fix.add(res.get("path"))
                    result.issues.append({
                        "file": res.get("path"),
                        "line": res.get("start", {}).get("line"),
                        "rule": res.get("check_id"),
                        "message": res.get("extra", {}).get("message"),
                    })
        except: pass
        return result

    @staticmethod
    def parse_prisma(result: VerificationGateResult) -> VerificationGateResult:
        """Prisma Validate/Generate Output."""
        # Prisma errors usually point to schema.prisma line
        pattern = re.compile(r"Error:.*?schema\.prisma.*?line (\d+)")
        for m in pattern.finditer(result.stderr):
            result.files_to_fix.add("prisma/schema.prisma")
            result.issues.append({"file": "prisma/schema.prisma", "line": int(m.group(1)), "message": m.group(0)})
        return result

    # Dispatcher
    PARSERS = {
        "lint_ts": parse_eslint,
        "typecheck_ts": parse_tsc,
        "test_unit": parse_vitest,
        "test_contract": parse_vitest,
        "build_nextjs": parse_nextjs_build,
        "test_e2e": parse_playwright,
        "security_audit": parse_semgrep,
        "prisma_validate": parse_prisma,
        "prisma_generate": parse_prisma,
    }

    def parse(self, gate_id: str, result: VerificationGateResult) -> VerificationGateResult:
        parser = self.PARSERS.get(gate_id)
        if parser:
            return parser(self, result) # Static method call hack
        return result
```

---

## 8. `specs/05_vertical_saas_web/skills/add_trpc_router/skill.yaml`

```yaml
# specs/05_vertical_saas_web/skills/add_trpc_router/skill.yaml
id: add_trpc_router
name: "Add tRPC Router & Procedures"
version: "1.2.0"
description: |
  Creates a new tRPC Router file with CRUD procedures (list, getById, create, update, delete)
  using protectedProcedure, Zod validation, and Prisma. Auto-registers in root router (via hook).
category: feature
tags: [trpc, backend, crud, api, zod, prisma]
depends_on: [init_trpc_setup, init_prisma_postgres]
provides: [trpc_router]

inputs:
  type: object
  required: [router_name, model_name, fields]
  properties:
    router_name:
      type: string
      pattern: '^[a-z][a-zA-Z0-9]*$'
      description: "Router key (kebab-case, e.g. 'project', 'user-settings')"
    model_name:
      type: string
      pattern: '^[A-Z][a-zA-Z0-9]*$'
      description: "Prisma Model Name (PascalCase, e.g. 'Project', 'UserSetting')"
    description:
      type: string
      default: ""
    fields:
      type: array
      description: "Fields for Create/Update Inputs (Zod)"
      items:
        type: object
        required: [name, type]
        properties:
          name: {type: string, pattern: '^[a-z][a-zA-Z0-9]*$'}
          type: {type: string, enum: [string, number, boolean, date, json, enum]}
          optional: {type: boolean, default: true}
          enum_values: {type: array, items: {type: string}}
          validation: {type: string} # e.g. "email", "url", "min(3)", "max(100)"
    permissions:
      type: object
      default: {}
      properties:
        list: {type: string, enum: [public, protected, admin], default: "protected"}
        get: {type: string, enum: [public, protected, admin], default: "protected"}
        create: {type: string, enum: [protected, admin], default: "protected"}
        update: {type: string, enum: [protected, admin], default: "protected"}
        delete: {type: string, enum: [protected, admin], default: "admin"}

template_dir: "templates"
entrypoint: "hooks.py"
post_scripts:
  - "pnpm run typecheck" # Verify types immediately
validation:
  - "test -f src/server/api/routers/{{ router_name }}.ts"
  - "pnpm run typecheck"
outputs:
  router_name: "{{ router_name }}"
  model_name: "{{ model_name }}"
  procedures: ["list", "getById", "create", "update", "delete"]
compatible_verticals: ["saas_web"]
```

---

## 9. `specs/05_vertical_saas_web/skills/add_trpc_router/templates/router.ts.j2`

```typescript
// specs/05_vertical_saas_web/skills/add_trpc_router/templates/router.ts.j2
import { createTRPCRouter, protectedProcedure, publicProcedure, adminProcedure } from "@/server/api/trpc";
import { z } from "zod";
import { TRPCError } from "@trpc/server";
import { prisma } from "@/server/db";

{% macro zod_type(field) %}
{% if field.type == "string" %}
z.string(){% if field.validation %}.{{ field.validation }}{% endif %}{% if field.optional %}.optional(){% else %}.nonempty(){% endif %}
{% elif field.type == "number" %}
z.number(){% if field.validation %}.{{ field.validation }}{% endif %}{% if field.optional %}.optional(){% endif %}
{% elif field.type == "boolean" %}
z.boolean(){% if field.optional %}.optional(){% endif %}
{% elif field.type == "date" %}
z.coerce.date(){% if field.optional %}.optional(){% endif %}
{% elif field.type == "json" %}
z.any(){% if field.optional %}.optional(){% endif %}
{% elif field.type == "enum" %}
z.enum([{% for v in field.enum_values %}'{{ v }}'{% if not loop.last %}, {% endif %}{% endfor %}]){% if field.optional %}.optional(){% endif %}
{% endif %}
{% endmacro %}

{% set create_input = namespace(fields=[]) %}
{% set update_input = namespace(fields=[]) %}
{% for field in fields %}
{% if not field.optional or loop.first %}{% set create_input.fields = create_input.fields + [field] %}{% endif %}
{% set update_input.fields = update_input.fields + [field] %}
{% endfor %}

export const {{ router_name }}Router = createTRPCRouter({
  // LIST: Paginated, Filterable
  list: {% if permissions.list == "public" %}publicProcedure{% elif permissions.list == "admin" %}adminProcedure{% else %}protectedProcedure{% endif %}
    .input(
      z.object({
        limit: z.number().min(1).max(100).default(20),
        cursor: z.string().optional(),
        {% for field in fields %}
        {% if field.type == "string" %}{{ field.name }}: z.string().optional(),{% endif %}
        {% if field.type == "boolean" %}{{ field.name }}: z.boolean().optional(),{% endif %}
        {% endfor %}
        orderBy: z.enum(["asc", "desc"]).default("desc"),
      })
    )
    .query(async ({ ctx, input }) => {
      const { limit, cursor, orderBy, ...filters } = input;
      const items = await ctx.db.{{ model_name }}.findMany({
        take: limit + 1,
        cursor: cursor ? { id: cursor } : undefined,
        where: {
          {% for field in fields %}
          {% if field.type == "string" %}{{ field.name }}: filters.{{ field.name }} ? { contains: filters.{{ field.name }}, mode: "insensitive" } : undefined,{% endif %}
          {% if field.type == "boolean" %}{{ field.name }}: filters.{{ field.name }},{% endif %}
          {% endfor %}
        },
        orderBy: { createdAt: orderBy },
      });
      let nextCursor: typeof cursor = undefined;
      if (items.length > limit) {
        const nextItem = items.pop();
        nextCursor = nextItem!.id;
      }
      return { items, nextCursor };
    }),

  // GET BY ID
  getById: {% if permissions.get == "public" %}publicProcedure{% elif permissions.get == "admin" %}adminProcedure{% else %}protectedProcedure{% endif %}
    .input(z.object({ id: z.string().cuid() }))
    .query(async ({ ctx, input }) => {
      const item = await ctx.db.{{ model_name }}.findUnique({ where: { id: input.id } });
      if (!item) throw new TRPCError({ code: "NOT_FOUND", message: "{{ model_name }} not found" });
      return item;
    }),

  // CREATE
  create: {% if permissions.create == "admin" %}adminProcedure{% else %}protectedProcedure{% endif %}
    .input(z.object({
      {% for field in create_input.fields %}
      {{ field.name }}: {{ zod_type(field) }},
      {% endfor %}
    }))
    .mutation(async ({ ctx, input }) => {
      // Authorization: Check ownership/org if needed
      return ctx.db.{{ model_name }}.create({ data: input });
    }),

  // UPDATE
  update: {% if permissions.update == "admin" %}adminProcedure{% else %}protectedProcedure{% endif %}
    .input(z.object({
      id: z.string().cuid(),
      {% for field in update_input.fields %}
      {{ field.name }}: {{ zod_type(field) }},
      {% endfor %}
    }))
    .mutation(async ({ ctx, input }) => {
      const { id, ...data } = input;
      // TODO: Add ownership check: const existing = await ctx.db.{{ model_name }}.findUnique({ where: { id } }); if (existing.userId !== ctx.session.user.id) throw...
      return ctx.db.{{ model_name }}.update({ where: { id }, data });
    }),

  // DELETE
  delete: {% if permissions.delete == "admin" %}adminProcedure{% else %}protectedProcedure{% endif %}
    .input(z.object({ id: z.string().cuid() }))
    .mutation(async ({ ctx, input }) => {
      // TODO: Add ownership check
      return ctx.db.{{ model_name }}.delete({ where: { id: input.id } });
    }),
});
```

---

## 10. `specs/05_vertical_saas_web/skills/add_trpc_router/hooks.py`

```python
# specs/05_vertical_saas_web/skills/add_trpc_router/hooks.py

from __future__ import annotations
from typing: Dict, Any, List
from kernel.state import FileChange
from ...SKILL_DEFINITION import HookContext

async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    inputs = ctx.inputs
    
    # 1. Auto-derive Zod validation strings from field types
    for field in inputs.get("fields", []):
        if field.get("type") == "string" and not field.get("validation"):
            if field["name"].endswith("_email") or field["name"] == "email":
                field["validation"] = "email"
            elif field["name"].endswith("_url") or field["name"] == "url":
                field["validation"] = "url"
            elif field["name"] in ("name", "title", "description"):
                field["validation"] = "min(1).max(255)"
    
    # 2. Ensure permissions object exists
    inputs.setdefault("permissions", {
        "list": "protected", "get": "protected", 
        "create": "protected", "update": "protected", "delete": "admin"
    })
    
    return inputs

async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    # The VerticalImpl.on_task_complete handles root router registration.
    # But we can also generate a test file here if needed.
    return file_changes

async def validate(ctx: HookContext) -> List[Any]:
    # Typecheck is run as post_script
    return []
```

---

## 11. `specs/05_vertical_saas_web/evals/DATASET.jsonl`

```jsonl
{"id": "eval_001", "category": "saas_web", "prompt": "Build a Project Management SaaS: Projects, Tasks, Comments, Team Members, Stripe Billing (Pro/Enterprise plans), Admin Dashboard.", "vertical": "saas_web", "expected_skills": ["init_nextjs_app_router", "init_prisma_postgres", "add_trpc_router", "add_nextauth_credentials", "add_stripe_billing", "add_admin_dashboard", "add_shadcn_ui", "add_dockerfile_prod", "add_github_actions_ci", "add_playwright_e2e"], "constraints": ["Strict TS", "tRPC only", "Server Components"], "success_criteria": {"build": true, "lint": true, "typecheck": true, "test_unit": true, "test_e2e": true, "docker_build": true}}
{"id": "eval_002", "category": "saas_web", "prompt": "Simple Blog Platform with NextAuth (GitHub), Prisma (Post, Comment, User), tRPC API, Tailwind UI. No billing.", "vertical": "saas_web", "expected_skills": ["init_nextjs_app_router", "init_prisma_postgres", "add_trpc_router", "add_nextauth_credentials", "add_shadcn_ui", "add_dockerfile_prod"], "success_criteria": {"build": true, "lint": true, "typecheck": true}}
{"id": "eval_003", "category": "saas_web", "prompt": "Add 'Notifications' feature to existing project: DB Model, tRPC Router (list, markRead), React Bell Icon Component, Real-time via tRPC Subscription.", "vertical": "saas_web", "expected_skills": ["add_trpc_router", "add_prisma_model"], "context": {"existing_project": true}, "success_criteria": {"build": true, "typecheck": true, "lint": true}}
{"id": "eval_004", "category": "saas_web", "prompt": "Migration: Convert Pages Router Next.js 13 project to App Router 14. Update getServerSideProps to Server Components, next-auth to Auth.js v5.", "vertical": "saas_web", "expected_skills": [], "success_criteria": {"build": true, "typecheck": true}, "note": "Requires freeform_coding heavy"}
```

---

## 12. `specs/05_vertical_saas_web/evals/RUN_EVALS.py`

```python
# specs/05_vertical_saas_web/evals/RUN_EVALS.py
"""
Evaluation Runner for SaaS Web Vertical.
Executes dataset, measures Pass@K, Cost, Latency.
"""

import asyncio
import json
from pathlib import Path
from typing: List, Dict, Any
from kernel.config import settings
from kernel.graph.builder import build_graph
from kernel.vertical.loader import VerticalLoader
from kernel.sandbox.manager import SandboxManager
from kernel.tools.registry import ToolRegistry
from kernel.llm.client import LiteLLMClient

class VerticalEvaluator:
    def __init__(self, vertical_id: str = "saas_web"):
        self.vertical_id = vertical_id
        self.loader = VerticalLoader(Path("verticals"))
        self.vertical = self.loader.load_vertical(vertical_id)
        self.llm = LiteLLMClient()
        self.sandbox_mgr = SandboxManager()
        self.tool_reg = ToolRegistry(self.sandbox_mgr)
        self.graph = build_graph(self.vertical)

    async def run_eval(self, dataset_path: Path, max_concurrent: int = 2) -> Dict[str, Any]:
        with open(dataset_path) as f:
            dataset = [json.loads(line) for line in f]
        
        sem = asyncio.Semaphore(max_concurrent)
        results = []
        
        async def run_one(item):
            async with sem:
                return await self._run_single(item)
        
        tasks = [run_one(item) for item in dataset]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return self._aggregate(results, dataset)

    async def _run_single(self, item: Dict) -> Dict:
        from kernel.state import AgentState
        from kernel.protocols import GenerateRequest
        
        request = GenerateRequest(
            prompt=item["prompt"],
            vertical_id=self.vertical_id,
            tech_stack_hints=item.get("tech_stack_hints", {}),
            constraints=item.get("constraints", []),
        )
        
        # Execute Graph
        config = {"configurable": {"thread_id": f"eval_{item['id']}", "vertical": self.vertical, "tool_registry": self.tool_reg, "llm_client": self.llm}}
        
        try:
            final_state = await self.graph.ainvoke(None, config=config) # Resume from init
            # Actually need to invoke with input first
            final_state = await self.graph.ainvoke(request.model_dump(), config=config)
            
            success = final_state.get("status") == "completed"
            gates = final_state.get("verification_history", [])
            gate_results = {g["gate_id"]: g["status"] for g in gates}
            
            return {"id": item["id"], "success": success, "gates": gate_results, "cost": final_state.get("token_usage", {}).get("cost_usd", 0), "error": final_state.get("error")}
        except Exception as e:
            return {"id": item["id"], "success": False, "error": str(e)}

    def _aggregate(self, results: List, dataset: List) -> Dict:
        total = len(dataset)
        passed = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        failed = total - passed
        avg_cost = sum(r.get("cost", 0) for r in results if isinstance(r, dict)) / max(total, 1)
        
        gate_stats = {}
        for r in results:
            if isinstance(r, dict):
                for gate, status in r.get("gates", {}).items():
                    gate_stats.setdefault(gate, {"pass": 0, "fail": 0})
                    gate_stats[gate][status.lower()] += 1
        
        return {
            "vertical": self.vertical_id,
            "total": total, "passed": passed, "failed": failed, "pass_rate": passed/total,
            "avg_cost_usd": avg_cost,
            "gate_stats": gate_stats,
            "details": results
        }

if __name__ == "__main__":
    evaluator = VerticalEvaluator()
    report = asyncio.run(evaluator.run_eval(Path("evals/DATASET.jsonl")))
    print(json.dumps(report, indent=2))
```

---

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №5)

> **CONTEXT FOR NEXT STEP (Ops Spec):**
> Ты реализуешь `specs/05_vertical_saas_web/`.
> 1. Создай `verticals/saas_web/` с `MANIFEST.yaml` и `VERTICAL_IMPL.py`.
> 2. Реализуй `SaasWebVertical` класс, наследующий `IVertical`.
> 3. Создай все 10+ скиллов в `skills/` (минимально: `init_nextjs`, `init_prisma`, `add_trpc`, `add_nextauth`, `add_shadcn`, `add_dockerfile`, `add_ci`). Используй структуру из Ответа №3.
> 4. Реализуй `verification/GATES.yaml` и `PARSERS.py` для парсинга ESLint, TSC, Vitest, Next Build, Playwright.
> 5. Напиши промпты в `prompts/` (Planner, Coder, Fixer, Reviewer, Documenter) с Jinja2.
> 6. Настрой `PromptCompiler` в `prompts/compiler.py` (загрузка few-shot примеров).
> 7. **Интеграционный тест (`tests/verticals/test_saas_web.py`):**
>    *   Запуск `GenerateRequest` с промптом `"Simple Todo App with Auth"`.
>    *   Проверка прохождения графа: `init_nextjs` -> `init_prisma` -> `add_trpc` -> `add_nextauth` -> `add_shadcn` -> `docker` -> `ci`.
>    *   Проверка артефактов: `pnpm run build` успешен, `docker build` успешен, `pnpm test:e2e` проходит (хотя бы smoke test).
>    *   Запуск `RUN_EVALS.py` на `DATASET.jsonl` -> `pass_rate > 0.8`.

---

### ✅ Чек-лист готовности Vertical Spec (Definition of Done для Ответа №5)

- [ ] `MANIFEST.yaml` валиден, все скиллы обнаруживаются `SkillRegistry`.
- [ ] `SaasWebVertical` имплементирует `IVertical` полностью (initialize, prompts, gates, skills, finalize).
- [ ] Все ключевые скиллы (`init_nextjs`, `add_trpc`, `add_nextauth`, `add_stripe`, `add_docker`, `add_ci`) имеют `skill.yaml`, `templates/`, `hooks.py` и проходят валидацию.
- [ ] `GATES.yaml` покрывает все этапы: Lint, Typecheck, Prisma, Unit Test, Build, Docker, E2E, Security.
- [ ] `PARSERS.py` извлекает `files_to_fix` и `issues` для Fixer Agent.
- [ ] Промпты используют `retrieved_context` и `skill_output` глобалы.
- [ ] Eval Dataset запускается и дает `pass_rate > 80%` на базовых сценариях.

---

---

# Ответ №6: Ops Spec — CI/CD, Observability, Cost Control, HITL UI, Deployment

Это **нервная система и кожа** платформы. Без этого слоя система — «пет-проект, который работает у меня на машине». С этим слоем — **Enterprise-ready Platform**.

Сохрани в структуру:
```text
specs/06_ops/
├── ci_cd/
│   ├── KERNEL_CI.yml              # CI для самого Kernel
│   ├── GENERATED_PROJECT_CI.yml   # Шаблон CI для генерируемых проектов (Vertical-specific)
│   ├── RELEASE_WORKFLOW.yml       # Semantic Release / Changelog
│   └── SECURITY_SCAN.yml          # SAST/DAST/Deps Scan
├── observability/
│   ├── LANGSMITH_SETUP.md
│   ├── OTEL_CONFIG.py             # OpenTelemetry Instrumentation
│   ├── METRICS.py                 # Prometheus Metrics Definitions
│   ├── GRAFANA_DASHBOARDS.json    # JSON Dashboards
│   └── LOGGING_CONFIG.py          # Structured Logging (JSON)
├── cost_control/
│   ├── BUDGET_MANAGER.py          # Token/Cost Limits Enforcement
│   ├── MODEL_ROUTER.yaml          # LiteLLM Router Config
│   ├── CACHE_STRATEGY.md          # Prompt/Embedding/Response Caching
│   └── COST_REPORTER.py           # Daily/Run Cost Reports
├── hitl/
│   ├── HITL_API.py                # FastAPI Endpoints for Human Review
│   ├── WEBSOCKET_MANAGER.py       # Real-time Log/State Streaming
│   ├── UI_COMPONENTS.md           # React Components Spec (for Frontend Team)
│   └── APPROVAL_WORKFLOW.py       # Approve/Edit/Abort Logic
├── deployment/
│   ├── DOCKERFILE.kernel          # Kernel Production Image
│   ├── DOCKERCOMPOSE.yml          # Local Dev Stack (Kernel, DBs, Sandboxes)
│   ├── HELM_CHART/                # Kubernetes Deployment
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   └── templates/
│   ├── TERRAFORM/                 # Cloud Infra (AWS/GCP/Azure)
│   │   ├── main.tf
│   │   ├── modules/
│   │   └── environments/
│   └── SANDBOX_DEPLOYMENT.md      # E2B/Daytona/Modal Scaling Config
└── scripts/
    ├── DEPLOY.sh
    ├── MIGRATE_DB.sh
    └── SEED_KNOWLEDGE.sh
```

---

## 1. `specs/06_ops/ci_cd/KERNEL_CI.yml`

```yaml
# specs/06_ops/ci_cd/KERNEL_CI.yml
# GitHub Actions Workflow for Kernel Platform Repository
# Path: .github/workflows/kernel-ci.yml

name: Kernel Platform CI

on:
  push:
    branches: [main, develop]
    paths:
      - 'kernel/**'
      - 'specs/**'
      - 'tests/**'
      - 'pyproject.toml'
      - 'Dockerfile.kernel'
  pull_request:
    branches: [main]
  workflow_dispatch:

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}/kernel
  PYTHON_VERSION: '3.11'
  NODE_VERSION: '20'

jobs:
  # --- 1. Code Quality & Static Analysis ---
  lint-and-typecheck:
    name: Lint & Typecheck
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with: { fetch-depth: 0 } # For semantic-release

      - name: Setup Python
        uses: actions/setup-python@v5
        with: { python-version: ${{ env.PYTHON_VERSION }} }

      - name: Install Dependencies (uv)
        uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - run: uv sync --all-extras --dev

      - name: Ruff Lint
        run: uv run ruff check kernel/ tests/ specs/

      - name: Ruff Format Check
        run: uv run ruff format --check kernel/ tests/ specs/

      - name: MyPy Type Check
        run: uv run mypy kernel/ --strict --show-error-codes

      - name: Check Manifest Schemas (YAML/JSON)
        run: |
          find specs/ -name "*.yaml" -exec yamllint -c .yamllint {} \;
          find specs/ -name "*.json" -exec python -m json.tool {} \; > /dev/null

  # --- 2. Unit & Integration Tests ---
  test:
    name: Unit & Integration Tests
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: autogen_test }
        ports: [5432:5432]
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 10s --health-timeout 5s --health-retries 5
      qdrant:
        image: qdrant/qdrant:v1.8.0
        ports: [6333:6333]
      redis:
        image: redis:7-alpine
        ports: [6379:6379]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ env.PYTHON_VERSION }} }
      - uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - run: uv sync --all-extras --dev

      - name: Run Pytest (Kernel Core)
        run: uv run pytest tests/kernel/ -v --cov=kernel --cov-report=xml --junitxml=report.xml
        env:
          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/autogen_test
          QDRANT_URL: http://localhost:6333
          REDIS_URL: redis://localhost:6379

      - name: Run Integration Tests (Verticals)
        run: uv run pytest tests/verticals/ -v -k "not e2e" --tb=short
        env:
          LLM_PROVIDER: "mock" # Use Mock LLM for CI speed
          SANDBOX_PROVIDER: "docker" # Requires Docker-in-Docker or ACT

      - name: Upload Coverage
        uses: codecov/codecov-action@v3
        with: { files: ./coverage.xml, flags: unittests }

  # --- 3. Contract Tests (Vertical Interfaces) ---
  contract-tests:
    name: Vertical Contract Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ env.PYTHON_VERSION }} }
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --all-extras --dev
      - name: Verify Vertical Manifests
        run: uv run python -m scripts.validate_manifests
      - name: Verify Skill Schemas
        run: uv run python -m scripts.validate_skills

  # --- 4. Docker Build & Push ---
  docker:
    name: Build & Push Kernel Image
    needs: [lint-and-typecheck, test, contract-tests]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    steps:
      - uses: actions/checkout@v4
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with: { registry: ${{ env.REGISTRY }}, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - name: Extract Metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=ref,event=branch
            type=sha,prefix=
            type=raw,value=latest,enable={{is_default_branch}}
      - name: Build and Push
        uses: docker/build-push-action@v5
        with:
          context: .
          file: ./specs/06_ops/deployment/DOCKERFILE.kernel
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  # --- 5. Semantic Release (Automated Versioning) ---
  release:
    name: Semantic Release
    needs: [docker]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions: { contents: write, packages: write, issues: write, pull-requests: write }
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0, token: ${{ secrets.GH_TOKEN }} }
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: npm ci
      - name: Release
        run: npx semantic-release
        env:
          GITHUB_TOKEN: ${{ secrets.GH_TOKEN }}
          NPM_TOKEN: ${{ secrets.NPM_TOKEN }} # If publishing PyPI package
```

---

## 2. `specs/06_ops/ci_cd/GENERATED_PROJECT_CI.yml`

```yaml
# specs/06_ops/ci_cd/GENERATED_PROJECT_CI.yml
# TEMPLATE: Rendered by `add_github_actions_ci` Skill into generated project's .github/workflows/ci.yml
# Uses Jinja2 variables: {{ project_name }}, {{ vertical_id }}, {{ docker_image }}

name: CI/CD - {{ project_name }}

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]
  workflow_dispatch:

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}
  NODE_VERSION: '20'
  PNPM_VERSION: '9'

jobs:
  # --- 1. Install & Lint (Fast Feedback) ---
  lint-and-typecheck:
    name: Lint & Typecheck
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup pnpm
        uses: pnpm/action-setup@v3
        with: { version: ${{ env.PNPM_VERSION }} }
      - name: Setup Node
        uses: actions/setup-node@v4
        with: { node-version: ${{ env.NODE_VERSION }}, cache: 'pnpm' }
      - name: Install Dependencies
        run: pnpm install --frozen-lockfile
      - name: Lint
        run: pnpm run lint
      - name: Typecheck
        run: pnpm run typecheck
      - name: Format Check
        run: pnpm run format:check

  # --- 2. Unit & Contract Tests ---
  test:
    name: Unit & Contract Tests
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: test_db }
        ports: [5432:5432]
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 10s --health-timeout 5s --health-retries 5
      redis:
        image: redis:7-alpine
        ports: [6379:6379]
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v3
      - uses: actions/setup-node@v4
        with: { node-version: ${{ env.NODE_VERSION }}, cache: 'pnpm' }
      - run: pnpm install --frozen-lockfile
      - name: Generate Prisma Client
        run: pnpm prisma generate
      - name: Run Migrations
        run: pnpm prisma migrate deploy
        env: { DATABASE_URL: postgresql://postgres:postgres@localhost:5432/test_db }
      - name: Run Unit Tests
        run: pnpm run test:unit -- --coverage
      - name: Run Contract Tests
        run: pnpm run test:contract

  # --- 3. Build Application ---
  build:
    name: Build Next.js App
    needs: [lint-and-typecheck, test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v3
      - uses: actions/setup-node@v4
        with: { node-version: ${{ env.NODE_VERSION }}, cache: 'pnpm' }
      - run: pnpm install --frozen-lockfile
      - name: Build
        run: pnpm run build
        env:
          NEXT_TELEMETRY_DISABLED: 1
          # Inject build-time env vars if needed
      - name: Upload Build Artifact
        uses: actions/upload-artifact@v4
        with: { name: nextjs-build, path: .next/, retention-days: 1 }

  # --- 4. E2E Tests (Playwright) ---
  e2e:
    name: E2E Tests
    needs: [build]
    runs-on: ubuntu-latest
    timeout-minutes: 30
    services:
      postgres: { image: postgres:16-alpine, env: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: test_db }, ports: [5432:5432] }
      redis: { image: redis:7-alpine, ports: [6379:6379] }
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v3
      - uses: actions/setup-node@v4
        with: { node-version: ${{ env.NODE_VERSION }}, cache: 'pnpm' }
      - run: pnpm install --frozen-lockfile
      - name: Install Playwright Browsers
        run: pnpm playwright install --with-deps chromium
      - name: Run E2E Tests
        run: pnpm run test:e2e -- --project=chromium
        env:
          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/test_db
          PLAYWRIGHT_BASE_URL: http://localhost:3000
      - name: Upload Playwright Report
        if: always()
        uses: actions/upload-artifact@v4
        with: { name: playwright-report, path: playwright-report/, retention-days: 7 }

  # --- 5. Docker Build & Security Scan ---
  docker:
    name: Docker Build & Scan
    needs: [build]
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write, security-events: write }
    steps:
      - uses: actions/checkout@v4
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with: { registry: ${{ env.REGISTRY }}, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - name: Build Image
        uses: docker/build-push-action@v5
        with:
          context: .
          file: ./Dockerfile
          push: false # Only push on main/deploy
          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:pr-${{ github.event.number }}
          load: true
      - name: Run Trivy Scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:pr-${{ github.event.number }}
          format: 'sarif'
          output: 'trivy-results.sarif'
      - name: Upload Trivy Results
        uses: github/codeql-action/upload-sarif@v3
        with: { sarif_file: 'trivy-results.sarif' }

  # --- 6. Deploy Preview (PR) ---
  deploy-preview:
    name: Deploy Preview (Vercel/Cloud Run)
    needs: [docker, e2e]
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to Vercel Preview
        uses: amondnet/vercel-action@v25
        with:
          vercel-token: ${{ secrets.VERCEL_TOKEN }}
          vercel-org-id: ${{ secrets.VERCEL_ORG_ID }}
          vercel-project-id: ${{ secrets.VERCEL_PROJECT_ID }}
          vercel-args: '--prod=false'
      - name: Comment PR with Preview URL
        uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: '🚀 Preview Deployment Ready: ${{ steps.deploy.outputs.url }}'
            })

  # --- 7. Deploy Production (Main Branch) ---
  deploy-prod:
    name: Deploy Production
    needs: [docker, e2e]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Push Docker Image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:latest
            ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
      - name: Deploy to Production (Vercel/AWS/Cloud Run)
        run: |
          # Example: Vercel Prod
          npx vercel --prod --token=${{ secrets.VERCEL_TOKEN }}
          # OR: Kubernetes (kubectl set image...)
          # OR: AWS ECS (ecspresso deploy...)
      - name: Create GitHub Release
        uses: softprops/action-gh-release@v1
        with:
          tag_name: v${{ github.run_number }} # Or semantic-release tag
          generate_release_notes: true
```

---

## 3. `specs/06_ops/observability/OTEL_CONFIG.py`

```python
# specs/06_ops/observability/OTEL_CONFIG.py
"""
OpenTelemetry Instrumentation for Kernel.
Traces: LangGraph execution, LLM calls, Tool calls, Sandbox exec.
Metrics: Latency, Token Usage, Cost, Error Rates, Queue Depths.
Logs: Structured JSON -> Loki/Elastic.
"""

from __future__ import annotations
import os
from typing import Optional
from opentelemetry import trace, metrics, baggage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator
from opentelemetry.propagators.baggage import BaggagePropagator
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.sdk.resources import Resource


# --- Custom Attributes (Semantic Conventions) ---
class Attr:
    RUN_ID = "autogen.run_id"
    THREAD_ID = "autogen.thread_id"
    VERTICAL = "autogen.vertical"
    NODE_NAME = "autogen.node_name"
    TASK_ID = "autogen.task_id"
    SKILL_ID = "autogen.skill_id"
    MODEL_NAME = "autogen.model_name"
    TOKEN_USAGE = "autogen.token_usage"
    COST_USD = "autogen.cost_usd"
    SANDBOX_ID = "autogen.sandbox_id"
    GATE_ID = "autogen.gate_id"
    GATE_STATUS = "autogen.gate_status"


def setup_otel(service_name: str = "autogen-kernel", endpoint: str = None):
    """Initialize OpenTelemetry SDK."""
    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: service_name,
            ResourceAttributes.SERVICE_VERSION: os.getenv("APP_VERSION", "dev"),
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: os.getenv("ENVIRONMENT", "development"),
        }
    )

    # 1. Tracer Provider
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    # trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter())) # Debug
    trace.set_tracer_provider(trace_provider)

    # 2. Meter Provider
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=10000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # 3. Propagators (W3C TraceContext + Baggage)
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), BaggagePropagator()]))

    # 4. Auto-Instrumentation
    LoggingInstrumentor().instrument(set_logging_format=True)  # JSON Logs
    HTTPXClientInstrumentor().instrument()
    RequestsInstrumentor().instrument()

    return trace.get_tracer(__name__), metrics.get_meter(__name__)


# --- Metric Instruments (Created once) ---
meter = metrics.get_meter("autogen.kernel")

METRICS = {
    "run_duration": meter.create_histogram("autogen.run.duration", unit="s", description="Total run latency"),
    "node_duration": meter.create_histogram("autogen.node.duration", unit="s", description="Node execution latency"),
    "llm_tokens": meter.create_counter("autogen.llm.tokens", unit="1", description="Tokens used (prompt/completion)"),
    "llm_cost": meter.create_counter("autogen.llm.cost", unit="USD", description="Cost in USD"),
    "llm_errors": meter.create_counter("autogen.llm.errors", unit="1", description="LLM API errors"),
    "tool_calls": meter.create_counter("autogen.tool.calls", unit="1", description="Tool invocations"),
    "tool_errors": meter.create_counter("autogen.tool.errors", unit="1", description="Tool execution errors"),
    "sandbox_exec_duration": meter.create_histogram("autogen.sandbox.exec.duration", unit="s"),
    "gate_status": meter.create_counter("autogen.gate.status", unit="1", description="Gate Pass/Fail/Error"),
    "active_runs": meter.create_up_down_counter("autogen.runs.active", unit="1", description="Concurrent active runs"),
    "queue_depth": meter.create_up_down_counter("autogen.queue.depth", unit="1", description="Pending runs in queue"),
}


# --- Helper Decorators for Nodes ---
def trace_node(node_name: str):
    """Decorator to trace a LangGraph node execution."""

    def decorator(func):
        async def wrapper(state, config, *args, **kwargs):
            tracer = trace.get_tracer(__name__)
            run_id = config.get("configurable", {}).get("thread_id", "unknown")
            vertical = (
                config.get("configurable", {}).get("vertical", {}).manifest.id
                if config.get("configurable", {}).get("vertical")
                else "unknown"
            )

            with tracer.start_as_current_span(
                f"node.{node_name}",
                attributes={Attr.RUN_ID: run_id, Attr.VERTICAL: vertical, Attr.NODE_NAME: node_name},
            ) as span:
                # Inject Baggage for downstream correlation
                ctx = baggage.set_baggage("run_id", run_id)
                token = baggage.attach(ctx)
                try:
                    METRICS["active_runs"].add(1, {Attr.VERTICAL: vertical})
                    result = await func(state, config, *args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise
                finally:
                    baggage.detach(token)
                    METRICS["active_runs"].add(-1, {Attr.VERTICAL: vertical})

        return wrapper

    return decorator


def record_llm_usage(model: str, usage: dict, cost: float, vertical: str):
    attrs = {Attr.MODEL_NAME: model, Attr.VERTICAL: vertical}
    METRICS["llm_tokens"].add(usage.get("prompt_tokens", 0), {**attrs, "type": "prompt"})
    METRICS["llm_tokens"].add(usage.get("completion_tokens", 0), {**attrs, "type": "completion"})
    METRICS["llm_cost"].add(cost, attrs)


def record_gate_result(gate_id: str, status: str, vertical: str):
    METRICS["gate_status"].add(1, {Attr.GATE_ID: gate_id, Attr.GATE_STATUS: status, Attr.VERTICAL: vertical})
```

---

## 4. `specs/06_ops/observability/GRAFANA_DASHBOARDS.json`

```json
{
  "dashboard": {
    "title": "AutoGen Platform - Operational Overview",
    "uid": "autogen-ops",
    "tags": ["autogen", "llm", "agents"],
    "timezone": "utc",
    "panels": [
      {
        "title": "Active Runs (Concurrency)",
        "type": "graph",
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        "targets": [
          {"expr": "autogen_runs_active", "legendFormat": "{{vertical}}", "refId": "A"}
        ]
      },
      {
        "title": "Run Success Rate (5m)",
        "type": "stat",
        "gridPos": {"x": 12, "y": 0, "w": 6, "h": 8},
        "targets": [
          {"expr": "sum(rate(autogen_gate_status{status=\"passed\"}[5m])) / sum(rate(autogen_gate_status[5m]))", "refId": "A"}
        ],
        "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1}}
      },
      {
        "title": "Avg Run Duration (p50, p95, p99)",
        "type": "graph",
        "gridPos": {"x": 18, "y": 0, "w": 12, "h": 8},
        "targets": [
          {"expr": "histogram_quantile(0.50, sum(rate(autogen_run_duration_bucket[5m])) by (le))", "legendFormat": "p50"},
          {"expr": "histogram_quantile(0.95, sum(rate(autogen_run_duration_bucket[5m])) by (le))", "legendFormat": "p95"},
          {"expr": "histogram_quantile(0.99, sum(rate(autogen_run_duration_bucket[5m])) by (le))", "legendFormat": "p99"}
        ]
      },
      {
        "title": "LLM Token Usage & Cost (Hourly)",
        "type": "graph",
        "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
        "targets": [
          {"expr": "sum(rate(autogen_llm_tokens_total[1h])) by (model_name, type)", "legendFormat": "{{model_name}} - {{type}}"},
          {"expr": "sum(rate(autogen_llm_cost_total[1h])) by (model_name)", "legendFormat": "Cost: {{model_name}}", "refId": "B"}
        ]
      },
      {
        "title": "Node Latency Breakdown (p95)",
        "type": "bargauge",
        "gridPos": {"x": 12, "y": 8, "w": 18, "h": 8},
        "targets": [
          {"expr": "histogram_quantile(0.95, sum(rate(autogen_node_duration_bucket[5m])) by (le, node_name))", "legendFormat": "{{node_name}}"}
        ]
      },
      {
        "title": "Verification Gate Status (Last 1h)",
        "type": "piechart",
        "gridPos": {"x": 0, "y": 16, "w": 12, "h": 8},
        "targets": [
          {"expr": "sum(increase(autogen_gate_status_total[1h])) by (gate_id, status)", "legendFormat": "{{gate_id}} - {{status}}"}
        ]
      },
      {
        "title": "Tool Call Errors (Rate)",
        "type": "graph",
        "gridPos": {"x": 12, "y": 16, "w": 12, "h": 8},
        "targets": [
          {"expr": "sum(rate(autogen_tool_errors_total[5m])) by (tool_name)", "legendFormat": "{{tool_name}}"}
        ]
      },
      {
        "title": "Sandbox Execution Time (p95)",
        "type": "graph",
        "gridPos": {"x": 24, "y": 16, "w": 12, "h": 8},
        "targets": [
          {"expr": "histogram_quantile(0.95, sum(rate(autogen_sandbox_exec_duration_bucket[5m])) by (le))", "legendFormat": "Sandbox Exec"}
        ]
      },
      {
        "title": "Queue Depth & Pending Runs",
        "type": "graph",
        "gridPos": {"x": 0, "y": 24, "w": 24, "h": 8},
        "targets": [
          {"expr": "autogen_queue_depth", "legendFormat": "Queue Depth"},
          {"expr": "autogen_runs_active", "legendFormat": "Active Runs"}
        ]
      }
    ],
    "templating": {
      "list": [
        {"name": "vertical", "type": "query", "query": "label_values(autogen_runs_active, vertical)", "default": "all", "multi": true}
      ]
    }
  }
}
```

---

## 5. `specs/06_ops/cost_control/BUDGET_MANAGER.py`

```python
# specs/06_ops/cost_control/BUDGET_MANAGER.py
"""
Budget Enforcement & Cost Tracking.
Integrates with LangGraph Checkpointer to stop runs exceeding budget.
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing: Dict, Optional
from contextlib import asynccontextmanager
from kernel.state import AgentState, TokenUsage
from kernel.config import settings

@dataclass
class BudgetConfig:
    max_cost_usd_per_run: float = 5.0
    max_tokens_per_run: int = 2_000_000
    max_cost_usd_per_day: float = 100.0
    max_tokens_per_minute: int = 500_000 # Rate limit
    alert_threshold_pct: float = 0.8 # Alert at 80%

@dataclass
class BudgetState:
    current_run_cost: float = 0.0
    current_run_tokens: int = 0
    daily_cost: float = 0.0
    daily_tokens: int = 0
    minute_tokens: int = 0
    last_minute_reset: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class BudgetManager:
    def __init__(self, config: BudgetConfig = None):
        self.config = config or BudgetConfig()
        self.state = BudgetState()
        self._alerts_sent: set = set()

    @asynccontextmanager
    async def check_budget(self, state: AgentState, estimated_cost: float = 0.0, estimated_tokens: int = 0):
        """Context manager to check budget before and after node execution."""
        async with self.state._lock:
            await self._reset_minute_window()
            
            # Pre-flight Check
            projected_cost = self.state.current_run_cost + estimated_cost
            projected_tokens = self.state.current_run_tokens + estimated_tokens
            
            if projected_cost > self.config.max_cost_usd_per_run:
                raise BudgetExceededError(f"Run cost limit exceeded: ${projected_cost:.4f} > ${self.config.max_cost_usd_per_run}")
            if projected_tokens > self.config.max_tokens_per_run:
                raise BudgetExceededError(f"Run token limit exceeded: {projected_tokens} > {self.config.max_tokens_per_run}")
            if self.state.minute_tokens + estimated_tokens > self.config.max_tokens_per_minute:
                # Wait for rate limit window
                wait_time = 60 - (asyncio.get_event_loop().time() - self.state.last_minute_reset)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    await self._reset_minute_window()
            
            # Reserve budget
            self.state.current_run_cost += estimated_cost
            self.state.current_run_tokens += estimated_tokens
            self.state.minute_tokens += estimated_tokens
            
        try:
            yield
        finally:
            # Actual usage is recorded via `record_actual_usage` callback from LLM Client
            pass

    async def record_actual_usage(self, usage: TokenUsage, vertical: str):
        """Called by LLM Client after successful call."""
        async with self.state._lock:
            self.state.current_run_cost += usage.cost_usd
            self.state.current_run_tokens += usage.total_tokens
            self.state.daily_cost += usage.cost_usd
            self.state.daily_tokens += usage.total_tokens
            self.state.minute_tokens += usage.total_tokens
            
            await self._check_alerts(vertical)

    async def _reset_minute_window(self):
        import time
        now = time.time()
        if now - self.state.last_minute_reset > 60:
            self.state.minute_tokens = 0
            self.state.last_minute_reset = now

    async def _check_alerts(self, vertical: str):
        run_pct = self.state.current_run_cost / self.config.max_cost_usd_per_run
        daily_pct = self.state.daily_cost / self.config.max_cost_usd_per_day
        
        for pct, limit_type in [(run_pct, "run"), (daily_pct, "daily")]:
            if pct >= self.config.alert_threshold_pct and f"{vertical}_{limit_type}" not in self._alerts_sent:
                self._alerts_sent.add(f"{vertical}_{limit_type}")
                # Fire Alert (Webhook, Slack, PagerDuty)
                print(f"🚨 BUDGET ALERT: {vertical} {limit_type} budget at {pct*100:.0f}%")

    def get_remaining_budget(self) -> Dict[str, float]:
        return {
            "run_cost_remaining": max(0, self.config.max_cost_usd_per_run - self.state.current_run_cost),
            "run_tokens_remaining": max(0, self.config.max_tokens_per_run - self.state.current_run_tokens),
            "daily_cost_remaining": max(0, self.config.max_cost_usd_per_day - self.state.daily_cost),
        }

    def reset_run(self):
        """Call at start of new run."""
        self.state.current_run_cost = 0.0
        self.state.current_run_tokens = 0.0

class BudgetExceededError(Exception):
    pass
```

---

## 6. `specs/06_ops/cost_control/MODEL_ROUTER.yaml`

```yaml
# specs/06_ops/cost_control/MODEL_ROUTER.yaml
# LiteLLM Router Configuration (config.yaml for LiteLLM Proxy)
# Deploy LiteLLM Proxy as a sidecar or separate service.

general:
  master_key: "sk-litellm-master-key" # Secure in production
  log_level: "INFO"
  redis_url: "redis://redis:6379/0" # For caching & rate limiting

router_settings:
  strategy: "cost-optimized" # or "latency-optimized", "fallback"
  enable_retries: true
  num_retries: 3
  retry_after: 1
  cooldown_time: 30

model_list:
  # --- PLANNER / ARCHITECT (Smart, Expensive) ---
  - model_name: "router/planner"
    litellm_params:
      model: "anthropic/claude-3-5-sonnet-20241022" # Or gpt-4o
      api_key: "${ANTHROPIC_API_KEY}"
      max_tokens: 8192
      temperature: 0.1
    model_info:
      mode: "chat"
      cost_per_1k_input: 0.003
      cost_per_1k_output: 0.015
      max_tokens: 200000
    tpm: 100000
    rpm: 500

  - model_name: "router/planner_fallback"
    litellm_params:
      model: "openai/gpt-4o-2024-08-06"
      api_key: "${OPENAI_API_KEY}"
    model_info:
      cost_per_1k_input: 0.0025
      cost_per_1k_output: 0.01

  # --- CODER / FIXER (Fast, Cheap, Good at Code) ---
  - model_name: "router/coder"
    litellm_params:
      model: "anthropic/claude-3-5-haiku-20241022"
      api_key: "${ANTHROPIC_API_KEY}"
      max_tokens: 4096
      temperature: 0.0
    model_info:
      cost_per_1k_input: 0.00025
      cost_per_1k_output: 0.00125
      max_tokens: 200000
    tpm: 200000
    rpm: 1000

  - model_name: "router/coder_fallback"
    litellm_params:
      model: "openai/gpt-4o-mini-2024-07-18"
      api_key: "${OPENAI_API_KEY}"
    model_info:
      cost_per_1k_input: 0.00015
      cost_per_1k_output: 0.0006

  # --- ENRICHMENT / CLASSIFIER (Fastest, Cheapest) ---
  - model_name: "router/enricher"
    litellm_params:
      model: "openai/gpt-4o-mini"
      api_key: "${OPENAI_API_KEY}"
      max_tokens: 1024
    model_info:
      cost_per_1k_input: 0.00015
      cost_per_1k_output: 0.0006

  - model_name: "router/classifier"
    litellm_params:
      model: "openai/gpt-4o-mini"
      api_key: "${OPENAI_API_KEY}"
      max_tokens: 50
    model_info:
      cost_per_1k_input: 0.00015
      cost_per_1k_output: 0.0006

  # --- LOCAL FALLBACK (Ollama / vLLM) ---
  - model_name: "router/local_coder"
    litellm_params:
      model: "ollama/codellama:34b-instruct"
      api_base: "http://ollama:11434"
    model_info:
      cost_per_1k_input: 0
      cost_per_1k_output: 0
      max_tokens: 16000

# --- Caching Config ---
cache:
  type: "redis"
  host: "redis"
  port: 6379
  ttl: 86400 # 24h
  enabled: true
  cache_control: true # Respect Cache-Control headers

# --- Callbacks (for Budget Manager) ---
callbacks:
  - "kernel.cost_control.budget_callback.BudgetCallbackHandler"
```

---

## 7. `specs/06_ops/hitl/HITL_API.py`

```python
# specs/06_ops/hitl/HITL_API.py
"""
FastAPI Service for Human-in-the-Loop (HITL) Interactions.
Endpoints: Get Interrupt, Submit Approval, Stream Logs.
"""

from __future__ import annotations
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing: Dict, List, Any, Optional, Literal
from contextlib import asynccontextmanager
import asyncio
import json
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from kernel.state import AgentState, RunStatus
from kernel.config import settings

# --- Models ---
class InterruptPayload(BaseModel):
    run_id: str
    interrupt_type: Literal["plan_review", "gate_failure", "destructive_action", "budget_exceeded"]
    payload: Dict[str, Any]
    actions: List[Literal["approve", "reject", "edit", "abort", "skip_gate"]]
    created_at: str

class ApprovalRequest(BaseModel):
    action: Literal["approve", "reject", "edit", "abort", "skip_gate"]
    edited_data: Optional[Dict[str, Any]] = None # For 'edit' action
    comment: Optional[str] = None

class RunStatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    progress: float # 0.0 - 1.0
    current_node: Optional[str]
    logs: List[str]

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, run_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(run_id, []).append(websocket)

    def disconnect(self, run_id: str, websocket: WebSocket):
        if run_id in self.active_connections:
            self.active_connections[run_id].remove(websocket)

    async def broadcast(self, run_id: str, message: Dict):
        if run_id in self.active_connections:
            dead = []
            for ws in self.active_connections[run_id]:
                try:
                    await ws.send_json(message)
                except:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(run_id, ws)

manager = ConnectionManager()

# --- App Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Checkpointer
    app.state.checkpointer = PostgresSaver.from_conn_string(settings.database.postgres_dsn)
    yield
    # Shutdown
    pass

app = FastAPI(title="AutoGen HITL API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- REST Endpoints ---
@app.get("/runs/{run_id}/interrupt", response_model=InterruptPayload)
async def get_interrupt(run_id: str):
    """Fetch current interrupt payload for a run."""
    # Check if graph is interrupted
    config = {"configurable": {"thread_id": run_id}}
    state_snapshot = await app.state.checkpointer.aget_tuple(config)
    if not state_snapshot or not state_snapshot.metadata.get("interrupts"):
        raise HTTPException(404, "No active interrupt")
    
    interrupt = state_snapshot.metadata["interrupts"][-1] # Latest
    return InterruptPayload(**interrupt.value)

@app.post("/runs/{run_id}/approve")
async def submit_approval(run_id: str, request: ApprovalRequest, background_tasks: BackgroundTasks):
    """Resume graph with human decision."""
    config = {"configurable": {"thread_id": run_id}}
    
    # Map action to LangGraph Command
    if request.action == "approve":
        cmd = Command(resume={"action": "approve"})
    elif request.action == "edit":
        cmd = Command(resume={"action": "edit", "data": request.edited_data})
    elif request.action == "skip_gate":
        cmd = Command(resume={"action": "skip_gate"})
    elif request.action == "abort":
        cmd = Command(resume={"action": "abort"})
    else:
        raise HTTPException(400, "Invalid action")
    
    # Resume graph in background
    background_tasks.resume_graph(run_id, cmd) # Custom background task
    
    return {"status": "resumed", "action": request.action}

@app.get("/runs/{run_id}/status", response_model=RunStatusResponse)
async def get_run_status(run_id: str):
    config = {"configurable": {"thread_id": run_id}}
    state = await app.state.checkpointer.aget(config)
    if not state:
        raise HTTPException(404, "Run not found")
    
    # Calculate progress (heuristic)
    tasks = state.get("task_graph", [])
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    progress = completed / len(tasks) if tasks else 0.0
    
    return RunStatusResponse(
        run_id=run_id,
        status=state.get("status", "unknown"),
        progress=progress,
        current_node=state.get("current_node"),
        logs=state.get("logs", [])[-50:] # Last 50 logs
    )

# --- WebSocket for Real-time Logs ---
@app.websocket("/ws/runs/{run_id}")
async def websocket_logs(websocket: WebSocket, run_id: str):
    await manager.connect(run_id, websocket)
    try:
        # Send current state immediately
        state = await app.state.checkpointer.aget({"configurable": {"thread_id": run_id}})
        if state:
            await websocket.send_json({"type": "state", "data": state})
        
        # Keep alive, listen for client messages (ping)
        while True:
            data = await websocket.receive_text()
            if data == "ping": await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(run_id, websocket)

# --- Internal Helper for Nodes to Stream Logs ---
async def stream_log(run_id: str, level: str, message: str, node: str = ""):
    """Called by Kernel Nodes to push logs to UI."""
    log_entry = {"timestamp": asyncio.get_event_loop().time(), "level": level, "node": node, "message": message}
    await manager.broadcast(run_id, {"type": "log", "data": log_entry})

# --- Background Task to Resume Graph ---
async def resume_graph_task(run_id: str, command: Command):
    from kernel.graph.builder import build_graph
    from kernel.vertical.loader import VerticalLoader
    # Reconstruct graph (or get from pool)
    # This is simplified; production needs a persistent Graph Runner Pool
    vertical = VerticalLoader(Path("verticals")).load_vertical("saas_web") # Dynamic lookup needed
    graph = build_graph(vertical)
    await graph.ainvoke(command, config={"configurable": {"thread_id": run_id}})
```

---

## 8. `specs/06_ops/deployment/DOCKERFILE.kernel`

```dockerfile
# specs/06_ops/deployment/DOCKERFILE.kernel
# Multi-stage Build for Kernel Production Image

# --- Stage 1: Base & Dependencies ---
FROM python:3.11-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=1.8.3 \
    UV_VERSION=0.3.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
RUN pip install uv==${UV_VERSION}

WORKDIR /app

# --- Stage 2: Install Dependencies ---
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --compile-bytecode

# --- Stage 3: Development / Test Image ---
FROM deps AS dev
COPY . .
RUN uv sync --frozen --all-extras --compile-bytecode
# Install Playwright/Chrome for E2E if needed in CI
# RUN uv run playwright install --with-deps chromium

# --- Stage 4: Production Runtime ---
FROM base AS runtime
ENV ENVIRONMENT=production
ENV PORT=8000

# Create non-root user
RUN groupadd -r autogen && useradd -r -g autogen -d /app -s /sbin/nologin autogen

# Copy only necessary files from deps
COPY --from=deps --chown=autogen:autogen /app/.venv /app/.venv
COPY --from=deps --chown=autogen:autogen /app/kernel /app/kernel
COPY --from=deps --chown=autogen:autogen /app/specs /app/specs
COPY --from=deps --chown=autogen:autogen /app/verticals /app/verticals

# Ensure virtual env is in PATH
ENV PATH="/app/.venv/bin:$PATH"

USER autogen

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Entry point
CMD ["python", "-m", "kernel.main", "serve"]
```

---

## 9. `specs/06_ops/deployment/HELM_CHART/values.yaml`

```yaml
# specs/06_ops/deployment/HELM_CHART/values.yaml
# Helm Chart for Kubernetes Deployment

replicaCount: 3

image:
  repository: ghcr.io/your-org/autogen-kernel
  tag: "latest"
  pullPolicy: IfNotPresent
  pullSecrets: [ghcr-secret]

service:
  type: ClusterIP
  port: 8000
  targetPort: 8000

ingress:
  enabled: true
  className: "nginx"
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/proxy-body-size: "50m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "300"
  hosts:
    - host: autogen.yourdomain.com
      paths: ["/"]
  tls:
    - secretName: autogen-tls
      hosts: [autogen.yourdomain.com]

resources:
  limits:
    cpu: "2000m"
    memory: "4Gi"
  requests:
    cpu: "500m"
    memory: "2Gi"

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 20
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 10
          periodSeconds: 60

env:
  - name: ENVIRONMENT
    value: "production"
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: autogen-secrets
        key: database-url
  - name: QDRANT_URL
    value: "http://qdrant:6333"
  - name: KUZU_PATH
    value: "/data/kuzu"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-collector:4317"
  - name: LITE_LLM_URL
    value: "http://litellm:4000"

# External Dependencies (Deployed separately via their own Helm charts)
dependencies:
  - name: postgresql
    repository: "https://charts.bitnami.com/bitnami"
    version: "12.x"
    condition: postgresql.enabled
  - name: redis
    repository: "https://charts.bitnami.com/bitnami"
    version: "18.x"
    condition: redis.enabled
  - name: qdrant
    repository: "https://qdrant.github.io/qdrant-helm"
    version: "1.x"
    condition: qdrant.enabled
  - name: loki
    repository: "https://grafana.github.io/helm-charts"
    version: "6.x"
    condition: loki.enabled

# Persistent Volume for Kuzu (Embedded Graph DB)
persistence:
  kuzu:
    enabled: true
    size: 50Gi
    storageClass: "fast-ssd"
    accessMode: ReadWriteOnce

# ConfigMap for Verticals (mounted as volume)
verticalsConfigMap:
  enabled: true
  mountPath: /app/verticals

# Pod Disruption Budget
pdb:
  enabled: true
  minAvailable: 50%

# Network Policies
networkPolicy:
  enabled: true
  ingress:
    - from:
      - namespaceSelector:
          matchLabels:
            name: ingress-nginx
      ports:
      - protocol: TCP
        port: 8000
```

---

## 10. `specs/06_ops/deployment/TERRAFORM/main.tf`

```hcl
# specs/06_ops/deployment/TERRAFORM/main.tf
# Terraform Module for AWS Cloud Infrastructure

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.23" }
    helm = { source = "hashicorp/helm", version = "~> 2.10" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {
    bucket = "autogen-terraform-state"
    key    = "prod/infra/terraform.tfstate"
    region = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "autogen"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# --- VPC & Networking ---
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"
  name    = "${var.project_name}-${var.environment}"
  cidr    = "10.0.0.0/16"
  azs             = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]
  enable_nat_gateway     = true
  single_nat_gateway     = var.environment != "prod"
  enable_dns_hostnames   = true
  enable_dns_support     = true
}

# --- EKS Cluster ---
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 19.0"
  cluster_name    = "${var.project_name}-${var.environment}"
  cluster_version = "1.28"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets
  cluster_endpoint_public_access = true
  
  eks_managed_node_group_defaults = {
    ami_type       = "AL2_x86_64"
    instance_types = ["m6i.xlarge"]
  }

  eks_managed_node_groups = {
    general = {
      name           = "general"
      instance_types = ["m6i.xlarge", "m6i.2xlarge"]
      capacity_type  = "ON_DEMAND"
      min_size       = 3
      max_size       = 20
      desired_size   = 3
      labels = { workload = "general" }
    }
    sandbox = {
      name           = "sandbox"
      instance_types = ["c6i.2xlarge", "c6i.4xlarge"] # CPU optimized for code execution
      capacity_type  = "SPOT"
      min_size       = 0
      max_size       = 10
      desired_size   = 0
      labels = { workload = "sandbox" }
      taints = [{ key = "workload", value = "sandbox", effect = "NO_SCHEDULE" }]
    }
  }
}

# --- RDS PostgreSQL (State & Metadata) ---
module "rds" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.0"
  identifier = "${var.project_name}-${var.environment}-postgres"
  engine               = "postgres"
  engine_version       = "16.3"
  instance_class       = "db.r6g.xlarge"
  allocated_storage    = 100
  max_allocated_storage = 500
  storage_encrypted    = true
  db_name              = "autogen"
  username             = "autogen_admin"
  password             = random_password.db.result
  vpc_id               = module.vpc.vpc_id
  subnet_ids           = module.vpc.private_subnets
  security_groups      = [module.rds_sg.id]
  backup_retention_period = 7
  deletion_protection  = var.environment == "prod"
}

# --- ElastiCache Redis (Caching, Queue, Rate Limit) ---
module "redis" {
  source  = "terraform-aws-modules/elasticache/aws"
  version = "~> 2.0"
  cluster_id           = "${var.project_name}-${var.environment}-redis"
  engine               = "redis"
  node_type            = "cache.r6g.xlarge"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  subnet_group_name    = module.elasticache_subnet_group.name
  security_group_ids   = [module.redis_sg.id]
  at_rest_encryption   = true
  transit_encryption   = true
  auth_token           = random_password.redis.result
}

# --- S3 for Artifacts & Knowledge Base ---
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-${var.environment}-artifacts"
  server_side_encryption_configuration {
    rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
  }
  versioning { enabled = true }
  lifecycle_rule {
    enabled = true
    expiration { days = 90 }
    noncurrent_version_expiration { days = 30 }
  }
}

# --- IAM Roles for Service Accounts (IRSA) ---
module "irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"
  role_map = {
    autogen-kernel = {
      role_name = "autogen-kernel-${var.environment}"
      attach_policies = [
        "arn:aws:iam::aws:policy/SecretsManagerReadWrite",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess", # For artifacts
      ]
      oidc_providers = {
        main = { provider_arn = module.eks.oidc_provider_arn, namespace_service_accounts = ["default:autogen-kernel"] }
      }
    }
  }
}

# --- Secrets ---
resource "random_password" "db" { length = 32, special = false }
resource "random_password" "redis" { length = 32, special = false }

resource "aws_secretsmanager_secret" "db" { name = "/autogen/${var.environment}/database-url" }
resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    host = module.rds.db_instance_endpoint
    port = 5432
    username = "autogen_admin"
    password = random_password.db.result
    dbname = "autogen"
  })
}

# --- Outputs ---
output "kubeconfig" {
  value = module.eks.kubeconfig
  sensitive = true
}
output "rds_endpoint" { value = module.rds.db_instance_endpoint }
output "redis_endpoint" { value = module.redis.elasticache_replication_group_primary_endpoint_address }
```

---

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №6)

> **CONTEXT FOR NEXT STEP (Gateway Spec - Optional):**
> Ты реализуешь `specs/06_ops/`.
> 1. Создай `.github/workflows/` с `kernel-ci.yml` и шаблон `generated-project-ci.yml.j2` (для рендеринга скиллом `add_github_actions_ci`).
> 2. Реализуй `kernel/observability/` с `otel.py` (setup_otel, metrics, trace_node декоратор).
> 3. Интегрируй `BudgetManager` в `LLMClient` (вызов `record_actual_usage` после каждого `achat`).
> 4. Реализуй `HITL_API` (FastAPI) с WebSocket менеджером и эндпоинтами `/interrupt`, `/approve`, `/ws`.
> 5. Напиши `DOCKERFILE.kernel` (multi-stage, non-root, uv).
> 6. Напиши `HELM_CHART/values.yaml` и `TERRAFORM/main.tf` для AWS EKS + RDS + Redis.
> 7. **Интеграционный тест (`tests/ops/test_observability.py`):**
>    *   Запуск Kernel с включенным OTEL -> Проверка появления метрик в Prometheus (`autogen_run_duration`).
>    *   Тест `BudgetManager`: Запуск задачи с лимитом $0.01 -> Ожидание `BudgetExceededError`.
>    *   Тест HITL: Запуск графа с прерыванием `plan_review` -> GET `/interrupt` возвращает payload -> POST `/approve` с `action: approve` -> Граф продолжается.
>    *   Деплой Helm чарта в Kind (local k8s) -> Проверка готовности подов, ingress, PVC для Kuzu.

---

### ✅ Чек-лист готовности Ops Spec (Definition of Done для Ответа №6)

- [ ] `kernel-ci.yml` проходит: Lint, Typecheck, Unit Tests, Contract Tests, Docker Build, Push.
- [ ] `GENERATED_PROJECT_CI.yml.j2` рендерится скиллом `add_github_actions_ci` и проходит в сгенерированном проекте.
- [ ] `OTEL_CONFIG` инициализируется без ошибок, метрики отдаются в Prometheus/Otel Collector.
- [ ] `GRAFANA_DASHBOARDS.json` импортируется и отображает данные.
- [ ] `BudgetManager` блокирует запуски при превышении `$/run` и `tokens/min`.
- [ ] `MODEL_ROUTER.yaml` загружается в LiteLLM Proxy, фоллбэки работают.
- [ ] `HITL_API` поднимается, WebSocket стримит логи, `/approve` резюмирует граф.
- [ ] `DOCKERFILE.kernel` собирается, образ < 1GB, проходит `trivy` scan (Critical=0).
- [ ] Helm Chart деплоится в Kind/K3s, поды становятся Ready, Ingress работает.
- [ ] Terraform `plan` проходит без ошибок для AWS.

---

---

# Ответ №7: Gateway Spec — Unified API, Router, Cross-Vertical Orchestration

Это **лицо платформы**. Единая точка входа для пользователей, CLI, CI/CD и других систем. Управляет маршрутизацией, аутентификацией, квотами и оркестрацией сложных мульти-вертикальных проектов.

Сохрани в структуру:
```text
specs/07_gateway/
├── GATEWAY_ARCH.md
├── api/
│   ├── OPENAPI_SPEC.yaml
│   ├── ROUTES.py
│   ├── MIDDLEWARE.py
│   └── WEBSOCKETS.py
├── router/
│   ├── CLASSIFIER.py
│   ├── ROUTER.py
│   └── CAPABILITY_MATCHER.py
├── orchestrator/
│   ├── CROSS_VERTICAL.py
│   ├── CONTRACT_RESOLVER.py
│   └── DEPENDENCY_GRAPH.py
├── auth/
│   ├── AUTH_MANAGER.py
│   ├── QUOTAS.py
│   └── API_KEYS.py
└── cli/
    ├── AUTOGEN_CLI.py
    └── CONFIG.yaml
```

---

## 1. `specs/07_gateway/GATEWAY_ARCH.md`

````markdown
# Gateway Specification: Unified API & Cross-Vertical Orchestration

## 1. Architecture Overview

```mermaid
graph TB
    Client[Clients: CLI, UI, CI/CD, Webhooks] --> GW[API Gateway :8000]
    
    subgraph Gateway
        GW --> Auth[Auth Middleware\nJWT / API Keys]
        Auth --> RateLimit[Rate Limiter\nRedis Token Bucket]
        RateLimit --> Router{Smart Router}
    end
    
    subgraph Routing Logic
        Router --> Classifier[Intent Classifier\nLLM / Rules]
        Router --> CapMatcher[Capability Matcher\nVertical Manifests]
        Router --> CrossOrch[Cross-Vertical Orchestrator]
    end
    
    subgraph Execution
        Classifier -->|Single Vertical| Kernel[Kernel Runtime]
        CapMatcher -->|Best Fit| Kernel
        CrossOrch -->|Multi-Project| Composer[Composition Engine]
        Composer --> Kernel1[Kernel: saas_web]
        Composer --> Kernel2[Kernel: py_microsvc]
        Composer --> Kernel3[Kernel: iac_terraform]
    end
    
    Kernel --> StateDB[(Postgres Checkpointer)]
    Kernel --> Sandbox[Sandbox Pool]
    Kernel --> LLM[LLM Gateway]
```

## 2. API Design Principles

| Principle | Implementation |
| :--- | :--- |
| **Async First** | All endpoints `async`, long-running ops return `202 Accepted` + `run_id` + WebSocket URL. |
| **Idempotency** | `Idempotency-Key` header for `POST /generate` (safe retries). |
| **Streaming** | `text/event-stream` for logs, `websocket` for real-time state. |
| **Versioning** | URL versioning `/api/v1/`, Protobuf/gRPC for internal high-perf. |
| **Observability** | `X-Request-ID` propagated everywhere. Structured JSON logs. |

## 3. Cross-Vertical Orchestration (The "Composer")

**Use Case:** "Build me a SaaS: Next.js Frontend + FastAPI Billing Service + Terraform AWS Infra".

**Flow:**
1.  **Decompose:** Planner (Meta-Agent) splits PRD into Sub-Projects per Vertical.
2.  **Contract Definition:** Generates **Shared Contracts** (OpenAPI, Protobuf, SQL Schemas, Event Schemas).
3.  **Parallel Generation:** Spawns independent Kernel Runs per Vertical with injected Contracts.
4.  **Synchronization:** Waits for `Contract Stabilization` (API signatures frozen).
5.  **Integration Tests:** Spins up `docker-compose` with all services, runs contract tests.
6.  **Aggregation:** Merges artifacts, generates root `README`, `ARCHITECTURE`, `docker-compose.yml`, `skaffold.yaml`.

## 4. Security Model

- **Authentication:** JWT (Auth0/Clerk/Custom) + API Keys (for CI/CD).
- **Authorization:** RBAC per Vertical (`admin`, `developer`, `viewer`).
- **Tenancy:** `X-Tenant-ID` header isolates workspaces, sandboxes, knowledge bases.
- **Secrets:** Injected at runtime via Vault/Secrets Manager, never in State.
````

---

## 2. `specs/07_gateway/api/OPENAPI_SPEC.yaml`

```yaml
# specs/07_gateway/api/OPENAPI_SPEC.yaml
openapi: 3.0.3
info:
  title: AutoGen Platform API
  version: 1.0.0
  description: Unified API for Automated Project Generation
  contact: { name: Platform Team, email: platform@autogen.dev }
servers:
  - url: https://api.autogen.dev/v1
    description: Production
  - url: http://localhost:8000/v1
    description: Local Development

components:
  securitySchemes:
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
    ApiKeyAuth:
      type: apiKey
      in: header
      name: X-API-Key
  schemas:
    GenerateRequest:
      type: object
      required: [prompt]
      properties:
        prompt: { type: string, maxLength: 50000, description: "Natural language PRD" }
        vertical_id: { type: string, description: "Explicit vertical (saas_web, py_fastapi, etc). Auto-detected if omitted." }
        tech_stack_hints: { type: object, additionalProperties: { type: string } }
        constraints: { type: array, items: { type: string } }
        context_files: 
          type: array
          items: { type: object, properties: { name: {type: string}, content_base64: {type: string} } }
        max_budget_usd: { type: number, minimum: 0.01, maximum: 100.0 }
        webhook_url: { type: string, format: uri }
    GenerateResponse:
      type: object
      properties:
        run_id: { type: string, format: uuid }
        status: { type: string, enum: [queued, running, completed, failed, needs_review] }
        message: { type: string }
        stream_url: { type: string, format: uri, description: "WebSocket for real-time logs" }
        artifact_url: { type: string, format: uri, nullable: true }
    RunStatus:
      type: object
      properties:
        run_id: { type: string }
        status: { type: string, enum: [queued, initializing, planning, coding, verifying, fixing, documenting, packaging, completed, failed, needs_review] }
        progress: { type: number, format: float, minimum: 0, maximum: 1 }
        current_node: { type: string, nullable: true }
        current_task: { type: string, nullable: true }
        logs: { type: array, items: { type: string } }
        error: { type: string, nullable: true }
        artifact: { $ref: '#/components/schemas/ArtifactMetadata' }
    ArtifactMetadata:
      type: object
      properties:
        project_name: { type: string }
        vertical_id: { type: string }
        git_commit_sha: { type: string }
        artifact_url: { type: string }
        quality_report: { type: object }
        total_cost_usd: { type: number }
        duration_seconds: { type: number }
    InterruptPayload:
      type: object
      properties:
        run_id: { type: string }
        interrupt_type: { type: string, enum: [plan_review, gate_failure, destructive_action, budget_exceeded] }
        payload: { type: object }
        actions: { type: array, items: { type: string, enum: [approve, reject, edit, abort, skip_gate] } }
    ApprovalRequest:
      type: object
      required: [action]
      properties:
        action: { type: string, enum: [approve, reject, edit, abort, skip_gate] }
        edited_data: { type: object }
        comment: { type: string }
    VerticalInfo:
      type: object
      properties:
        id: { type: string }
        name: { type: string }
        description: { type: string }
        version: { type: string }
        tech_stack: { type: object }
        key_skills: { type: array, items: { type: string } }
    ErrorResponse:
      type: object
      properties: { detail: { type: string }, code: { type: string } }

security: [ { BearerAuth: [] }, { ApiKeyAuth: [] } ]

paths:
  /generate:
    post:
      summary: Start a new project generation
      operationId: createGeneration
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/GenerateRequest' }
      responses:
        '202':
          description: Generation started
          content:
            application/json:
              schema: { $ref: '#/components/schemas/GenerateResponse' }
          headers:
            Location:
              schema: { type: string, format: uri }
              description: URL to poll for status (/runs/{run_id})
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Error' }
        '429': { $ref: '#/components/responses/Error' }

  /runs/{run_id}:
    get:
      summary: Get run status and artifact
      operationId: getRunStatus
      parameters:
        - { name: run_id, in: path, required: true, schema: { type: string, format: uuid } }
      responses:
        '200': { content: { application/json: { schema: { $ref: '#/components/schemas/RunStatus' } } } }
        '404': { $ref: '#/components/responses/Error' }

  /runs/{run_id}/interrupt:
    get:
      summary: Get current interrupt payload (HITL)
      operationId: getInterrupt
      parameters: [ { name: run_id, in: path, required: true, schema: { type: string } } ]
      responses:
        '200': { content: { application/json: { schema: { $ref: '#/components/schemas/InterruptPayload' } } } }
        '404': { description: "No active interrupt or run not found" }

    post:
      summary: Submit human approval/decision
      operationId: submitApproval
      parameters: [ { name: run_id, in: path, required: true, schema: { type: string } } ]
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/ApprovalRequest' }
      responses:
        '200': { description: "Graph resumed" }
        '400': { $ref: '#/components/responses/Error' }
        '409': { description: "No active interrupt" }

  /runs/{run_id}/logs:
    get:
      summary: Stream logs via Server-Sent Events (SSE)
      operationId: streamLogs
      parameters: [ { name: run_id, in: path, required: true, schema: { type: string } } ]
      responses:
        '200':
          description: Event stream
          content:
            text/event-stream:
              schema: { type: string }

  /ws/runs/{run_id}:
    get:
      summary: WebSocket for real-time bidirectional communication
      operationId: websocketRun
      parameters: [ { name: run_id, in: path, required: true, schema: { type: string } } ]
      responses:
        '101': { description: "Switching Protocols" }

  /verticals:
    get:
      summary: List available verticals
      operationId: listVerticals
      responses:
        '200':
          content:
            application/json:
              schema: { type: array, items: { $ref: '#/components/schemas/VerticalInfo' } }

  /verticals/{vertical_id}:
    get:
      summary: Get vertical manifest and capabilities
      operationId: getVertical
      parameters: [ { name: vertical_id, in: path, required: true, schema: { type: string } } ]
      responses:
        '200': { content: { application/json: { schema: { $ref: '#/components/schemas/VerticalInfo' } } } }

  /health:
    get:
      summary: Health check
      operationId: healthCheck
      responses:
        '200': { content: { application/json: { schema: { type: object, properties: { status: {type: string}, version: {type: string} } } } } }

responses:
  Error:
    description: Error response
    content:
      application/json:
        schema: { $ref: '#/components/schemas/ErrorResponse' }
```

---

## 3. `specs/07_gateway/router/CLASSIFIER.py`

```python
# specs/07_gateway/router/CLASSIFIER.py
"""
Intent Classifier & Vertical Router.
Routes incoming prompt to the best Vertical.
Supports: Rule-based (fast), LLM-based (smart), Capability Matching (precise).
"""

from __future__ import annotations
import re
from typing: Dict, List, Optional, Literal
from pydantic import BaseModel, Field
from kernel.llm.client import LiteLLMClient
from kernel.vertical.loader import VerticalLoader
from kernel.protocols import VerticalManifest

class RoutingDecision(BaseModel):
    vertical_id: str
    confidence: float
    method: Literal["rule", "llm", "capability", "explicit"]
    reasoning: str
    suggested_tech_stack: Dict[str, str] = Field(default_factory=dict)

class VerticalRouter:
    def __init__(self, vertical_loader: VerticalLoader, llm_client: LiteLLMClient):
        self.loader = vertical_loader
        self.llm = llm_client
        self._verticals: Dict[str, VerticalManifest] = {}
        self._rule_patterns = self._compile_rules()

    def _compile_rules(self) -> Dict[str, List[re.Pattern]]:
        """Keyword/Regex rules for instant routing (0 LLM cost)."""
        return {
            "saas_web": [
                re.compile(r"\b(next\.?js|react|vercel|saas|dashboard|admin panel|landing|marketing|stripe|nextauth|shadcn|tailwind)\b", re.I),
            ],
            "py_fastapi_microsvc": [
                re.compile(r"\b(fastapi|pydantic|sqlalchemy|alembic|postgres|async|microservice|api only|backend|python)\b", re.I),
            ],
            "go_microsvc": [
                re.compile(r"\b(go|golang|grpc|protobuf|high load|cli tool|cobra|viper)\b", re.I),
            ],
            "iac_terraform": [
                re.compile(r"\b(terraform|infrastructure|aws|azure|gcp|kubernetes|helm|vpc|rds|iam|cloud)\b", re.I),
            ],
            "data_pipeline": [
                re.compile(r"\b(airflow|dbt|spark|pandas|etl|data pipeline|mlops|training|model)\b", re.I),
            ],
        }

    async def load_verticals(self):
        """Cache manifests for capability matching."""
        self._verticals = await self.loader.discover()

    async def route(self, prompt: str, explicit_vertical: Optional[str] = None, tech_hints: Dict = None) -> RoutingDecision:
        tech_hints = tech_hints or {}

        # 1. Explicit Override
        if explicit_vertical and explicit_vertical in self._verticals:
            return RoutingDecision(vertical_id=explicit_vertical, confidence=1.0, method="explicit", reasoning="User explicitly selected vertical.")

        # 2. Rule-Based (Fast, Cheap)
        rule_match = self._match_rules(prompt)
        if rule_match:
            return RoutingDecision(vertical_id=rule_match, confidence=0.85, method="rule", reasoning=f"Matched keywords for {rule_match}.")

        # 3. Capability Matching (Structured Hints)
        cap_match = self._match_capabilities(tech_hints)
        if cap_match:
            return RoutingDecision(vertical_id=cap_match, confidence=0.9, method="capability", reasoning=f"Tech stack hints match {cap_match} capabilities.")

        # 4. LLM Classifier (Smart, Costly)
        return await self._llm_classify(prompt, tech_hints)

    def _match_rules(self, prompt: str) -> Optional[str]:
        scores = {}
        for vid, patterns in self._rule_patterns.items():
            for pat in patterns:
                if pat.search(prompt):
                    scores[vid] = scores.get(vid, 0) + 1
        if scores:
            return max(scores, key=scores.get)
        return None

    def _match_capabilities(self, hints: Dict) -> Optional[str]:
        """Match tech_stack_hints to vertical provides."""
        scores = {}
        for vid, manifest in self._verticals.items():
            score = 0
            stack = manifest.tech_stack
            for k, v in hints.items():
                if stack.get(k) == v: score += 2
                elif v in str(stack.values()): score += 1
            if score > 0: scores[vid] = score
        if scores: return max(scores, key=scores.get)
        return None

    async def _llm_classify(self, prompt: str, hints: Dict) -> RoutingDecision:
        descriptions = "\n".join([f"- **{v.id}**: {v.description} (Stack: {v.tech_stack})" for v in self._verticals.values()])
        
        system = f"""You are a Platform Router. Classify the user request into the BEST matching vertical.
Available Verticals:
{descriptions}

Rules:
1. If request mentions specific tech (Next.js, FastAPI, Terraform) -> Match that vertical.
2. If generic "web app" -> saas_web.
3. If generic "api/backend" -> py_fastapi_microsvc.
4. If "infrastructure/cloud" -> iac_terraform.
5. Return ONLY the vertical_id."""
        
        user = f"Request: {prompt}\nHints: {hints}"
        
        try:
            resp = await self.llm.achat(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                model="router/classifier",
                temperature=0.0,
                max_tokens=20
            )
            vid = resp.content.strip().lower()
            if vid in self._verticals:
                return RoutingDecision(vertical_id=vid, confidence=0.95, method="llm", reasoning="LLM classification.")
        except Exception:
            pass
        
        # Fallback
        return RoutingDecision(vertical_id="saas_web", confidence=0.5, method="fallback", reasoning="Default fallback.")
```

---

## 4. `specs/07_gateway/orchestrator/CROSS_VERTICAL.py`

````python
# specs/07_gateway/orchestrator/CROSS_VERTICAL.py
"""
Cross-Vertical Orchestrator (The Composer).
Generates multi-project systems (Frontend + Backend + Infra) with shared contracts.
"""

from __future__ import annotations
import asyncio
import uuid
from dataclasses import dataclass, field
from typing: Dict, List, Any, Optional
from pathlib import Path

from kernel.graph.builder import build_graph
from kernel.vertical.loader import VerticalLoader
from kernel.protocols import GenerateRequest, VerticalManifest
from kernel.state import AgentState

@dataclass
class SubProjectSpec:
    vertical_id: str
    name: str
    prompt: str
    tech_stack_hints: Dict[str, str]
    contracts: Dict[str, Any] = field(default_factory=dict) # Injected shared contracts
    depends_on: List[str] = field(default_factory=list) # Other sub-project names

@dataclass
class CompositionPlan:
    project_name: str
    sub_projects: List[SubProjectSpec]
    shared_contracts: Dict[str, Any] # OpenAPI, Protobuf, SQL, Events
    integration_tests: List[str] = field(default_factory=list)

class CrossVerticalOrchestrator:
    def __init__(self, vertical_loader: VerticalLoader):
        self.loader = vertical_loader
        self.verticals: Dict[str, VerticalManifest] = {}

    async def initialize(self):
        self.verticals = await self.loader.discover()

    async def compose(self, request: GenerateRequest) -> CompositionPlan:
        """Meta-Planning: Decompose monolithic prompt into vertical-specific sub-projects."""
        # This uses a specialized "Architect" LLM (router/planner model)
        # For spec, we define the output structure.
        
        # Example Output for "SaaS with Billing Microservice":
        return CompositionPlan(
            project_name=request.tech_stack_hints.get("project_name", "composed-system"),
            shared_contracts={
                "api": { "openapi": "shared/openapi.yaml" }, # Generated by Composer
                "database": { "sql": "shared/schema.sql" },
                "events": { "schemas": "shared/events/" }
            },
            sub_projects=[
                SubProjectSpec(
                    vertical_id="saas_web",
                    name="frontend",
                    prompt="Generate the Next.js Frontend consuming the Billing API defined in shared contracts.",
                    tech_stack_hints={"api_contract": "shared/openapi.yaml"},
                    depends_on=["billing-api"]
                ),
                SubProjectSpec(
                    vertical_id="py_fastapi_microsvc",
                    name="billing-api",
                    prompt="Generate FastAPI Billing Service with Stripe, implementing the shared OpenAPI contract.",
                    tech_stack_hints={"openapi_spec": "shared/openapi.yaml"},
                    depends_on=[]
                ),
                SubProjectSpec(
                    vertical_id="iac_terraform",
                    name="infra",
                    prompt="Generate Terraform for AWS: ECS Fargate for API, Vercel/CloudFront for Frontend, RDS, ElastiCache.",
                    tech_stack_hints={},
                    depends_on=["billing-api", "frontend"]
                )
            ],
            integration_tests=["contract_test_openapi", "docker_compose_up_smoke"]
        )

    async def execute_plan(self, plan: CompositionPlan, base_request: GenerateRequest) -> Dict[str, Any]:
        """Execute sub-projects in topological order with contract injection."""
        # 1. Resolve Dependency Order
        from kernel.skills.registry import SkillRegistry # Reuse topo sort logic
        # Simplified: Assume plan.sub_projects is already ordered.
        
        results = {}
        shared_contracts = plan.shared_contracts.copy()
        
        for sub_spec in plan.sub_projects:
            # 1. Inject Contracts into Prompt/Context
            enriched_prompt = self._inject_contracts(sub_spec.prompt, shared_contracts)
            
            # 2. Create Sub-Request
            sub_request = GenerateRequest(
                prompt=enriched_prompt,
                vertical_id=sub_spec.vertical_id,
                tech_stack_hints={**base_request.tech_stack_hints, **sub_spec.tech_stack_hints},
                constraints=base_request.constraints + [f"Part of composition: {plan.project_name}"],
                context_files=base_request.context_files
            )
            
            # 3. Run Kernel (Async, potentially parallel if no deps)
            vertical = self.loader.load_vertical(sub_spec.vertical_id)
            graph = build_graph(vertical)
            
            run_id = f"{base_request.run_id}-{sub_spec.name}"
            config = {"configurable": {"thread_id": run_id, "vertical": vertical}}
            
            final_state = await graph.ainvoke(sub_request.model_dump(), config=config)
            
            if final_state.get("status") != "completed":
                raise RuntimeError(f"Sub-project {sub_spec.name} failed: {final_state.get('error')}")
            
            # 4. Extract Contracts from Result (e.g., generated OpenAPI from FastAPI)
            new_contracts = self._extract_contracts(final_state, sub_spec.vertical_id)
            shared_contracts.update(new_contracts)
            
            results[sub_spec.name] = {
                "run_id": run_id,
                "artifact_url": final_state["final_artifact"]["artifact_url"],
                "contracts": new_contracts
            }
            
            # Stream progress to parent run
            # await stream_log(base_request.run_id, "info", f"Completed sub-project: {sub_spec.name}")
        
        # 5. Generate Integration Glue (docker-compose, skaffold, root README)
        glue_artifact = await self._generate_integration_glue(plan, results)
        
        return {
            "sub_projects": results,
            "integration_glue": glue_artifact,
            "shared_contracts": shared_contracts
        }

    def _inject_contracts(self, prompt: str, contracts: Dict) -> str:
        if not contracts: return prompt
        contract_md = "## Shared Contracts (MANDATORY)\n"
        for name, content in contracts.items():
            contract_md += f"### {name}\n```yaml\n{content}\n```\n"
        return f"{prompt}\n\n---\n{contract_md}"

    def _extract_contracts(self, state: AgentState, vertical_id: str) -> Dict:
        """Extract generated contracts (OpenAPI, SQL, Protobuf) from artifact."""
        # Implementation: Read specific files from artifact (via sandbox download or state metadata)
        # e.g., state['metadata'].get('generated_openapi')
        return state.get("metadata", {}).get("generated_contracts", {})

    async def _generate_integration_glue(self, plan: CompositionPlan, results: Dict) -> Dict:
        """Generate docker-compose.yml, skaffold.yaml, root README, Makefile."""
        # This is a Skill itself: `generate_integration_glue`
        # For now, return placeholder structure.
        return {
            "docker_compose": "version: '3.8'\nservices:\n  # ... generated from results",
            "skaffold_yaml": "apiVersion: skaffold/v4\n# ...",
            "readme_md": f"# {plan.project_name}\n\nGenerated composition.\n",
            "makefile": "up:\n\tdocker-compose up -d\n"
        }
````

---

## 5. `specs/07_gateway/auth/AUTH_MANAGER.py`

```python
# specs/07_gateway/auth/AUTH_MANAGER.py
"""
Authentication, Authorization & Quotas.
JWT Validation, API Key Management, Rate Limiting (Redis).
"""

from __future__ import annotations
import jwt
import time
import hashlib
from typing: Dict, Optional, List
from dataclasses import dataclass
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
import redis.asyncio as redis
from kernel.config import settings

# --- Security Schemes ---
bearer_scheme = HTTPBearer(auto_error=False)
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

@dataclass
class Principal:
    user_id: str
    tenant_id: str
    roles: List[str] # ["admin", "developer", "viewer"]
    vertical_access: List[str] # ["saas_web", "*"]
    rate_limit_tier: str # "free", "pro", "enterprise"

class AuthManager:
    def __init__(self):
        self.redis = redis.from_url(settings.redis.url, encoding="utf-8", decode_responses=True)
        self.jwt_secret = settings.auth.jwt_secret
        self.jwt_algo = "HS256"
        self.jwt_issuer = "autogen-platform"

    async def authenticate(self, 
                           token: HTTPAuthorizationCredentials = Security(bearer_scheme),
                           api_key: str = Security(api_key_scheme)) -> Principal:
        """Validate JWT or API Key."""
        if token:
            return await self._validate_jwt(token.credentials)
        if api_key:
            return await self._validate_api_key(api_key)
        raise HTTPException(401, "Missing authentication")

    async def _validate_jwt(self, token: str) -> Principal:
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algo], issuer=self.jwt_issuer)
            return Principal(
                user_id=payload["sub"],
                tenant_id=payload.get("tenant_id", "default"),
                roles=payload.get("roles", ["developer"]),
                vertical_access=payload.get("vertical_access", ["*"]),
                rate_limit_tier=payload.get("tier", "pro")
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(401, "Token expired")
        except jwt.InvalidTokenError as e:
            raise HTTPException(401, f"Invalid token: {e}")

    async def _validate_api_key(self, key: str) -> Principal:
        # Format: agk_<tenant>_<random>
        # Store hash in Redis: `apikey:{hash}` -> JSON Principal
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:32]
        data = await self.redis.get(f"apikey:{key_hash}")
        if not data:
            raise HTTPException(401, "Invalid API Key")
        import json
        return Principal(**json.loads(data))

    async def check_vertical_access(self, principal: Principal, vertical_id: str):
        if "*" not in principal.vertical_access and vertical_id not in principal.vertical_access:
            raise HTTPException(403, f"Access denied to vertical: {vertical_id}")

# --- Rate Limiter (Token Bucket per Tenant/User) ---
class RateLimiter:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.tiers = {
            "free": {"rpm": 10, "rpd": 100, "concurrent": 1},
            "pro": {"rpm": 60, "rpd": 1000, "concurrent": 5},
            "enterprise": {"rpm": 300, "rpd": 10000, "concurrent": 20},
        }

    async def check_limit(self, principal: Principal, cost: int = 1) -> bool:
        tier = self.tiers.get(principal.rate_limit_tier, self.tiers["free"])
        key_base = f"ratelimit:{principal.tenant_id}:{principal.user_id}"
        
        pipe = self.redis.pipeline()
        now = int(time.time())
        minute_key = f"{key_base}:min:{now // 60}"
        day_key = f"{key_base}:day:{now // 86400}"
        
        pipe.incrby(minute_key, cost)
        pipe.expire(minute_key, 120)
        pipe.incrby(day_key, cost)
        pipe.expire(day_key, 86400 * 2)
        results = await pipe.execute()
        
        if results[0] > tier["rpm"]:
            raise HTTPException(429, f"Rate limit exceeded: {tier['rpm']} req/min")
        if results[2] > tier["rpd"]:
            raise HTTPException(429, f"Daily quota exceeded: {tier['rpd']} req/day")
        return True

    async def check_concurrent(self, principal: Principal) -> bool:
        tier = self.tiers.get(principal.rate_limit_tier, self.tiers["free"])
        key = f"concurrent:{principal.tenant_id}"
        current = await self.redis.incr(key)
        if current == 1:
            await self.redis.expire(key, 3600)
        if current > tier["concurrent"]:
            await self.redis.decr(key)
            raise HTTPException(429, f"Concurrent run limit: {tier['concurrent']}")
        return True

    async def release_concurrent(self, principal: Principal):
        await self.redis.decr(f"concurrent:{principal.tenant_id}")

# --- FastAPI Dependencies ---
auth_manager = AuthManager()
rate_limiter = RateLimiter(redis.from_url(settings.redis.url))

async def get_principal(principal: Principal = Depends(auth_manager.authenticate)) -> Principal:
    return principal

async def check_rate_limit(principal: Principal = Depends(get_principal)):
    await rate_limiter.check_limit(principal)
    await rate_limiter.check_concurrent(principal)
    return principal

async def release_concurrent(principal: Principal = Depends(get_principal)):
    await rate_limiter.release_concurrent(principal)
```

---

## 6. `specs/07_gateway/cli/AUTOGEN_CLI.py`

```python
# specs/07_gateway/cli/AUTOGEN_CLI.py
"""
CLI Client for AutoGen Platform.
Usage:
  autogen generate "Build a SaaS with Stripe" --vertical saas_web --watch
  autogen status <run_id>
  autogen approve <run_id> --action approve
  autogen verticals list
"""

import asyncio
import click
import httpx
import websockets
import json
import os
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.markdown import Markdown
from pathlib import Path

console = Console()


class AutoGenCLI:
    def __init__(self, base_url: str = None, api_key: str = None):
        self.base_url = base_url or os.getenv("AUTOGEN_API_URL", "http://localhost:8000/v1")
        self.api_key = api_key or os.getenv("AUTOGEN_API_KEY")
        self.client = httpx.AsyncClient(base_url=self.base_url, headers={"X-API-Key": self.api_key}, timeout=300)

    async def generate(self, prompt: str, vertical: str = None, watch: bool = False, **kwargs) -> str:
        """Start generation, optionally watch progress."""
        payload = {"prompt": prompt, **kwargs}
        if vertical:
            payload["vertical_id"] = vertical

        resp = await self.client.post("/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        run_id = data["run_id"]

        console.print(f"[green]Generation started:[/green] {run_id}")
        console.print(f"[dim]Stream URL:[/dim] {data['stream_url']}")

        if watch:
            await self.watch(run_id)
        return run_id

    async def watch(self, run_id: str):
        """Watch progress via WebSocket."""
        ws_url = f"ws://localhost:8000/v1/ws/runs/{run_id}".replace("http://", "ws://").replace("https://", "wss://")
        try:
            async with websockets.connect(ws_url, extra_headers={"X-API-Key": self.api_key}) as ws:
                with Live(console=console, refresh_per_second=4) as live:
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if data["type"] == "log":
                            log = data["data"]
                            live.update(self._render_log(log))
                        elif data["type"] == "state":
                            # Update status table
                            pass
                        elif data["type"] == "interrupt":
                            console.print(
                                f"\n[yellow]⚠️ HUMAN INPUT REQUIRED:[/yellow] {data['data']['interrupt_type']}"
                            )
                            action = click.prompt(
                                "Action", type=click.Choice(["approve", "reject", "edit", "abort", "skip_gate"])
                            )
                            await self.approve(run_id, action)
                            # Resume watching...
        except KeyboardInterrupt:
            console.print("\n[red]Stopped watching.[/red]")

    def _render_log(self, log: dict):
        from rich.text import Text

        t = Text()
        t.append(f"[{log.get('timestamp', '')}] ", style="dim")
        t.append(f"{log.get('node', '')}: ", style="cyan")
        t.append(log.get("message", ""), style="white" if log.get("level") == "info" else "yellow")
        return t

    async def status(self, run_id: str):
        resp = await self.client.get(f"/runs/{run_id}")
        resp.raise_for_status()
        data = resp.json()
        console.print(
            Markdown(f"## Run: {run_id}\n**Status:** {data['status']}\n**Progress:** {data['progress'] * 100:.0f}%")
        )
        if data.get("artifact"):
            console.print(f"[green]Artifact:[/green] {data['artifact']['artifact_url']}")

    async def approve(self, run_id: str, action: str, data: dict = None):
        resp = await self.client.post(f"/runs/{run_id}/interrupt", json={"action": action, "edited_data": data})
        resp.raise_for_status()
        console.print(f"[green]Submitted:[/green] {action}")

    async def list_verticals(self):
        resp = await self.client.get("/verticals")
        resp.raise_for_status()
        verticals = resp.json()
        table = Table(title="Available Verticals")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("Stack")
        for v in verticals:
            table.add_row(v["id"], v["name"], v["description"][:50], str(v.get("tech_stack", {})))
        console.print(table)


@click.group()
@click.option("--url", envvar="AUTOGEN_API_URL", default="http://localhost:8000/v1")
@click.option("--key", envvar="AUTOGEN_API_KEY")
@click.pass_context
def cli(ctx, url, key):
    ctx.obj = AutoGenCLI(url, key)


@cli.command()
@click.argument("prompt")
@click.option("--vertical", "-v", help="Vertical ID (auto-detect if omitted)")
@click.option("--watch", "-w", is_flag=True, help="Watch progress in real-time")
@click.option("--budget", type=float, help="Max budget USD")
@click.pass_context
def generate(ctx, prompt, vertical, watch, budget):
    """Generate a new project."""
    asyncio.run(ctx.obj.generate(prompt, vertical, watch, max_budget_usd=budget))


@cli.command()
@click.argument("run_id")
@click.pass_context
def status(ctx, run_id):
    """Check run status."""
    asyncio.run(ctx.obj.status(run_id))


@cli.command()
@click.argument("run_id")
@click.option("--action", type=click.Choice(["approve", "reject", "edit", "abort", "skip_gate"]), required=True)
@click.pass_context
def approve(ctx, run_id, action):
    """Submit human approval for interrupted run."""
    asyncio.run(ctx.obj.approve(run_id, action))


@cli.command()
@click.pass_context
def verticals(ctx):
    """List available verticals."""
    asyncio.run(ctx.obj.list_verticals())


if __name__ == "__main__":
    cli()
```

---

## 7. `specs/07_gateway/MASTER_PROMPT.md`

> **ФИНАЛЬНЫЙ АРТЕФАКТ: Мастер-промпт для Агента-Билдера (Cursor / Cline / Devin / Custom).**
> Скопируй этот блок, сохрани как `MASTER_PROMPT.md` в корне репозитория и отдай агенту.

```markdown
# MASTER PROMPT: Build AutoGen Platform

## ROLE
You are a **Staff Platform Engineer**. Your mission: Build a **Production-Ready Agentic Platform** for automated software generation (AutoGen) strictly following the specifications in `./specs/`.

## SPECIFICATION SOURCE
All implementation details are in `./specs/`:
- `01_kernel/` — Core Graph, State, Protocols
- `02_infra/` — Sandbox, Tools (FS, Shell, LSP, RAG, Git)
- `03_skills/` — Skill Framework, Registry, Templates, Hooks
- `04_knowledge/` — Ingestion Pipeline, Vector/Graph DB, Retrieval
- `05_vertical_saas_web/` — First Vertical Implementation (Next.js/tRPC/Prisma)
- `06_ops/` — CI/CD, Observability (OTEL/Grafana), Cost Control, HITL API, Deployment (Docker/Helm/Terraform)
- `07_gateway/` — Unified API, Router, Cross-Vertical Orchestrator, CLI

## IMPLEMENTATION ORDER (STRICT)
**Do not proceed to Step N+1 until Step N passes all tests.**

### Phase 1: Kernel Foundation (Week 1)
1.  **`specs/01_kernel/`** → Implement `kernel/` package:
    - `state.py` (Pydantic/TypedDict), `protocols.py` (Interfaces)
    - `graph/builder.py` (LangGraph StateGraph compilation)
    - `graph/nodes/` (Stubs for planner, coder, verifier, fixer, documenter)
    - `graph/routing.py` (Exact logic from spec)
    - `tools/registry.py`, `filesystem.py`, `shell.py`, `lsp.py`, `rag.py`, `git.py`
    - `llm/client.py` (LiteLLM + Instructor + Cost Tracking)
    - `vertical/loader.py`, `vertical/protocols.py`
    - `config.py` (Pydantic Settings)
    - **Test:** `tests/kernel/test_graph_compilation.py` — Graph compiles, all nodes/edges present.

### Phase 2: Infrastructure & Tools (Week 1-2)
2.  **`specs/02_infra/`** → Implement Sandbox & Tools:
    - `Dockerfile.sandbox.base` + `Dockerfile.sandbox.saas_web` (Multi-lang LSPs, Tools)
    - `SandboxManager` (Pool, TTL, Affinity) + `E2BSandbox` adapter
    - `ToolRegistry` + All Tools implementing `ITool`
    - **LSP Tool:** Must connect to `typescript-language-server`, `pyright`, `gopls` in sandbox.
    - **Test:** `tests/infra/test_tools_integration.py` — Write/Read/Grep/LSP/Shell work in real sandbox.

### Phase 3: Skills Framework (Week 2)
3.  **`specs/03_skills/`** → Implement Skill System:
    - `SkillDef` loading from `skill.yaml` + `hooks.py`
    - `TemplateEngine` (Jinja2 + custom filters: `to_json`, `snake_case`, `skill_output`)
    - `SkillExecutor` (Lifecycle: Hooks -> Render -> Write -> PostScripts -> Validate)
    - `SkillRegistry` (Topological Sort for `depends_on`)
    - `VerticalLoader` + `GenericVertical` (Default `IVertical` impl)
    - **Test:** `tests/skills/test_skill_execution.py` — `init_nextjs` skill runs `pnpm install/lint/typecheck/build` successfully.

### Phase 4: Knowledge Base (Week 2-3)
4.  **`specs/04_knowledge/`** → Implement RAG + Graph:
    - `MultiLanguageParser` (Tree-sitter for TS, Python, Go)
    - `ChunkingStrategy` (Symbol, FileSummary, Config chunks)
    - `EnrichmentPipeline` (Batch LLM: Intent, Pattern, Tags, Issues)
    - `QdrantKB` (Hybrid Search, Payload Indexes, Batch Upsert)
    - `KuzuGraph` (Schema, Cypher Queries: Callers, Dependencies)
    - `RetrievalEngine` (Strategies: Coding, Planning, Fixing + Reranking)
    - `IngestionPipeline` (Git Clone -> Parse -> Chunk -> Enrich -> Embed -> Store)
    - **Test:** `tests/knowledge/test_ingestion_retrieval.py` — Ingest sample repo -> Query "streaming response" -> Returns `renderToReadableStream` chunk.

### Phase 5: SaaS Web Vertical (Week 3-4)
5.  **`specs/05_vertical_saas_web/`** → Implement First Vertical:
    - `MANIFEST.yaml`, `VERTICAL_IMPL.py` (`SaasWebVertical`)
    - **10 Skills:** `init_nextjs`, `init_prisma`, `add_trpc`, `add_nextauth`, `add_stripe`, `add_shadcn`, `add_dockerfile`, `add_github_actions_ci`, `add_playwright_e2e`, `add_admin_dashboard`.
    - `verification/GATES.yaml` + `PARSERS.py` (ESLint, TSC, Vitest, Next Build, Playwright, Prisma, Semgrep).
    - `prompts/` (Planner, Coder, Fixer, Reviewer, Documenter) with Few-Shot examples.
    - **Test:** `tests/verticals/test_saas_web.py` — `generate("Todo App with Auth")` passes all gates (Build, Lint, Typecheck, Test, Docker).

### Phase 6: Operations & Production (Week 4)
6.  **`specs/06_ops/`** → Harden for Production:
    - `.github/workflows/kernel-ci.yml` (Lint, Test, Contract, Docker, Release)
    - `GENERATED_PROJECT_CI.yml.j2` (Template for `add_github_actions_ci` skill)
    - `OTEL_CONFIG.py` (Traces, Metrics: `autogen_run_duration`, `llm_cost`, `gate_status`)
    - `BudgetManager` (Enforce `$/run`, `tokens/min`, Daily quota)
    - `MODEL_ROUTER.yaml` for LiteLLM Proxy (Planner=Sonnet, Coder=Haiku, Enricher=4o-mini)
    - `HITL_API` (FastAPI + WebSocket: `/interrupt`, `/approve`, `/ws/logs`)
    - `DOCKERFILE.kernel` (Multi-stage, non-root, uv)
    - `HELM_CHART` + `TERRAFORM` (AWS EKS, RDS, Redis, S3, IRSA)
    - **Test:** `tests/ops/test_observability.py` — Metrics appear in Prometheus, Budget blocks run, HITL resume works.

### Phase 7: Gateway & Composition (Week 4-5)
7.  **`specs/07_gateway/`** → Unify & Scale:
    - `OPENAPI_SPEC.yaml` → FastAPI App (`main.py`)
    - `Router/Classifier` (Rules -> Capabilities -> LLM)
    - `CrossVerticalOrchestrator` (Composer: Decompose -> Contracts -> Parallel Kernels -> Glue)
    - `AuthManager` (JWT + API Keys + Redis Rate Limiting)
    - `AUTOGEN_CLI` (Rich CLI: `generate`, `watch`, `approve`, `verticals`)
    - **Test:** `tests/gateway/test_composition.py` — `generate("SaaS with Billing API + Terraform")` -> 3 Kernels run -> Shared OpenAPI -> `docker-compose` works.

## TECH STACK LOCK (Non-Negotiable)
- **Language:** Python 3.11+ (Type Hints Mandatory, `mypy --strict`)
- **Async:** `asyncio`, `httpx`, `asyncpg`, `redis-py`
- **Graph:** `langgraph` (StateGraph, Checkpointer)
- **LLM:** `litellm` (Router), `instructor` (Structured Output)
- **Sandbox:** `e2b-code-interpreter` (Primary), `docker` (Fallback)
- **Vector DB:** `qdrant-client` (Async)
- **Graph DB:** `kuzu` (Embedded)
- **Parsing:** `tree-sitter`, `tree-sitter-languages`
- **Templates:** `jinja2` (StrictUndefined)
- **Validation:** `pydantic v2`, `pydantic-settings`
- **CI:** `ruff`, `mypy`, `pytest`, `semantic-release`
- **Infra:** `docker`, `helm`, `terraform`

## QUALITY GATES (Definition of Done for ENTIRE PROJECT)
1.  **`mypy --strict`** passes on `kernel/`, `specs/`.
2.  **`ruff check`** passes (0 errors, 0 warnings).
3.  **`pytest tests/`** passes (Unit > 80% cov, Integration > 50%).
4.  **`docker build -f specs/06_ops/deployment/DOCKERFILE.kernel .`** succeeds, image < 1.5GB.
5.  **Helm Chart** deploys in `kind` cluster, pods Ready, Ingress works.
5.  **E2E Scenario:** `autogen generate "Simple Blog with Auth" --watch` -> Completes in < 10 min -> Artifact builds/runs locally.
6.  **Cost Control:** `generate` with `max_budget_usd=0.50` stops gracefully with `BudgetExceededError`.

## DELIVERABLES
1.  Complete Source Code in `kernel/`, `verticals/`, `tests/`, `scripts/`.
2.  `specs/` directory (This documentation — **Source of Truth**).
3.  `README.md` with Architecture Diagram, Quickstart, Vertical Development Guide.
4.  `CHANGELOG.md` (Generated by semantic-release).
5.  GitHub Actions workflows passing on `main`.

## START NOW
**Begin with Phase 1, Step 1: Create `kernel/state.py`, `kernel/protocols.py`, `kernel/config.py`.**
Report progress after each file. Ask for clarification if any spec is ambiguous.
```

---

### 🎯 Финальный Чек-лист Весь Проект (Global Definition of Done)

| Слой | Статус | Ключевой Артефакт | Тест |
| :--- | :--- | :--- | :--- |
| **01 Kernel** | ✅ Spec Ready | `kernel/graph/builder.py`, `state.py` | Graph Compiles |
| **02 Infra** | ✅ Spec Ready | `SandboxManager`, `LSPTool`, `ToolRegistry` | Tools work in Sandbox |
| **03 Skills** | ✅ Spec Ready | `SkillExecutor`, `TemplateEngine`, `Registry` | `init_nextjs` passes gates |
| **04 Knowledge** | ✅ Spec Ready | `IngestionPipeline`, `QdrantKB`, `KuzuGraph`, `RetrievalEngine` | RAG Retrieval Precision > 0.8 |
| **05 Vertical** | ✅ Spec Ready | `SaasWebVertical`, 10 Skills, Gates, Prompts | `generate("Todo")` -> Green Build |
| **06 Ops** | ✅ Spec Ready | `CI/CD`, `OTEL`, `BudgetManager`, `HITL_API`, `Helm/Terraform` | Deploy to Kind, Metrics, Budget Block |
| **07 Gateway** | ✅ Spec Ready | `FastAPI`, `Router`, `CrossVerticalOrchestrator`, `CLI` | Multi-Vertical Composition Works |

---

**Проект спецификации завершен.** 

У тебя теперь есть **полный технический пакет (7 спеков + Мастер-промпт)** для построения **Enterprise-уровня Agentic Platform**. 

Следующий шаг — **написание кода**. Рекомендую начать с `kernel/state.py` и `kernel/protocols.py`, так как от них зависят все остальные слои. Удачи в строительстве! 🚀
