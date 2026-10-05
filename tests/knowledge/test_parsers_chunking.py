from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter")

from kernel.knowledge.ingestion.chunking import (
    CodeChunker,
    RepoInfo,
    chunk_body,
    config_type,
    detect_framework_version,
    split_markdown,
)
from kernel.knowledge.ingestion.parsers import MultiLanguageParser, detect_language
from kernel.knowledge.models import SymbolType


@pytest.fixture(scope="module")
def parser() -> MultiLanguageParser:
    return MultiLanguageParser()


def _by_name(pf):
    return {s.qualified_name: s for s in pf.symbols}


def test_detect_language():
    assert detect_language("a/b.tsx") == "tsx"
    assert detect_language("x.py") == "python"
    assert detect_language("main.go") == "go"
    assert detect_language("README.md") is None


def test_typescript_symbols_imports_calls(parser, sample_files):
    pf = parser.parse_file("src/server/users.ts", sample_files["src/server/users.ts"])
    syms = _by_name(pf)
    assert syms["getUser"].type == SymbolType.FUNCTION
    assert syms["getUser"].exported
    assert "Loads a single user" in (syms["getUser"].docstring or "")
    assert syms["getUser"].start_line == 13
    assert syms["UserDTO"].type == SymbolType.INTERFACE
    assert "getUser" in syms["updateUserName"].calls
    assert [(i.source, i.names) for i in pf.imports] == [("./db", ["db"])]


def test_tsx_components_hooks_classes(parser, sample_files):
    page = _by_name(parser.parse_file("src/app/profile/page.tsx", sample_files["src/app/profile/page.tsx"]))
    assert page["ProfilePage"].type == SymbolType.COMPONENT
    assert page["useProfileTab"].type == SymbolType.HOOK
    assert {"getUser", "Avatar"} <= set(page["ProfilePage"].calls)

    repo = _by_name(parser.parse_file("src/lib/repo.ts", sample_files["src/lib/repo.ts"]))
    assert repo["UserRepo"].bases == ["BaseRepo"]
    assert repo["UserRepo"].implements == ["IRepo"]
    assert repo["UserRepo.find"].type == SymbolType.METHOD
    assert repo["UserRepo.find"].parent == "UserRepo"
    assert repo["Role"].type == SymbolType.ENUM
    assert repo["MAX_PAGE_SIZE"].type == SymbolType.CONSTANT


def test_python_and_go(parser, sample_files):
    py = parser.parse_file("scripts/seed.py", sample_files["scripts/seed.py"])
    syms = _by_name(py)
    assert syms["Seeder"].docstring == "Writes fixture users to a JSON file."
    assert syms["Seeder.build"].decorators == ["@staticmethod"]
    assert syms["Seeder.build"].start_line == 15  # decorator included
    assert "build" in syms["Seeder.run"].calls
    assert syms["DEFAULT_USERS"].type == SymbolType.CONSTANT
    assert ("pathlib", ["Path"]) in [(i.source, i.names) for i in py.imports]

    go = _by_name(parser.parse_file("cmd/app/main.go", sample_files["cmd/app/main.go"]))
    assert go["Server"].type == SymbolType.STRUCT
    assert go["Server.Health"].type == SymbolType.METHOD
    assert go["Server.Health"].docstring == "Health reports liveness."
    assert "ListenAndServe" in go["main"].calls


def test_broken_and_unknown_files_do_not_raise(parser):
    pf = parser.parse_file("src/broken.ts", "export function (((( {")
    assert pf.language == "typescript"
    assert parser.parse_file("x.rs", "fn main() {}").symbols == []


def test_split_markdown_ignores_code_fences():
    md = "# A\ntext\n```bash\n# not a heading\n```\n## B\nmore\n"
    sections = split_markdown(md)
    assert [s[0] for s in sections] == ["A", "A > B"]
    assert "# not a heading" in sections[0][2]


def test_config_and_framework_detection(sample_files):
    assert config_type("prisma/schema.prisma") == "prisma"
    assert config_type(".github/workflows/ci.yml") == "ci_workflow"
    assert config_type("Dockerfile.prod") == "dockerfile"
    assert config_type("src/a.ts") is None
    assert detect_framework_version(sample_files) == "nextjs@14.2.35"
    assert (
        detect_framework_version({"pyproject.toml": 'dependencies = [\n  "fastapi>=0.115.0",\n]'}) == "fastapi@0.115.0"
    )


def test_chunker_builds_chunks_and_resolved_graph(parser, sample_files):
    parsed = [parser.parse_file(p, c) for p, c in sample_files.items() if parser.supports(p)]
    res = CodeChunker(RepoInfo("sample", framework_version="nextjs@14.2.35")).build(parsed, sample_files)
    ids = {c.id for c in res.chunks}
    assert "sample#src/server/users.ts#getUser#13" in ids
    assert "sample#src/server/users.ts#__file__" in ids
    assert "sample#package.json#__config__#0" in ids
    assert any(c.chunk_type == "doc" and c.metadata["header_path"] == "Streaming > How it works" for c in res.chunks)

    chunk = next(c for c in res.chunks if c.id == "sample#src/lib/stream.ts#streamResponse#8")
    assert chunk.metadata["symbol_type"] == "function"
    assert chunk.metadata["framework_version"] == "nextjs@14.2.35"
    assert chunk_body(chunk.content).startswith("/**") or chunk_body(chunk.content).startswith("export")
    assert "renderToReadableStream" in chunk.content

    def edges(rel):
        return {(e.src, e.dst) for e in res.edges if e.rel == rel}

    get_user = "sample#src/server/users.ts#getUser#13"
    callers = {src for src, dst in edges("CALLS") if dst == get_user}
    assert callers == {
        "sample#src/server/users.ts#updateUserName#18",
        "sample#src/app/api/users/route.ts#GET#4",
        "sample#src/app/profile/page.tsx#ProfilePage#11",
        "sample#src/lib/repo.ts#UserRepo.find#14",
    }
    assert ("sample#src/app/api/users/route.ts", get_user) in edges("IMPORTS")  # "@/" alias resolved
    assert ("sample#src/server/users.ts", "sample#src/server/db.ts") in edges("IMPORTS_FILE")  # relative import
    assert ("sample#src/lib/stream.ts", "module:react-dom") in edges("IMPORTS_FILE")
    assert ("sample#src/lib/repo.ts#UserRepo#13", "sample#src/lib/repo.ts#BaseRepo#8") in edges("INHERITS")
    assert ("sample#src/lib/repo.ts#UserRepo#13", "sample#src/lib/repo.ts#IRepo#4") in edges("IMPLEMENTS")
    assert ("sample#src/lib/repo.ts#UserRepo#13", "sample#src/lib/repo.ts#UserRepo.find#14") in edges("CONTAINS")
    node_ids = {n.id for n in res.nodes}
    for e in res.edges:
        assert e.src in node_ids and e.dst in node_ids, e
