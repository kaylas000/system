# specs/04_knowledge/ingestion/CHUNKING_STRATEGIES.py
"""
Chunking Strategies.
Transforms flat list of Symbols into Retrieval Chunks with Context.
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path
from .PARSERS import Symbol, SymbolType


@dataclass
class RetrievalChunk:
    id: str  # Global unique: repo#file#symbol
    content: str  # The text to embed (Code + Docstring + Metadata summary)
    metadata: Dict[str, Any]  # Payload for Qdrant
    # Relationships for Graph DB
    graph_edges: List[Tuple[str, str, str]] = []  # (src_id, rel_type, dst_id)


class ChunkingStrategy:
    def __init__(self, repo_name: str, repo_url: str, branch: str, framework_version: str = ""):
        self.repo_name = repo_name
        self.repo_url = repo_url
        self.branch = branch
        self.framework_version = framework_version

    def create_chunks(self, symbols: List[Symbol], file_content_map: Dict[str, str]) -> List[RetrievalChunk]:
        """
        Main entry point.
        1. Create Symbol Chunks.
        2. Create File Summary Chunks.
        3. Create Config Chunks (if detected).
        4. Build Graph Edges.
        """
        chunks = []
        symbol_map = {f"{s.file_path}:{s.name}": s for s in symbols}

        # 1. Symbol Chunks
        for sym in symbols:
            if sym.type in (SymbolType.IMPORT, SymbolType.EXPORT, SymbolType.CALL):
                continue  # Handled via edges
            chunk = self._create_symbol_chunk(sym, file_content_map)
            chunks.append(chunk)

        # 2. File Summary Chunks
        file_symbols = {}
        for s in symbols:
            file_symbols.setdefault(s.file_path, []).append(s)
        for fpath, syms in file_symbols.items():
            chunks.append(self._create_file_summary_chunk(fpath, syms, file_content_map.get(fpath, "")))

        # 3. Config Chunks (Heuristic)
        for fpath, content in file_content_map.items():
            if self._is_config_file(fpath):
                chunks.append(self._create_config_chunk(fpath, content))

        return chunks

    def _create_symbol_chunk(self, sym: Symbol, file_map: Dict[str, str]) -> RetrievalChunk:
        # Content for Embedding: Signature + Docstring + Code (truncated)
        # We want the *signature* and *intent* to dominate the vector.
        embed_text = f"# {sym.signature}\n"
        if sym.docstring:
            embed_text += f"# Doc: {sym.docstring}\n"
        embed_text += f"# File: {sym.file_path}\n"
        embed_text += sym.code[:2000]  # Limit code size for embedding

        # Metadata for Filtering
        meta = {
            "repo": self.repo_name,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "file_path": sym.file_path,
            "symbol_name": sym.name,
            "symbol_type": sym.type.value,
            "language": self._detect_lang(sym.file_path),
            "framework_version": self.framework_version,
            "parent": sym.parent or "",
            "decorators": sym.decorators,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            # Enrichment fields (filled later)
            "intent": sym.intent,
            "pattern": sym.pattern,
            "complexity": sym.complexity,
            "side_effects": sym.side_effects,
            "tags": sym.tags,
        }

        chunk_id = f"{self.repo_name}#{sym.file_path}#{sym.name}#{sym.start_line}"

        # Graph Edges
        edges = []
        for imp in sym.imports:
            edges.append((chunk_id, "IMPORTS", self._resolve_import_id(imp, sym.file_path)))
        for call in sym.calls:
            edges.append((chunk_id, "CALLS", self._resolve_call_id(call, sym.file_path)))
        if sym.parent:
            edges.append((chunk_id, "BELONGS_TO", f"{self.repo_name}#{sym.file_path}#{sym.parent}"))

        return RetrievalChunk(id=chunk_id, content=embed_text, metadata=meta, graph_edges=edges)

    def _create_file_summary_chunk(self, file_path: str, symbols: List[Symbol], content: str) -> RetrievalChunk:
        imports = [s.name for s in symbols if s.type == SymbolType.IMPORT]
        exports = [
            s.name
            for s in symbols
            if s.type in (SymbolType.FUNCTION, SymbolType.CLASS, SymbolType.INTERFACE) and not s.parent
        ]

        summary = f"# File: {file_path}\n"
        summary += f"# Imports: {', '.join(imports[:20])}\n"
        summary += f"# Exports: {', '.join(exports[:20])}\n"
        summary += f"# Symbol Count: {len(symbols)}\n"

        meta = {
            "repo": self.repo_name,
            "file_path": file_path,
            "chunk_type": "file_summary",
            "language": self._detect_lang(file_path),
            "framework_version": self.framework_version,
            "symbols": [s.name for s in exports],
            "imports": imports,
        }
        chunk_id = f"{self.repo_name}#{file_path}#__file_summary__"
        return RetrievalChunk(id=chunk_id, content=summary, metadata=meta, graph_edges=[])

    def _create_config_chunk(self, file_path: str, content: str) -> RetrievalChunk:
        meta = {
            "repo": self.repo_name,
            "file_path": file_path,
            "chunk_type": "config",
            "language": self._detect_lang(file_path),
            "framework_version": self.framework_version,
        }
        chunk_id = f"{self.repo_name}#{file_path}#__config__"
        return RetrievalChunk(id=chunk_id, content=content[:4000], metadata=meta, graph_edges=[])

    def _is_config_file(self, path: str) -> bool:
        name = Path(path).name
        return name in (
            "package.json",
            "tsconfig.json",
            "prisma.schema",
            "dockerfile",
            "docker-compose.yml",
            ".github/workflows/",
            "pyproject.toml",
            "cargo.toml",
            "go.mod",
            "pom.xml",
        )

    def _detect_lang(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        return {".ts": "typescript", ".tsx": "tsx", ".py": "python", ".go": "go", ".rs": "rust"}.get(ext, "text")

    def _resolve_import_id(self, imp: str, from_file: str) -> str:
        # Heuristic: We don't know target file yet. Use placeholder resolved in Graph Build step.
        return f"import:{imp}"

    def _resolve_call_id(self, call: str, from_file: str) -> str:
        return f"call:{call}"
