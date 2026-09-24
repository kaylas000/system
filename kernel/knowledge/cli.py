"""
``autogen-knowledge`` CLI (``specs/04_knowledge/cli/INGEST_CLI.py``; rewritten by the agent with
argparse instead of click, and with the spec's unimplemented ``reindex`` / ``delete`` done).

    autogen-knowledge ingest ./path/or/git-url --name nextjs_docs_v14 --tags nextjs,docs
    autogen-knowledge reindex --vertical saas_web          # manifest ``knowledge_sources``
    autogen-knowledge search "stream a response" --strategy coding
    autogen-knowledge callers getUser
    autogen-knowledge stats
    autogen-knowledge delete nextjs_docs_v14

Storage/embedder come from ``AUTOGEN_VECTOR_DB__*`` / ``AUTOGEN_KNOWLEDGE__*`` settings;
``--embedder hashing`` works fully offline (lexical quality), ``--no-enrich`` skips LLM calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from kernel.config import get_settings

from .factory import KnowledgeBase, build_ingestion, build_knowledge_base
from .ingestion.pipeline import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, IngestionConfig
from .retrieval.engine import format_context


def _llm() -> Any:
    from kernel.llm import LiteLLMClient

    return LiteLLMClient(get_settings())


async def _ingest_one(kb: KnowledgeBase, cfg: IngestionConfig) -> dict[str, Any]:
    llm = _llm() if cfg.enrich else None
    return await build_ingestion(kb, cfg, llm).run()


def _config_from_args(a: argparse.Namespace) -> IngestionConfig:
    return IngestionConfig(
        source=a.source,
        branch=a.branch,
        repo_name=a.name,
        framework_version=a.framework,
        tags=[t for t in a.tags.split(",") if t] if a.tags else [],
        include_patterns=list(a.include) or list(DEFAULT_INCLUDE),
        exclude_patterns=list(DEFAULT_EXCLUDE) + list(a.exclude),
        enrich=not a.no_enrich,
        incremental=not a.full,
    )


def _vertical_sources(vertical: str) -> list[IngestionConfig]:
    path = Path(vertical)
    manifest = path / "manifest.yaml" if path.is_dir() else Path("verticals") / vertical / "manifest.yaml"
    if not manifest.is_file():
        raise SystemExit(f"manifest not found: {manifest}")
    data = yaml.safe_load(manifest.read_text()) or {}
    sources = data.get("knowledge_sources") or []
    if not sources:
        raise SystemExit(f"{manifest}: no `knowledge_sources` defined")
    out = []
    for s in sources:
        out.append(
            IngestionConfig(
                source=str(s["source"]),
                branch=str(s.get("branch", "")),
                repo_name=str(s.get("name", "")),
                framework_version=str(s.get("framework", "")),
                tags=list(s.get("tags", [])),
                include_patterns=list(s.get("include") or DEFAULT_INCLUDE),
                exclude_patterns=list(DEFAULT_EXCLUDE) + list(s.get("exclude") or []),
                enrich=bool(s.get("enrich", True)),
            )
        )
    return out


async def _main(a: argparse.Namespace) -> int:
    kb = build_knowledge_base(get_settings(), llm=None, embedder_kind=a.embedder)
    try:
        if a.cmd == "ingest":
            print(json.dumps(await _ingest_one(kb, _config_from_args(a)), indent=2))
        elif a.cmd == "reindex":
            for cfg in _vertical_sources(a.vertical):
                cfg.enrich = cfg.enrich and not a.no_enrich
                print(json.dumps(await _ingest_one(kb, cfg), indent=2))
        elif a.cmd == "search":
            state = {"vertical_manifest": {"rag_collections": a.repo}} if a.repo else None
            if a.strategy == "planning":
                res = await kb.engine.retrieve_for_planning(a.query, state, top_k=a.top_k)
            elif a.strategy == "fixing":
                res = await kb.engine.retrieve_for_fixing(a.query, state, top_k=a.top_k)
            else:
                res = await kb.engine.retrieve_for_coding(a.query, state, top_k=a.top_k)
            if a.json:
                print(res.model_dump_json(indent=2))
            else:
                for h in res.hits:
                    p = h.payload
                    loc = f"{p.get('file_path')}:{p.get('start_line', '')}"
                    score = h.rerank_score or h.score
                    print(f"{score:7.3f}  {p.get('repo')}  {loc}  {p.get('symbol_name', '')} [{p.get('chunk_type')}]")
                if a.context:
                    print("\n" + format_context(res))
        elif a.cmd == "callers":
            rows = await kb.graph.query_callers(a.name, repo=a.repo, limit=a.limit)
            for r in rows:
                print(f"{r['file_path']}:{r.get('start_line', '')}  {r['name']} ({r.get('type', '')})")
            if not rows:
                print("no callers found")
        elif a.cmd == "stats":
            print(
                json.dumps(
                    {
                        "vectors": await kb.vector.count({"repo": a.repo} if a.repo else None),
                        "graph": await kb.graph.stats(a.repo),
                    },
                    indent=2,
                )
            )
        elif a.cmd == "delete":
            await kb.vector.delete(a.repo)
            await kb.graph.delete_repo(a.repo)
            print(f"deleted {a.repo}")
    finally:
        await kb.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autogen-knowledge", description="Knowledge base: ingest repositories and query them"
    )
    p.add_argument(
        "--embedder", choices=["litellm", "hashing"], default=None, help="override AUTOGEN_KNOWLEDGE__EMBEDDER"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="ingest a local directory or git URL")
    ing.add_argument("source")
    ing.add_argument("--branch", default="")
    ing.add_argument("--name", default="", help="repo name in the KB (default: from URL / directory)")
    ing.add_argument("--framework", default="", help="framework version tag, e.g. nextjs@14.2.35 (default: auto)")
    ing.add_argument("--tags", default="", help="comma-separated tags added to every chunk")
    ing.add_argument("--no-enrich", action="store_true", help="skip LLM enrichment")
    ing.add_argument("--full", action="store_true", help="re-embed everything (default: only changed files)")
    ing.add_argument("--include", action="append", default=[], help="glob to include (repeatable; replaces defaults)")
    ing.add_argument("--exclude", action="append", default=[], help="extra glob to exclude (repeatable)")

    rei = sub.add_parser("reindex", help="ingest all `knowledge_sources` of a vertical manifest")
    rei.add_argument("--vertical", required=True, help="vertical id (verticals/<id>) or directory")
    rei.add_argument("--no-enrich", action="store_true")

    se = sub.add_parser("search", help="retrieve chunks")
    se.add_argument("query")
    se.add_argument("--strategy", choices=["coding", "planning", "fixing"], default="coding")
    se.add_argument("--repo", action="append", default=[], help="limit to repo(s)")
    se.add_argument("--top-k", type=int, default=8)
    se.add_argument("--json", action="store_true")
    se.add_argument("--context", action="store_true", help="also print the prompt context block")

    ca = sub.add_parser("callers", help="who calls a symbol (code graph)")
    ca.add_argument("name")
    ca.add_argument("--repo", default=None)
    ca.add_argument("--limit", type=int, default=50)

    st = sub.add_parser("stats", help="vector / graph counts")
    st.add_argument("--repo", default=None)

    de = sub.add_parser("delete", help="delete a repo from the KB")
    de.add_argument("repo")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
