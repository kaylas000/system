"""
Data model of the knowledge base (``specs/04_knowledge``; written by the agent).

Fixes vs spec (ISSUES KN-xx): ``RetrievalChunk.graph_edges`` had a mutable default ``[]``
on a dataclass (``ValueError`` at import) and used ``Tuple`` without importing it;
``SymbolType.CALL`` was referenced but not defined.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SymbolType(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE_ALIAS = "type_alias"
    ENUM = "enum"
    CONSTANT = "constant"
    VARIABLE = "variable"
    COMPONENT = "component"  # React component (PascalCase function returning JSX)
    HOOK = "hook"  # useXxx
    STRUCT = "struct"  # Go
    FILE = "file"  # fallback: whole file


class ChunkType(StrEnum):
    SYMBOL = "symbol"
    FILE_SUMMARY = "file_summary"
    CONFIG = "config"
    DOC = "doc"
    PATTERN = "pattern"
    ERROR_FIX = "error_fix"


class ImportRef(BaseModel):
    source: str  # module specifier as written: "@/server/db", "./utils", "react", "os.path"
    names: list[str] = Field(default_factory=list)  # imported local names ("*" for namespace)
    is_default: bool = False
    line: int = 0


class Symbol(BaseModel):
    name: str
    type: SymbolType
    file_path: str
    start_line: int
    end_line: int
    code: str
    signature: str
    docstring: str | None = None
    parent: str | None = None  # enclosing class
    decorators: list[str] = Field(default_factory=list)
    exported: bool = False
    calls: list[str] = Field(default_factory=list)  # called identifiers (unresolved names)
    bases: list[str] = Field(default_factory=list)  # extends
    implements: list[str] = Field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


class ParsedFile(BaseModel):
    path: str
    language: str
    content: str
    symbols: list[Symbol] = Field(default_factory=list)
    imports: list[ImportRef] = Field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:16]


class GraphEdge(BaseModel):
    src: str
    rel: str  # CONTAINS | IMPORTS | CALLS | INHERITS | IMPLEMENTS
    dst: str
    props: dict[str, Any] = Field(default_factory=dict)


class RetrievalChunk(BaseModel):
    id: str  # global unique: repo#file#symbol#line
    content: str  # text to embed and to show to agents
    metadata: dict[str, Any] = Field(default_factory=dict)  # Qdrant payload
    graph_edges: list[GraphEdge] = Field(default_factory=list)

    @property
    def chunk_type(self) -> str:
        return str(self.metadata.get("chunk_type", ""))


class SearchHit(BaseModel):
    id: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "vector"  # vector | graph | symbol
    rerank_score: float | None = None

    @property
    def content(self) -> str:
        return str(self.payload.get("content", ""))


class GraphNode(BaseModel):
    id: str
    label: str  # Repo | File | Symbol | Config | Module
    name: str
    repo: str = ""
    file_path: str = ""
    props: dict[str, Any] = Field(default_factory=dict)
