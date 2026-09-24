"""
Ingestion pipeline (``specs/04_knowledge/ingestion/INGESTION_PIPELINE.py``; rewritten by the agent).

clone/scan -> parse -> chunk (+ graph) -> enrich -> embed -> store.

* the whole repository is parsed and the **graph is rebuilt** every run (cheap, keeps cross-file
  CALLS/IMPORTS exact);
* vectors are **incremental**: only files whose content hash changed are re-enriched, re-embedded
  and re-upserted; deleted files are removed from Qdrant;
* spec defects fixed (ISSUES KN-08): mutable dataclass defaults, ``from typing:`` syntax error,
  hard-wired global clients (now injected), ``_embed`` placeholder of zeros.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import RetrievalChunk
from ..storage.graph_store import IGraphStore
from ..storage.qdrant_store import QdrantKB
from .chunking import CodeChunker, RepoInfo, detect_framework_version
from .embedding import IEmbedder
from .enrichment import EnrichmentPipeline
from .parsers import MultiLanguageParser

logger = logging.getLogger(__name__)

DEFAULT_INCLUDE = [
    "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs", "**/*.py", "**/*.go",
    "**/*.md", "**/*.mdx", "**/*.json", "**/*.yml", "**/*.yaml", "**/*.toml", "**/*.prisma",
    "**/Dockerfile*", "**/.env.example", "**/go.mod", "**/requirements.txt",
]  # fmt: skip
DEFAULT_EXCLUDE = [
    "**/node_modules/**", "**/.git/**", "**/dist/**", "**/build/**", "**/.next/**", "**/out/**",
    "**/coverage/**", "**/__pycache__/**", "**/.venv/**", "**/venv/**", "**/vendor/**",
    "**/*.min.js", "**/*.map", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
    "**/*.snap", "**/*.d.ts",
]  # fmt: skip


@dataclass
class IngestionConfig:
    source: str  # git URL or local path
    branch: str = ""
    repo_name: str = ""  # default: derived from URL / directory name
    framework_version: str = ""  # default: auto-detected from package.json / pyproject / go.mod
    tags: list[str] = field(default_factory=list)  # added to every chunk (e.g. vertical tags)
    include_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    enrich: bool = True
    incremental: bool = True
    respect_gitignore: bool = True
    max_file_bytes: int = 300_000
    embed_batch: int = 256


def is_git_url(source: str) -> bool:
    return bool(re.match(r"^(https?://|git@|ssh://|git://)", source)) or (
        source.endswith(".git") and not Path(source).exists()
    )


def repo_name_from(source: str) -> str:
    name = source.rstrip("/").split("/")[-1].split(":")[-1]
    name = name[:-4] if name.endswith(".git") else name
    if is_git_url(source):
        owner = source.rstrip("/").split("/")[-2].split(":")[-1] if "/" in source else ""
        name = f"{owner}_{name}" if owner else name
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name) or "repo"


class _Matcher:
    """gitwildmatch patterns via ``pathspec`` (fnmatch fallback)."""

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = patterns
        self._fn: Callable[[str], bool]
        try:
            import pathspec

            spec: Any
            try:
                spec = pathspec.PathSpec.from_lines("gitignore", patterns)
            except (KeyError, LookupError):  # pathspec < 0.12
                spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
            self._fn = spec.match_file
        except ImportError:  # pragma: no cover - pathspec is part of the knowledge extra
            self._fn = self._fallback

    def _fallback(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        for p in self.patterns:
            if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p.removeprefix("**/")):
                return True
            core = p.strip("*/")
            if p.startswith("**/") and p.endswith("/**") and f"/{core}/" in f"/{path}":
                return True
        return False

    def __call__(self, path: str) -> bool:
        return bool(self.patterns) and self._fn(path)


class IngestionPipeline:
    def __init__(
        self,
        config: IngestionConfig,
        vector: QdrantKB,
        embedder: IEmbedder,
        graph: IGraphStore | None = None,
        enricher: EnrichmentPipeline | None = None,
        parser: MultiLanguageParser | None = None,
    ) -> None:
        self.config = config
        self.vector = vector
        self.embedder = embedder
        self.graph = graph
        self.enricher = enricher if config.enrich else None
        self.parser = parser or MultiLanguageParser()
        self.repo_name = config.repo_name or repo_name_from(config.source)
        self._temp_dir: Path | None = None

    async def run(self) -> dict[str, Any]:
        started = time.monotonic()
        stats: dict[str, Any] = {"repo": self.repo_name}
        try:
            root = await self._prepare_repo()
            files = self.scan_files(root)
            stats["files"] = len(files)
            framework = self.config.framework_version or detect_framework_version(files)
            stats["framework_version"] = framework

            parsed = [self.parser.parse_file(p, c) for p, c in files.items() if self.parser.supports(p)]
            stats["parsed_files"] = len(parsed)
            stats["symbols"] = sum(len(pf.symbols) for pf in parsed)
            chunker = CodeChunker(RepoInfo(self.repo_name, self._repo_url(), self.config.branch, framework))
            result = chunker.build(parsed, files)

            if self.graph is not None:
                await self.graph.replace_repo(self.repo_name, result.nodes, result.edges)
                stats["graph_nodes"] = len(result.nodes)
                stats["graph_edges"] = len(result.edges)

            hashes = {p: hashlib.sha256(c.encode("utf-8")).hexdigest()[:16] for p, c in files.items()}
            for chunk in result.chunks:
                path = str(chunk.metadata.get("file_path", ""))
                chunk.metadata["file_hash"] = hashes.get(path, "")
                if self.config.tags:
                    chunk.metadata["tags"] = sorted(set(chunk.metadata.get("tags") or []) | set(self.config.tags))

            existing = await self.vector.file_hashes(self.repo_name) if self.config.incremental else {}
            if self.config.incremental:
                changed = {p for p, h in hashes.items() if existing.get(p) != h}
                removed = set(existing) - set(hashes)
                await self.vector.delete(self.repo_name, sorted(changed | removed))
            else:
                changed, removed = set(hashes), set()
                await self.vector.delete(self.repo_name)
            to_index = [c for c in result.chunks if c.metadata.get("file_path") in changed]
            stats.update(
                {"changed_files": len(changed), "removed_files": len(removed), "chunks_total": len(result.chunks)}
            )

            if self.enricher is not None and to_index:
                await self.enricher.enrich_chunks(to_index)
                stats["enrichment"] = dict(self.enricher.stats)
            stats["vectors"] = await self._embed_and_store(to_index)
        finally:
            self._cleanup()
        stats["seconds"] = round(time.monotonic() - started, 2)
        logger.info("ingestion done: %s", stats)
        return stats

    def _repo_url(self) -> str:
        return self.config.source if is_git_url(self.config.source) else ""

    async def _prepare_repo(self) -> Path:
        if not is_git_url(self.config.source):
            root = await asyncio.to_thread(lambda: Path(self.config.source).expanduser().resolve())
            if not root.is_dir():
                raise FileNotFoundError(f"source directory not found: {root}")
            return root
        self._temp_dir = Path(tempfile.mkdtemp(prefix="kb-ingest-"))
        cmd = ["git", "clone", "--depth", "1", "--single-branch"]
        if self.config.branch:
            cmd += ["--branch", self.config.branch]
        cmd += [self.config.source, str(self._temp_dir / "repo")]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {err.decode(errors='replace').strip()[:500]}")
        return self._temp_dir / "repo"

    def scan_files(self, root: Path) -> dict[str, str]:
        include = _Matcher(self.config.include_patterns)
        exclude = _Matcher(self.config.exclude_patterns)
        gitignore = _Matcher(self._gitignore_lines(root)) if self.config.respect_gitignore else _Matcher([])
        out: dict[str, str] = {}
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            rel = f.relative_to(root).as_posix()
            if exclude(rel) or gitignore(rel) or not include(rel):
                continue
            try:
                if f.stat().st_size > self.config.max_file_bytes:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue  # binary
            out[rel] = data.decode("utf-8", errors="replace")
        return out

    @staticmethod
    def _gitignore_lines(root: Path) -> list[str]:
        gi = root / ".gitignore"
        if not gi.is_file():
            return []
        return [
            ln.strip() for ln in gi.read_text(errors="replace").splitlines() if ln.strip() and not ln.startswith("#")
        ]

    async def _embed_and_store(self, chunks: list[RetrievalChunk]) -> int:
        total = 0
        batch = self.config.embed_batch
        for i in range(0, len(chunks), batch):
            part = chunks[i : i + batch]
            vectors = await self.embedder.embed([c.content for c in part])
            total += await self.vector.upsert(part, vectors)
        return total

    def _cleanup(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
