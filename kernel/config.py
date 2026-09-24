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
    qdrant_url: str = "http://qdrant:6333"
    api_key: SecretStr | None = None


class ObservabilitySection(BaseModel):
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "autogen-kernel"
    otel_endpoint: str | None = None


class HITLSection(BaseModel):
    plan_review: bool = False  # Interrupt after planner for human approval


class ArtifactsSection(BaseModel):
    local_dir: Path = Path("artifacts")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOGEN_", env_nested_delimiter="__", extra="ignore")

    kernel: KernelSection = Field(default_factory=KernelSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    vector_db: VectorDBSection = Field(default_factory=VectorDBSection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)
    hitl: HITLSection = Field(default_factory=HITLSection)
    artifacts: ArtifactsSection = Field(default_factory=ArtifactsSection)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
