# specs/04_knowledge/ingestion/INGESTION_PIPELINE.py
"""
Main Ingestion Orchestrator.
Runs as: CLI Tool, GitHub Action, or Scheduled Service.
"""

from __future__ import annotations
import asyncio
import hashlib
import os
import tempfile
import shutil
from pathlib import Path
from typing: List, Dict, Any, Optional
from dataclasses import dataclass

from kernel.config import settings
from .PARSERS import MultiLanguageParser
from .CHUNKING_STRATEGIES import ChunkingStrategy, RetrievalChunk
from .ENRICHMENT import EnrichmentPipeline
from ..storage.QDRANT_CLIENT import QdrantKB
from ..storage.KUZU_CLIENT import KuzuGraph
from kernel.llm.client import LiteLLMClient

@dataclass
class IngestionConfig:
    repo_url: str
    branch: str = "main"
    repo_name: str = "" # Auto from URL
    framework_version: str = ""
    vertical_tags: List[str] = None
    include_patterns: List[str] = ["**/*.ts", "**/*.tsx", "**/*.py", "**/*.go", "**/*.rs", "**/*.json", "**/*.yml", "**/*.md"]
    exclude_patterns: List[str] = ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.git/**", "**/__pycache__/**", "**/*.test.ts", "**/*.spec.ts"]
    batch_size: int = 100
    enrich: bool = True

class IngestionPipeline:
    def __init__(self, config: IngestionConfig):
        self.config = config
        self.parser = MultiLanguageParser()
        self.chunker = ChunkingStrategy(
            repo_name=config.repo_name or self._extract_repo_name(config.repo_url),
            repo_url=config.repo_url,
            branch=config.branch,
            framework_version=config.framework_version
        )
        self.vector_db = QdrantKB(settings.vector_db.qdrant_url, settings.vector_db.api_key)
        self.graph_db = KuzuGraph(settings.knowledge.graph_db_path)
        self.llm = LiteLLMClient()
        self.enricher = EnrichmentPipeline(self.llm) if config.enrich else None
        
        self._temp_dir: Optional[Path] = None

    def _extract_repo_name(self, url: str) -> str:
        return url.split("/")[-1].replace(".git", "").replace(".", "_")

    async def run(self) -> Dict[str, Any]:
        """Full Pipeline: Clone -> Parse -> Chunk -> Enrich -> Embed -> Store."""
        stats = {"files": 0, "symbols": 0, "chunks": 0, "vectors": 0, "errors": 0}
        
        try:
            # 1. Clone / Checkout
            repo_path = await self._prepare_repo()
            
            # 2. Scan Files
            files = self._scan_files(repo_path)
            stats["files"] = len(files)
            
            # 3. Parse & Chunk (Streaming to avoid memory issues)
            all_chunks = []
            file_content_map = {}
            
            for file_path in files:
                try:
                    content = file_path.read_bytes()
                    file_content_map[str(file_path.relative_to(repo_path))] = content.decode('utf-8', errors='ignore')
                    symbols = self.parser.parse_file(file_path.relative_to(repo_path), content)
                    stats["symbols"] += len(symbols)
                except Exception as e:
                    stats["errors"] += 1
                    print(f"[Ingest] Parse error {file_path}: {e}")
            
            # 4. Chunking
            chunks = self.chunker.create_chunks(symbols, file_content_map)
            stats["chunks"] = len(chunks)
            
            # 5. Enrichment (LLM)
            if self.enricher:
                chunks = await self.enricher.enrich_chunks(chunks)
            
            # 6. Embedding
            vectors = await self._embed_chunks(chunks)
            stats["vectors"] = len(vectors)
            
            # 7. Store
            await self.vector_db.upsert_chunks(chunks, vectors)
            self.graph_db.upsert_chunks(chunks)
            
            # 8. Store Raw MD (Debug/Backup)
            await self._store_raw_chunks(chunks)
            
        finally:
            self._cleanup()
            
        return stats

    async def _prepare_repo(self) -> Path:
        if self.config.repo_url.startswith("http"):
            self._temp_dir = Path(tempfile.mkdtemp(prefix="autogen_ingest_"))
            # Use git clone --depth=1 --branch
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth=1", "--branch", self.config.branch, 
                self.config.repo_url, str(self._temp_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Git clone failed for {self.config.repo_url}")
            return self._temp_dir
        else:
            # Local path
            return Path(self.config.repo_url)

    def _scan_files(self, root: Path) -> List[Path]:
        import pathspec
        # Compile gitignore-style patterns
        spec = pathspec.PathSpec.from_lines('gitwildmatch', self.config.exclude_patterns)
        include_spec = pathspec.PathSpec.from_lines('gitwildmatch', self.config.include_patterns)
        
        files = []
        for f in root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                if not spec.match_file(rel) and include_spec.match_file(rel):
                    files.append(f)
        return files

    async def _embed_chunks(self, chunks: List[RetrievalChunk]) -> List[List[float]]:
        # Batch embedding via API (OpenAI/Cohere/Local)
        texts = [c.content for c in chunks]
        # return await self.llm.embed_batch(texts) 
        return [[0.0]*768] * len(texts) # Placeholder

    async def _store_raw_chunks(self, chunks: List[RetrievalChunk]):
        # Save to S3/MinIO or local FS for debugging
        pass

    def _cleanup(self):
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self.graph_db.close()
