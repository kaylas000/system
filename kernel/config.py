"""
Kernel configuration (``specs/01_kernel/ARCHITECTURE.md`` §7).

Values are read from environment variables with the ``AUTOGEN_`` prefix and
``__`` as nested delimiter, e.g. ``AUTOGEN_LLM__PLANNER_MODEL=router/planner``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class KernelSection(BaseModel):
    max_concurrent_runs: int = 20
    default_max_retries: int = 3
    recursion_limit: int = 100  # LangGraph recursion limit (spec: 50, too low for 25 tasks x fix loops)
    generate_docs: bool = True  # documenter writes README/ARCHITECTURE via LLM if the vertical has a prompt
    final_verification: bool = True  # run all blocking gates once before packaging
    verticals_dir: str = "verticals"  # VerticalLoader root (service mode)
    default_vertical: str = "saas_web"  # used when a request has no vertical_id (gateway classifier: phase 7)


class LLMSection(BaseModel):
    provider: str = "litellm"  # "litellm" (real calls) | "fake" (tests only)
    # LiteLLM proxy URL. None -> call providers directly (model names like "anthropic/claude-...").
    gateway_url: str | None = None
    api_key: SecretStr | None = None  # gateway / provider key (provider keys may also come from their own env vars)
    max_structured_retries: int = 2  # re-ask on invalid structured output
    default_model: str = "router/coder"
    planner_model: str = "router/planner"
    fixer_model: str = "router/coder"
    request_timeout: int = 120
    max_tokens_per_run: int = 2_000_000


class SandboxSection(BaseModel):
    provider: str = "local"  # "e2b" | "docker" | "local" (dev/test only, no isolation)
    api_key: SecretStr | None = None
    default_image: str = "ghcr.io/autogen/sandbox-base:latest"
    workspace_path: str = "/workspace"
    cpu: int = 2
    memory_mb: int = 4096
    timeout_sec: int = 300
    max_concurrent: int = 20  # SandboxManager quota
    ttl_minutes: int = 60  # idle sandboxes are closed after this
    docker_network: str = "bridge"  # DockerSandbox only
    # E2B uses template IDs, not docker images: SandboxSpec.image -> template (fallback: e2b_default_template)
    e2b_template_map: dict[str, str] = Field(default_factory=dict)
    e2b_default_template: str | None = None  # None -> E2B base template


class DatabaseSection(BaseModel):
    postgres_dsn: str | None = None  # None -> in-memory checkpointer
    pool_size: int = 20


class VectorDBSection(BaseModel):
    qdrant_url: str | None = None  # e.g. "http://qdrant:6333"; None -> embedded Qdrant at ``qdrant_path``
    api_key: SecretStr | None = None
    qdrant_path: str = ".data/qdrant"  # embedded mode storage (":memory:" for throw-away)
    collection: str = "code_chunks"


class KnowledgeSection(BaseModel):
    """Knowledge base (``specs/04_knowledge``). Disabled by default: agents work without RAG."""

    enabled: bool = False
    graph_path: str = ".data/knowledge_graph.sqlite"  # ":memory:" for throw-away
    embedder: str = "litellm"  # "litellm" | "hashing" (offline, lexical only)
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 768
    embedding_api_base: str | None = None  # None -> llm.gateway_url
    enrichment_model: str = "router/enricher"
    reranker: str = "heuristic"  # "heuristic" | "llm" | "cross_encoder"
    reranker_model: str = "router/reranker"
    top_k_planning: int = 8
    top_k_coding: int = 6
    top_k_fixing: int = 5
    max_context_chars: int = 6000


class ObservabilitySection(BaseModel):
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "autogen-kernel"
    otel_endpoint: str | None = None  # OTLP/gRPC, e.g. "http://otel-collector:4317"; None -> no trace export
    log_level: str = "INFO"
    log_json: bool = True


class TierLimits(BaseModel):
    rpm: int  # requests per minute (all authenticated API calls)
    rpd: int  # generations per day
    concurrent: int  # runs executing at the same time (per tenant)


def _default_tiers() -> dict[str, TierLimits]:
    return {
        "free": TierLimits(rpm=10, rpd=100, concurrent=1),
        "pro": TierLimits(rpm=60, rpd=1000, concurrent=5),
        "enterprise": TierLimits(rpm=300, rpd=10000, concurrent=20),
    }


class GatewaySection(BaseModel):
    """Public API (``kernel.gateway``): auth, quotas, routing, composition."""

    auth_enabled: bool = True  # False only for local development (every request is an anonymous admin)
    jwt_secret: SecretStr | None = None  # HS256 tokens
    jwt_public_key: str | None = None  # PEM for RS256/ES256 tokens
    jwks_url: str | None = None  # Auth0 / Clerk / Keycloak JWKS endpoint
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["HS256", "RS256", "ES256"])
    jwt_issuer: str | None = "autogen-platform"
    jwt_audience: str | None = None
    db_path: str = ".data/gateway.sqlite"  # API keys, run registry, idempotency keys
    redis_url: str | None = None  # shared rate limits across replicas; None -> in-process counters
    tiers: dict[str, TierLimits] = Field(default_factory=_default_tiers)
    default_tier: str = "pro"
    classifier_model: str = "router/classifier"
    composer_model: str = "router/planner"
    public_base_url: str | None = None  # e.g. "https://api.example.com" for stream_url; None -> from the request
    cors_origins: list[str] = Field(default_factory=list)
    max_budget_usd: float = 100.0  # upper bound for GenerateRequest.max_budget_usd
    allow_private_webhooks: bool = False  # webhook_url to private / loopback addresses (SSRF guard)


class BudgetSection(BaseModel):
    """Limits enforced by ``kernel.llm.budget.BudgetManager`` (in addition to ``max_budget_usd`` per request)."""

    max_cost_usd_per_run: float | None = None  # default for requests without max_budget_usd
    max_cost_usd_per_day: float | None = None  # all runs together
    max_tokens_per_minute: int | None = None
    alert_threshold_pct: float = 0.8
    alert_webhook_url: str | None = None  # Slack-compatible incoming webhook
    rate_limit_max_wait_s: float = 60.0
    usage_db_path: str | None = ".data/llm_usage.sqlite"  # None -> in-memory (lost on restart)


class HITLSection(BaseModel):
    plan_review: bool = False  # Interrupt after planner for human approval


class ArtifactsSection(BaseModel):
    local_dir: Path = Path("artifacts")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOGEN_", env_nested_delimiter="__", extra="ignore", env_ignore_empty=True
    )

    kernel: KernelSection = Field(default_factory=KernelSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    vector_db: VectorDBSection = Field(default_factory=VectorDBSection)
    knowledge: KnowledgeSection = Field(default_factory=KnowledgeSection)
    budget: BudgetSection = Field(default_factory=BudgetSection)
    gateway: GatewaySection = Field(default_factory=GatewaySection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)
    hitl: HITLSection = Field(default_factory=HITLSection)
    artifacts: ArtifactsSection = Field(default_factory=ArtifactsSection)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
