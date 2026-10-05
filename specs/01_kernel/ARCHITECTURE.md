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
