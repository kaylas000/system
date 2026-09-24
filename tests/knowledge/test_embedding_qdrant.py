from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("qdrant_client")

from kernel.knowledge.ingestion.embedding import (
    HashingEmbedder,
    LiteLLMEmbedder,
    build_embedder,
    sparse_doc_vector,
    sparse_query_vector,
    tokenize,
)
from kernel.knowledge.models import RetrievalChunk
from kernel.knowledge.storage.qdrant_store import QdrantKB, path_prefixes, point_id


def test_tokenize_splits_identifiers():
    toks = tokenize("export async function renderToReadableStream(user_id) {}")
    assert "rendertoreadablestream" in toks
    assert {"render", "readable", "stream", "user", "id"} <= set(toks)
    assert "function" not in toks


async def test_hashing_embedder_is_deterministic_and_normalised():
    emb = HashingEmbedder(64)
    a, b, c = await emb.embed(["stream response", "stream response", "database user query"])
    assert a == b
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    dot_same = sum(x * y for x, y in zip(a, b, strict=True))
    dot_other = sum(x * y for x, y in zip(a, c, strict=True))
    assert dot_same > dot_other


def test_sparse_vectors():
    idx, vals = sparse_doc_vector("stream stream stream user")
    assert len(idx) == 2 and idx == sorted(idx)
    assert max(vals) < 2.2  # BM25 saturation k1=1.2
    q_idx, q_vals = sparse_query_vector("stream")
    assert q_vals == [1.0] and q_idx[0] in idx


async def test_litellm_embedder_batches_and_passes_dimensions(monkeypatch):
    calls = []

    async def aembedding(**kwargs):
        calls.append(kwargs)
        data = [{"index": i, "embedding": [float(i)] * 8} for i in range(len(kwargs["input"]))]
        return types.SimpleNamespace(data=list(reversed(data)))

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(aembedding=aembedding))
    emb = LiteLLMEmbedder(model="text-embedding-3-small", dim=8, batch_size=2)
    vecs = await emb.embed(["a", "b", "c"])
    assert [v[0] for v in vecs] == [0.0, 1.0, 0.0]  # sorted by index within each batch
    assert len(calls) == 2 and calls[0]["dimensions"] == 8

    wrong = LiteLLMEmbedder(model="text-embedding-3-small", dim=16)
    with pytest.raises(ValueError, match="dim"):
        await wrong.embed(["x"])
    with pytest.raises(ValueError):
        build_embedder("nope", "m", 8)


def test_ids_and_prefixes():
    assert point_id("r#a.ts#f#1") == point_id("r#a.ts#f#1")
    assert len(point_id("x")) == 36
    assert path_prefixes("src/app/page.tsx") == ["src", "src/app"]


def _chunk(cid, content, **meta):
    base = {"repo": "r", "chunk_type": "symbol", "language": "typescript", "file_path": "src/a.ts"}
    base.update(meta)
    return RetrievalChunk(id=cid, content=content, metadata=base)


async def test_qdrant_hybrid_search_filters_and_delete():
    emb = HashingEmbedder(64)
    kb = QdrantKB(location=":memory:", collection="t", dim=64)
    chunks = [
        _chunk(
            "r#src/lib/stream.ts#streamResponse#1",
            "streamResponse renderToReadableStream SSR streaming",
            file_path="src/lib/stream.ts",
            file_hash="h1",
        ),
        _chunk(
            "r#src/server/users.ts#getUser#1",
            "getUser loads user from database prisma",
            file_path="src/server/users.ts",
            file_hash="h2",
        ),
        _chunk(
            "r#scripts/seed.py#main#1",
            "seed users json",
            file_path="scripts/seed.py",
            language="python",
            file_hash="h3",
        ),
        _chunk("other#x.ts#y#1", "renderToReadableStream in another repo", repo="other"),
    ]
    assert await kb.upsert(chunks, await emb.embed([c.content for c in chunks])) == 4
    assert await kb.count() == 4

    q = "how is renderToReadableStream used for streaming"
    hits = await kb.hybrid_search((await emb.embed([q]))[0], q, filters={"repo": "r"}, limit=3)
    assert hits[0].id == "r#src/lib/stream.ts#streamResponse#1"
    assert all(h.payload["repo"] == "r" for h in hits)
    assert hits[0].content.startswith("streamResponse")

    sparse_only = await kb.hybrid_search(None, "prisma database", filters={"repo": "r"}, limit=1)
    assert sparse_only[0].id == "r#src/server/users.ts#getUser#1"

    ts_only = await kb.hybrid_search(None, "users", filters={"repo": "r", "language": ["typescript", "tsx"]})
    assert {h.id for h in ts_only} == {"r#src/server/users.ts#getUser#1"}
    in_src = await kb.hybrid_search(None, "users seed stream", filters={"path_prefix": "src/server"})
    assert {h.id for h in in_src} == {"r#src/server/users.ts#getUser#1"}
    excluded = await kb.hybrid_search(None, "users seed", filters={"repo": "r", "exclude": {"language": "python"}})
    assert "r#scripts/seed.py#main#1" not in {h.id for h in excluded}

    assert await kb.file_hashes("r") == {
        "src/lib/stream.ts": "h1",
        "src/server/users.ts": "h2",
        "scripts/seed.py": "h3",
    }
    got = await kb.get(["r#scripts/seed.py#main#1"])
    assert got[0].payload["language"] == "python"

    await kb.delete("r", ["scripts/seed.py"])
    assert await kb.count({"repo": "r"}) == 2
    await kb.delete("r", [])  # no-op
    await kb.delete("r")
    assert await kb.count() == 1
    await kb.close()


async def test_qdrant_dim_mismatch_is_detected(tmp_path):
    kb = QdrantKB(path=str(tmp_path / "q"), collection="c", dim=8)
    await kb.ensure_collection()
    await kb.close()
    kb2 = QdrantKB(path=str(tmp_path / "q"), collection="c", dim=16)
    with pytest.raises(ValueError, match="dense size"):
        await kb2.ensure_collection()
    await kb2.close()
