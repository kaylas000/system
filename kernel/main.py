"""
Service entry point (``specs/06_ops/deployment/DOCKERFILE.kernel``: ``python -m kernel.main serve``;
the spec has no content for this module — written by the agent).

    python -m kernel.main serve --host 0.0.0.0 --port 8000
    python -m kernel.main check        # print the resolved configuration (secrets masked) and exit

Wires everything from ``Settings`` (env ``AUTOGEN_*``): checkpointer (Postgres or in-memory),
LiteLLM client, sandbox manager, budget manager, verticals from ``kernel.verticals_dir``,
optional knowledge retriever, logging/tracing, and serves ``kernel.api.create_app``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_components(settings: Settings) -> dict[str, Any]:
    """Everything except the checkpointer (which needs an async context)."""
    from .llm import LiteLLMClient
    from .llm.budget import build_budget_manager
    from .sandbox import create_sandbox
    from .skills import VerticalLoader

    loader = VerticalLoader(Path(settings.kernel.verticals_dir))

    def resolve(vertical_id: str) -> Any:
        return loader.load_vertical(vertical_id or settings.kernel.default_vertical)

    return {
        "loader": loader,
        "resolve_vertical": resolve,
        "llm_client": LiteLLMClient(settings),
        "sandbox": create_sandbox(settings),
        "budget": build_budget_manager(settings),
    }


def build_app(settings: Settings | None = None) -> Any:
    from fastapi import FastAPI

    from .api import create_app
    from .graph import build_graph
    from .observability import configure_logging, setup_langsmith, setup_tracing
    from .persistence import open_checkpointer
    from .service import RunManager

    settings = settings or get_settings()
    configure_logging(settings.observability.log_level, settings.observability.log_json)
    setup_tracing(settings)
    setup_langsmith(settings)
    parts = build_components(settings)

    @asynccontextmanager
    async def setup(app: FastAPI) -> AsyncIterator[None]:
        async with open_checkpointer(settings) as checkpointer:
            graph = build_graph(None, checkpointer=checkpointer, budget=parts["budget"])
            app.state.run_manager = RunManager(
                graph,
                parts["resolve_vertical"],
                settings,
                llm_client=parts["llm_client"],
                sandbox=parts["sandbox"],
                budget=parts["budget"],
            )
            logger.info("kernel service ready (verticals: %s)", sorted(parts["loader"].discover()))
            yield

    return create_app(None, settings, setup=setup)


def _masked(settings: Settings) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(settings.model_dump_json())  # SecretStr -> "**********"
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kernel", description="AutoGen kernel service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API (uvicorn)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("check", help="print resolved settings and discovered verticals")
    a = ap.parse_args(argv)

    settings = get_settings()
    if a.cmd == "check":
        from .skills import VerticalLoader

        loader = VerticalLoader(Path(settings.kernel.verticals_dir))
        out = {"settings": _masked(settings), "verticals": sorted(loader.discover()), "errors": loader.errors}
        print(json.dumps(out, indent=2, default=str))
        return 0

    import uvicorn

    # single process: active runs and the event bus live in memory (checkpoints are in Postgres)
    uvicorn.run(build_app(settings), host=a.host, port=a.port, proxy_headers=True, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
