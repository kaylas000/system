"""
Protocol Definitions (Interfaces) for Dependency Inversion.

Implements ``specs/01_kernel/protocols.py``. The kernel depends ONLY on these
interfaces; concrete implementations live in ``verticals/``, ``kernel/sandbox``,
``kernel/tools`` and ``kernel/llm``.

Deviations from the spec (see ``specs/ISSUES.md``):

* K-06 ``Literal`` import was missing.
* K-07 ``LLMResponse.parsed`` carries the structured (Instructor-style) output.
* K-08 ``VerticalManifest`` matches the real ``MANIFEST.yaml`` layout (Part 5)
  instead of the abstract field list of Part 1; unknown keys are kept.
* K-09 ``IVertical.get_fixer_prompt`` added (used by the fixer node and
  implemented by ``SaaSWebVertical`` in Part 5).
* K-10 ``IVertical.finalize`` returns a *partial* state update (dict), like
  every node, instead of a full ``AgentState``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .state import AgentState, FileChange, Task, VerificationGateResult

# =============================================================================
# LLM ABSTRACTION
# =============================================================================


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, int] = Field(default_factory=dict)  # {prompt_tokens, completion_tokens, total_tokens}
    model: str
    cost_usd: float = 0.0
    parsed: Any = None  # Instance of `response_model` when requested


@runtime_checkable
class ILLMClient(Protocol):
    """Unified LLM Interface (Structured Output + Streaming)."""

    async def achat(
        self,
        messages: list[LLMMessage],
        model: str,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single call. ``LLMResponse.parsed`` holds the model if ``response_model`` given."""
        ...

    def astream_chat(self, messages: list[LLMMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        """Streaming text chunks (for UI)."""
        ...

    def estimate_tokens(self, messages: list[LLMMessage], model: str) -> int:
        """Local estimation (tiktoken) for budget checks."""
        ...


# =============================================================================
# SANDBOX / EXECUTION ENVIRONMENT
# =============================================================================


class SandboxSpec(BaseModel):
    image: str
    cpu: int = 2
    memory_mb: int = 4096
    env_vars: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)  # Ports to expose (e.g. 3000, 8000)
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
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        """Execute command and wait for completion."""
        ...

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        """Write single file (creates parent dirs)."""
        ...

    async def write_files(self, sandbox_id: str, files: list[FileChange], workdir: str = "/workspace") -> None:
        """Batch apply file changes relative to ``workdir``."""
        ...

    async def read_file(self, sandbox_id: str, path: str) -> str: ...

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[FileStat]: ...

    async def download_dir(self, sandbox_id: str, src_path: str, dest_path: str) -> None:
        """Download directory as archive for artifact packaging."""
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
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ITool(Protocol):
    """Base protocol for all tools."""

    name: str
    description: str
    parameters_json_schema: dict[str, Any]  # For LLM function calling

    async def execute(self, sandbox_id: str, args: dict[str, Any], state: AgentState) -> ToolResult: ...


# =============================================================================
# VERTICAL INTERFACE (The Plugin Contract)
# =============================================================================


class VerticalManifest(BaseModel):
    """Parsed MANIFEST.yaml (layout of specs/05_vertical_saas_web/MANIFEST.yaml)."""

    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    version: str
    description: str = ""
    runtime: dict[str, Any] = Field(default_factory=dict)  # sandbox spec overrides
    tech_stack: dict[str, str] = Field(default_factory=dict)
    skills_dir: str = "skills"
    key_skills: list[str] = Field(default_factory=list)
    planner: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)  # prompt_name -> template_path
    rag_collections: list[str] = Field(default_factory=list)
    compatible_kernels: list[str] = Field(default_factory=list)


class SkillDef(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    description: str = ""
    version: str = "0.1.0"
    tags: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs_json_schema: dict[str, Any] = Field(default_factory=dict)
    template_dir: str = "templates"  # relative to skill root
    post_scripts: list[str] = Field(default_factory=list)  # commands to run after render
    validation: list[str] = Field(default_factory=list)  # commands to validate success


@runtime_checkable
class IVerificationGate(Protocol):
    """Single verification gate (Lint, Test, Typecheck, Build, Custom)."""

    id: str
    name: str
    description: str
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
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, inputs: dict[str, Any]
    ) -> list[FileChange]:
        """Render templates, write files, run post_scripts & validation; return applied changes."""
        ...


@runtime_checkable
class IVertical(Protocol):
    """Main Vertical Entry Point. Loaded by Kernel at startup."""

    @property
    def manifest(self) -> VerticalManifest: ...

    @property
    def skills(self) -> dict[str, SkillDef]: ...

    async def initialize_state(self, request: GenerateRequest) -> dict[str, Any]:
        """Called by `initialize_node`. Returns partial state dict."""
        ...

    def get_planner_prompt(self, state: AgentState) -> str:
        """Render system prompt for Planner (Jinja2 + Context)."""
        ...

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        """Render system prompt for Coder."""
        ...

    def get_fixer_prompt(self, state: AgentState, task: Task) -> str:
        """Render system prompt for Fixer."""
        ...

    def get_verification_gates(self, state: AgentState, task: Task | None) -> list[IVerificationGate]:
        """Task-level gates if ``task`` given, project-level gates if ``None``."""
        ...

    def get_skill_executor(self, skill_id: str) -> ISkillExecutor:
        """Return executor for specific skill."""
        ...

    async def on_task_complete(self, state: AgentState, task: Task) -> None:
        """Hook: Update derived artifacts (e.g. regenerate OpenAPI spec after model change)."""
        ...

    async def finalize(self, state: AgentState) -> dict[str, Any]:
        """Final modifications before packaging. Returns partial state update."""
        ...


# =============================================================================
# REQUEST/RESPONSE (Gateway Contract)
# =============================================================================


class GenerateRequest(BaseModel):
    prompt: str
    vertical_id: str | None = None
    tech_stack_hints: dict[str, str] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    context_files: list[dict[str, str]] = Field(default_factory=list)  # {name, content_base64}
    max_budget_usd: float | None = None
    webhook_url: str | None = None  # For async completion


class GenerateResponse(BaseModel):
    run_id: str
    status: str
    message: str
    stream_url: str  # WebSocket URL for logs
