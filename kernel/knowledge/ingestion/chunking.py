"""
Chunking + graph extraction (``specs/04_knowledge/ingestion/CHUNKING_STRATEGIES.py``; rewritten by the agent).

One pass over the parsed repository produces:

* ``RetrievalChunk`` s for Qdrant: ``symbol`` (one per function/class/...), ``file_summary``
  (exports + imports + head of file; also used for files without a parser), ``config``
  (package.json, tsconfig, prisma schema, Dockerfile, ...) and ``doc`` (Markdown split by headings);
* ``GraphNode`` / ``GraphEdge`` s following ``storage/GRAPH_DB_SCHEMA.md``: Repo/File/Symbol/Config/Module
  nodes and CONTAINS, IMPORTS, IMPORTS_FILE, CALLS, INHERITS, IMPLEMENTS edges.

Import and call resolution is heuristic but repository-aware (spec left ``import:<name>`` /
``call:<name>`` placeholders, ISSUES KN-05): relative and ``@/`` / tsconfig ``paths`` imports are
resolved to files, calls are resolved to the same file, then to imported names, then to a unique
exported symbol elsewhere in the repo.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..models import ChunkType, GraphEdge, GraphNode, ParsedFile, RetrievalChunk, Symbol, SymbolType

MAX_CHUNK_CHARS = 4000
FILE_HEAD_LINES = 40

CONFIG_FILES: dict[str, str] = {
    "package.json": "package_json",
    "tsconfig.json": "tsconfig",
    "jsconfig.json": "tsconfig",
    "schema.prisma": "prisma",
    "dockerfile": "dockerfile",
    "docker-compose.yml": "docker_compose",
    "docker-compose.yaml": "docker_compose",
    "compose.yml": "docker_compose",
    "compose.yaml": "docker_compose",
    "pyproject.toml": "pyproject",
    "requirements.txt": "requirements",
    "cargo.toml": "cargo",
    "go.mod": "go_mod",
    "pom.xml": "maven",
    "next.config.js": "next_config",
    "next.config.mjs": "next_config",
    "next.config.ts": "next_config",
    "tailwind.config.ts": "tailwind_config",
    "tailwind.config.js": "tailwind_config",
    ".env.example": "env_example",
    "manifest.yaml": "manifest",
}

DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt"}
TS_RESOLVE_SUFFIXES = ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"]
# Calls with these names are never resolved via the repo-wide fallback (too ambiguous).
_AMBIGUOUS_CALLS = {
    "get",
    "set",
    "run",
    "main",
    "init",
    "update",
    "find",
    "json",
    "map",
    "push",
    "then",
    "log",
    "error",
}


@dataclass
class RepoInfo:
    name: str
    url: str = ""
    branch: str = ""
    framework_version: str = ""


@dataclass
class ChunkingResult:
    chunks: list[RetrievalChunk] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


def config_type(path: str) -> str | None:
    p = PurePosixPath(path)
    name = p.name.lower()
    if ".github/workflows/" in path and p.suffix in (".yml", ".yaml"):
        return "ci_workflow"
    if name.startswith("dockerfile"):
        return "dockerfile"
    return CONFIG_FILES.get(name)


def is_doc_file(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in DOC_EXTENSIONS


def detect_framework_version(files: dict[str, str]) -> str:
    """``nextjs@14.2.35`` / ``fastapi@0.115.0`` / ``django@5.1`` / ``go@1.22`` from manifests (best effort)."""
    pkg = files.get("package.json")
    if pkg:
        try:
            data = json.loads(pkg)
            deps = {**data.get("devDependencies", {}), **data.get("dependencies", {})}
            for dep, label in (
                ("next", "nextjs"),
                ("nuxt", "nuxt"),
                ("@sveltejs/kit", "sveltekit"),
                ("react", "react"),
                ("express", "express"),
            ):
                if dep in deps:
                    return f"{label}@{str(deps[dep]).lstrip('^~>=')}"
        except (ValueError, AttributeError):
            pass
    py = files.get("pyproject.toml", "") + "\n" + files.get("requirements.txt", "")
    for lib in ("fastapi", "django", "flask"):
        m = re.search(rf"(?im)^\s*\"?{lib}\s*(?:[=~><]=?\s*([\w.]+))?", py)
        if m:
            return f"{lib}@{m.group(1)}" if m.group(1) else lib
    gomod = files.get("go.mod")
    if gomod:
        m = re.search(r"(?m)^go\s+([\d.]+)", gomod)
        return f"go@{m.group(1)}" if m else "go"
    return ""


def symbol_id(repo: str, sym: Symbol) -> str:
    return f"{repo}#{sym.file_path}#{sym.qualified_name}#{sym.start_line}"


def file_id(repo: str, path: str) -> str:
    return f"{repo}#{path}"


def _header_line(key: str, value: object) -> str:
    text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return f"# {key}: " + " ".join(text.split())


def render_symbol_content(meta: dict[str, Any], body: str) -> str:
    """Header (name, type, enrichment fields, file) + code. Re-rendered after enrichment."""
    lines = [f"# {meta.get('symbol_name')} ({meta.get('symbol_type')})"]
    for key, label in (("intent", "Intent"), ("pattern", "Pattern"), ("tags", "Tags"), ("docstring", "Doc")):
        if meta.get(key):
            lines.append(_header_line(label, meta[key]))
    lines.append(f"# File: {meta.get('file_path')}:{meta.get('start_line')}-{meta.get('end_line')}")
    return ("\n".join(lines) + "\n\n" + body)[:MAX_CHUNK_CHARS]


def chunk_body(content: str) -> str:
    """Code part of a rendered chunk (everything after the ``# ...`` header block)."""
    return content.split("\n\n", 1)[1] if "\n\n" in content else content


class _Resolver:
    """Maps import specifiers and call names to repository files / symbols."""

    def __init__(self, repo: str, parsed: dict[str, ParsedFile], files: dict[str, str]) -> None:
        self.repo = repo
        self.parsed = parsed
        self.paths = set(files) | set(parsed)
        self.alias_map = self._ts_paths(files)
        self.go_module = self._go_module(files.get("go.mod", ""))
        self.by_file: dict[str, dict[str, Symbol]] = defaultdict(dict)
        self.by_name: dict[str, list[Symbol]] = defaultdict(list)
        for pf in parsed.values():
            for s in pf.symbols:
                if s.parent is None:
                    self.by_file[pf.path].setdefault(s.name, s)
                    self.by_name[s.name].append(s)

    @staticmethod
    def _ts_paths(files: dict[str, str]) -> list[tuple[str, list[str]]]:
        raw = files.get("tsconfig.json") or files.get("jsconfig.json")
        mapping: list[tuple[str, list[str]]] = []
        if raw:
            try:
                cleaned = re.sub(r"(?m)^\s*//.*$", "", raw)
                cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
                opts = json.loads(cleaned).get("compilerOptions", {})
                base = opts.get("baseUrl", ".")
                for alias, targets in (opts.get("paths") or {}).items():
                    mapping.append((alias, [posixpath.normpath(posixpath.join(base, t)) for t in targets]))
            except (ValueError, AttributeError):
                pass
        if not any(a == "@/*" for a, _ in mapping):
            mapping.append(("@/*", ["src/*", "*"]))
        if not any(a == "~/*" for a, _ in mapping):
            mapping.append(("~/*", ["src/*"]))
        return mapping

    @staticmethod
    def _go_module(gomod: str) -> str:
        m = re.search(r"(?m)^module\s+(\S+)", gomod)
        return m.group(1) if m else ""

    def _try_ts(self, base: str) -> str | None:
        for suffix in TS_RESOLVE_SUFFIXES:
            cand = posixpath.normpath(base + suffix)
            if cand in self.paths:
                return cand
        return None

    def resolve_import(self, from_file: str, source: str, language: str) -> str | list[str] | None:
        """Repo-relative file path (list of files for Go packages) or None for external modules."""
        if language in ("typescript", "tsx", "javascript"):
            if source.startswith("."):
                return self._try_ts(posixpath.join(posixpath.dirname(from_file), source))
            for alias, targets in self.alias_map:
                prefix = alias.rstrip("*")
                if (alias.endswith("*") and source.startswith(prefix)) or source == alias:
                    rest = source[len(prefix) :] if alias.endswith("*") else ""
                    for t in targets:
                        hit = self._try_ts(t.replace("*", rest) if "*" in t else t)
                        if hit:
                            return hit
            return None
        if language == "python":
            if source.startswith("."):
                level = len(source) - len(source.lstrip("."))
                base = posixpath.dirname(from_file)
                for _ in range(level - 1):
                    base = posixpath.dirname(base)
                mod = source.lstrip(".").replace(".", "/")
                stem = posixpath.join(base, mod) if mod else base
            else:
                stem = source.replace(".", "/")
            for cand in (f"{stem}.py", f"{stem}/__init__.py", f"src/{stem}.py", f"src/{stem}/__init__.py"):
                if posixpath.normpath(cand) in self.paths:
                    return posixpath.normpath(cand)
            return None
        if language == "go" and self.go_module and source.startswith(self.go_module):
            pkg_dir = source[len(self.go_module) :].lstrip("/")
            files = sorted(p for p in self.parsed if posixpath.dirname(p) == pkg_dir and p.endswith(".go"))
            return files or None
        return None

    def module_name(self, source: str, language: str = "") -> str:
        if language in ("go", "python"):
            return source.split(".")[0] if language == "python" else source
        parts = source.split("/")
        if source.startswith("@") and len(parts) > 1:
            return "/".join(parts[:2])
        return parts[0]

    def resolve_call(self, pf: ParsedFile, caller: Symbol, name: str, imported: dict[str, str]) -> Symbol | None:
        if name == caller.name and caller.parent is None:
            return None  # recursion adds no information
        # 1. method of the same class / symbol in the same file
        if caller.parent:
            for s in pf.symbols:
                if s.parent == caller.parent and s.name == name and s is not caller:
                    return s
        local = self.by_file[pf.path].get(name)
        if local is not None and local is not caller:
            return local
        # 2. imported name -> symbol in target file
        target = imported.get(name)
        if target is not None:
            hit = self.by_file.get(target, {}).get(name)
            if hit is not None:
                return hit
            # default import: pick the default-exported symbol of the file
            exported = [s for s in self.by_file.get(target, {}).values() if s.exported]
            if len(exported) == 1:
                return exported[0]
        # 3. Go: same package (same directory)
        if pf.language == "go":
            for s in self.by_name.get(name, []):
                if posixpath.dirname(s.file_path) == posixpath.dirname(pf.path):
                    return s
        # 4. unique exported symbol elsewhere
        if name in _AMBIGUOUS_CALLS or len(name) < 4:
            return None
        cands = [s for s in self.by_name.get(name, []) if s.exported and _same_family(s.file_path, pf.path)]
        return cands[0] if len(cands) == 1 else None


def _family(path: str) -> str:
    ext = PurePosixPath(path).suffix.lower()
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"):
        return "js"
    return ext


def _same_family(a: str, b: str) -> bool:
    return _family(a) == _family(b)


def split_markdown(content: str) -> list[tuple[str, int, str]]:
    """``[(header_path, start_line, text)]`` — sections split by ATX headings, ignoring code fences."""
    sections: list[tuple[str, int, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    in_fence = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append((" > ".join(h for _, h in stack), start, text))

    for i, line in enumerate(content.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
            buf = [line]
            start = i
        else:
            buf.append(line)
    flush()
    return sections


class CodeChunker:
    def __init__(self, repo: RepoInfo, max_chunk_chars: int = MAX_CHUNK_CHARS) -> None:
        self.repo = repo
        self.max_chars = max_chunk_chars

    def _base_meta(self, path: str, language: str, chunk_type: ChunkType) -> dict[str, Any]:
        return {
            "repo": self.repo.name,
            "repo_url": self.repo.url,
            "branch": self.repo.branch,
            "file_path": path,
            "language": language,
            "framework_version": self.repo.framework_version,
            "chunk_type": chunk_type.value,
        }

    def build(self, parsed_files: list[ParsedFile], files: dict[str, str]) -> ChunkingResult:
        """``parsed_files``: source files; ``files``: all text files of the repo (path -> content)."""
        repo = self.repo.name
        res = ChunkingResult()
        self._module_ids: set[str] = set()
        parsed = {pf.path: pf for pf in parsed_files}
        resolver = _Resolver(repo, parsed, files)
        repo_node = f"repo:{repo}"
        res.nodes.append(
            GraphNode(
                id=repo_node,
                label="Repo",
                name=repo,
                repo=repo,
                props={
                    "url": self.repo.url,
                    "branch": self.repo.branch,
                    "framework_version": self.repo.framework_version,
                },
            )
        )

        for pf in parsed_files:
            fid = file_id(repo, pf.path)
            res.nodes.append(
                GraphNode(
                    id=fid,
                    label="File",
                    name=PurePosixPath(pf.path).name,
                    repo=repo,
                    file_path=pf.path,
                    props={"language": pf.language, "size": len(pf.content), "hash": pf.content_hash},
                )
            )
            res.edges.append(GraphEdge(src=repo_node, rel="CONTAINS", dst=fid))
            for sym in pf.symbols:
                sid = symbol_id(repo, sym)
                res.chunks.append(self._symbol_chunk(pf, sym, sid))
                res.nodes.append(
                    GraphNode(
                        id=sid,
                        label="Symbol",
                        name=sym.name,
                        repo=repo,
                        file_path=sym.file_path,
                        props={
                            "type": sym.type.value,
                            "signature": sym.signature,
                            "start_line": sym.start_line,
                            "end_line": sym.end_line,
                            "parent": sym.parent or "",
                            "exported": sym.exported,
                        },
                    )
                )
                res.edges.append(GraphEdge(src=fid, rel="CONTAINS", dst=sid))
            res.chunks.append(self._file_chunk(pf))
            res.edges.extend(self._file_edges(pf, resolver, res))

        for path, content in files.items():
            if path in parsed:
                continue
            ctype = config_type(path)
            if ctype:
                res.chunks.extend(self._config_chunks(path, content, ctype))
                cid = f"config:{repo}:{path}"
                res.nodes.append(
                    GraphNode(
                        id=cid,
                        label="Config",
                        name=PurePosixPath(path).name,
                        repo=repo,
                        file_path=path,
                        props={"type": ctype},
                    )
                )
                res.edges.append(GraphEdge(src=repo_node, rel="CONTAINS", dst=cid))
            elif is_doc_file(path):
                res.chunks.extend(self._doc_chunks(path, content))
            else:
                res.chunks.append(self._plain_file_chunk(path, content))
        # parsed files without symbols of a known language still get their summary chunk above
        return res

    # --- chunks ------------------------------------------------------------------
    def _symbol_chunk(self, pf: ParsedFile, sym: Symbol, sid: str) -> RetrievalChunk:
        meta = self._base_meta(pf.path, pf.language, ChunkType.SYMBOL)
        meta.update(
            {
                "symbol_name": sym.name,
                "qualified_name": sym.qualified_name,
                "symbol_type": sym.type.value,
                "signature": sym.signature,
                "docstring": (sym.docstring or "")[:500],
                "parent": sym.parent or "",
                "decorators": sym.decorators,
                "exported": sym.exported,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "calls": sym.calls[:50],
                "tags": [],
                "pattern": "",
                "intent": "",
            }
        )
        return RetrievalChunk(id=sid, content=render_symbol_content(meta, sym.code), metadata=meta)

    def _file_chunk(self, pf: ParsedFile) -> RetrievalChunk:
        meta = self._base_meta(pf.path, pf.language, ChunkType.FILE_SUMMARY)
        exports = [s.name for s in pf.symbols if s.exported and s.parent is None]
        imports = [i.source for i in pf.imports]
        head = "\n".join(pf.content.splitlines()[:FILE_HEAD_LINES])
        lines = [f"# File: {pf.path} ({pf.language})"]
        if exports:
            lines.append(_header_line("Exports", exports))
        if imports:
            lines.append(_header_line("Imports", imports))
        if not pf.symbols:
            head = pf.content
        meta.update(
            {
                "symbol_name": PurePosixPath(pf.path).name,
                "symbol_type": SymbolType.FILE.value,
                "symbols": exports,
                "imports": imports,
                "tags": [],
            }
        )
        return RetrievalChunk(
            id=f"{self.repo.name}#{pf.path}#__file__",
            content=("\n".join(lines) + "\n\n" + head)[: self.max_chars],
            metadata=meta,
        )

    def _plain_file_chunk(self, path: str, content: str) -> RetrievalChunk:
        lang = PurePosixPath(path).suffix.lstrip(".").lower() or "text"
        meta = self._base_meta(path, lang, ChunkType.FILE_SUMMARY)
        meta.update({"symbol_name": PurePosixPath(path).name, "symbol_type": SymbolType.FILE.value, "tags": []})
        return RetrievalChunk(
            id=f"{self.repo.name}#{path}#__file__",
            content=(f"# File: {path}\n\n{content}")[: self.max_chars],
            metadata=meta,
        )

    def _config_chunks(self, path: str, content: str, ctype: str) -> list[RetrievalChunk]:
        out = []
        body_limit = self.max_chars - 200
        parts = [content[i : i + body_limit] for i in range(0, max(len(content), 1), body_limit)] or [""]
        for n, part in enumerate(parts):
            meta = self._base_meta(path, ctype, ChunkType.CONFIG)
            meta.update(
                {
                    "symbol_name": PurePosixPath(path).name,
                    "symbol_type": "config",
                    "config_type": ctype,
                    "part": n,
                    "tags": [ctype],
                }
            )
            suffix = f" (part {n + 1}/{len(parts)})" if len(parts) > 1 else ""
            out.append(
                RetrievalChunk(
                    id=f"{self.repo.name}#{path}#__config__#{n}",
                    content=f"# Config: {path} [{ctype}]{suffix}\n\n{part}",
                    metadata=meta,
                )
            )
        return out

    def _doc_chunks(self, path: str, content: str) -> list[RetrievalChunk]:
        out = []
        for header_path, start, text in split_markdown(content) or [("", 1, content)]:
            body_limit = self.max_chars - 200
            for n in range(0, max(len(text), 1), body_limit):
                meta = self._base_meta(path, "markdown", ChunkType.DOC)
                meta.update(
                    {
                        "symbol_name": header_path or PurePosixPath(path).name,
                        "symbol_type": "doc",
                        "header_path": header_path,
                        "start_line": start,
                        "tags": [],
                    }
                )
                out.append(
                    RetrievalChunk(
                        id=f"{self.repo.name}#{path}#{header_path}#{start}#{n}",
                        content=f"# Doc: {path} — {header_path}\n\n{text[n : n + body_limit]}",
                        metadata=meta,
                    )
                )
        return out

    # --- graph -------------------------------------------------------------------
    def _file_edges(self, pf: ParsedFile, resolver: _Resolver, res: ChunkingResult) -> list[GraphEdge]:
        repo = self.repo.name
        fid = file_id(repo, pf.path)
        edges: list[GraphEdge] = []
        imported: dict[str, str] = {}  # local name -> target file
        for imp in pf.imports:
            target = resolver.resolve_import(pf.path, imp.source, pf.language)
            if target is None:
                mod = resolver.module_name(imp.source, pf.language)
                mid = f"module:{mod}"
                if mid not in self._module_ids:
                    self._module_ids.add(mid)
                    res.nodes.append(GraphNode(id=mid, label="Module", name=mod, repo="", props={}))
                edges.append(
                    GraphEdge(src=fid, rel="IMPORTS_FILE", dst=mid, props={"specifier": imp.source, "line": imp.line})
                )
                continue
            targets = target if isinstance(target, list) else [target]
            for t in targets:
                edges.append(
                    GraphEdge(
                        src=fid,
                        rel="IMPORTS_FILE",
                        dst=file_id(repo, t),
                        props={"specifier": imp.source, "line": imp.line},
                    )
                )
            if isinstance(target, str):
                for name in imp.names:
                    if name == "*":
                        continue
                    imported[name] = target
                    sym = resolver.by_file.get(target, {}).get(name)
                    if sym is not None:
                        edges.append(
                            GraphEdge(
                                src=fid,
                                rel="IMPORTS",
                                dst=symbol_id(repo, sym),
                                props={
                                    "import_type": "default" if imp.is_default and name == imp.names[0] else "named"
                                },
                            )
                        )
        for sym in pf.symbols:
            sid = symbol_id(repo, sym)
            if sym.parent:
                parent = next((s for s in pf.symbols if s.name == sym.parent and s.parent is None), None)
                if parent is not None:
                    edges.append(GraphEdge(src=symbol_id(repo, parent), rel="CONTAINS", dst=sid))
            for call in sym.calls:
                target_sym = resolver.resolve_call(pf, sym, call, imported)
                if target_sym is not None:
                    edges.append(
                        GraphEdge(src=sid, rel="CALLS", dst=symbol_id(repo, target_sym), props={"call_type": "static"})
                    )
            for rel, names in (("INHERITS", sym.bases), ("IMPLEMENTS", sym.implements)):
                for name in names:
                    target_sym = resolver.resolve_call(pf, sym, name.split("<")[0].split(".")[-1], imported)
                    if target_sym is not None:
                        edges.append(GraphEdge(src=sid, rel=rel, dst=symbol_id(repo, target_sym)))
        return edges
