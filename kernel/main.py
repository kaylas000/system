"""
Service entry point (``specs/06_ops/deployment/DOCKERFILE.kernel``: ``python -m kernel.main serve``;
the spec has no content for this module — written by the agent).

    python -m kernel.main serve --host 0.0.0.0 --port 8000
    python -m kernel.main check        # print the resolved configuration (secrets masked) and exit
    python -m kernel.main migrate      # create the Postgres checkpoint tables
    python -m kernel.main keys create --tenant acme --user alice --role developer   # gateway API key
    python -m kernel.main keys list | keys revoke agk_acme_xx
    python -m kernel.main token --tenant acme --user alice      # HS256 JWT (gateway.jwt_secret)

Wires everything from ``Settings`` (env ``AUTOGEN_*``): checkpointer (Postgres or in-memory),
LiteLLM client, sandbox manager, budget manager, verticals from ``kernel.verticals_dir``,
optional knowledge retriever, logging/tracing, and serves ``kernel.api.create_app``.
"""

from __future__ import annotations

import argparse
import asyncio
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


async def migrate(settings: Settings) -> int:
    """``AsyncPostgresSaver.setup()`` (idempotent). The service also runs it on start-up."""
    from .persistence import open_checkpointer

    if not settings.database.postgres_dsn:
        print("AUTOGEN_DATABASE__POSTGRES_DSN is not set: nothing to migrate (in-memory checkpointer)", file=sys.stderr)
        return 1
    async with open_checkpointer(settings):
        pass
    print("checkpoint tables are up to date")
    return 0


def _masked(settings: Settings) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(settings.model_dump_json())  # SecretStr -> "**********"
    return data


async def _keys_command(settings: Settings, a: argparse.Namespace) -> int:
    from .gateway.auth import issue_jwt
    from .gateway.store import GatewayStore, Principal

    def principal() -> Principal:
        return Principal(
            user_id=a.user,
            tenant_id=a.tenant,
            roles=a.role or ["developer"],
            vertical_access=a.vertical or ["*"],
            tier=a.tier or settings.gateway.default_tier,
        )

    if a.cmd == "token":
        print(issue_jwt(settings, principal(), ttl_seconds=a.ttl))
        return 0
    store = GatewayStore(settings.gateway.db_path)
    try:
        if a.keys_cmd == "create":
            print(await store.create_api_key(principal(), name=a.name))
            print("store it now: the key is not shown again", file=sys.stderr)
        elif a.keys_cmd == "list":
            for k in await store.list_api_keys(a.tenant):
                state = "revoked" if k.revoked_at else "active"
                p = k.principal
                print(f"{k.key_prefix}  {state:7}  tenant={p.tenant_id} user={p.user_id} roles={','.join(p.roles)} "
                      f"tier={p.tier} verticals={','.join(p.vertical_access)} name={k.name}")  # fmt: skip
        else:
            n = await store.revoke_api_key(a.prefix)
            print(f"revoked {n} key(s)")
            return 0 if n else 1
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kernel", description="AutoGen kernel service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API (uvicorn)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("check", help="print resolved settings and discovered verticals")
    sub.add_parser("migrate", help="create/upgrade the Postgres checkpoint tables and exit")
    keys = sub.add_parser("keys", help="manage gateway API keys").add_subparsers(dest="keys_cmd", required=True)
    kc = keys.add_parser("create", help="issue a key (printed once)")
    token = sub.add_parser("token", help="issue an HS256 JWT signed with gateway.jwt_secret")
    for p in (kc, token):
        p.add_argument("--tenant", required=True)
        p.add_argument("--user", required=True)
        p.add_argument("--role", action="append", choices=["viewer", "developer", "admin"], default=None)
        p.add_argument("--tier", default=None)
        p.add_argument("--vertical", action="append", default=None, help="allowed vertical (repeat); default: all")
    kc.add_argument("--name", default="")
    token.add_argument("--ttl", type=int, default=3600)
    kl = keys.add_parser("list")
    kl.add_argument("--tenant", default=None)
    kr = keys.add_parser("revoke")
    kr.add_argument("prefix", help="key prefix as shown by `keys list`")
    a = ap.parse_args(argv)

    settings = get_settings()
    if a.cmd == "check":
        from .skills import VerticalLoader

        loader = VerticalLoader(Path(settings.kernel.verticals_dir))
        out = {"settings": _masked(settings), "verticals": sorted(loader.discover()), "errors": loader.errors}
        print(json.dumps(out, indent=2, default=str))
        return 0

    if a.cmd == "migrate":
        return asyncio.run(migrate(settings))

    if a.cmd in ("keys", "token"):
        return asyncio.run(_keys_command(settings, a))

    import uvicorn

    # single process: active runs and the event bus live in memory (checkpoints are in Postgres)
    uvicorn.run(build_app(settings), host=a.host, port=a.port, proxy_headers=True, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
