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
