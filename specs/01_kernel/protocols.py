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
