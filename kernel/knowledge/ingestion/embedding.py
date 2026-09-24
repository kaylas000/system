"""
Embeddings (``specs/MISSING_FILES.md`` #7 ``EMBEDDING.py`` — only the name was given; written by the agent).

* ``LiteLLMEmbedder`` — production: ``litellm.aembedding`` (OpenAI ``text-embedding-3-small`` with
  ``dimensions=768`` by default, or any provider / LiteLLM proxy model).
* ``HashingEmbedder`` — deterministic feature-hashing embedder: no network, no model download.
  Used in tests and as an offline fallback (quality is lexical, not semantic).
* ``sparse_doc_vector`` / ``sparse_query_vector`` — client-side BM25 term weights for Qdrant
  sparse vectors (IDF is applied server-side via ``Modifier.IDF``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from collections import Counter
from itertools import pairwise
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for", "on", "with", "as", "by", "be", "this",
    "that", "are", "at", "from", "if", "return", "const", "let", "var", "import", "export", "function", "def",
    "self", "new", "async", "await", "string", "number", "null", "none", "true", "false",
}  # fmt: skip

SPARSE_INDEX_SPACE = 2**31 - 1  # Qdrant sparse indices are uint32


def _norm(token: str) -> str:
    """Tiny plural folding (``users`` -> ``user``, ``queries`` -> ``query``) — no full stemmer needed for code."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Code-aware tokens: ``renderToReadableStream`` -> [renderToReadableStream, render, to, readable, stream]."""
    out: list[str] = []
    for word in _WORD_RE.findall(text):
        lower = word.lower()
        parts = [p.lower() for piece in word.split("_") for p in _CAMEL_RE.findall(piece)]
        if len(parts) > 1:
            if lower not in _STOPWORDS:
                out.append(lower)
            out.extend(p for p in parts if len(p) > 1 and p not in _STOPWORDS)
        elif len(lower) > 1 and lower not in _STOPWORDS:
            out.append(lower)
    return [_norm(t) for t in out]


def _hash(token: str, salt: str = "") -> int:
    return int.from_bytes(hashlib.blake2b((salt + token).encode("utf-8"), digest_size=8).digest(), "big")


def sparse_doc_vector(text: str, k1: float = 1.2) -> tuple[list[int], list[float]]:
    """BM25 term-frequency part (``b=0``): ``tf*(k1+1)/(tf+k1)``. Indices are hashed tokens."""
    counts = Counter(_hash(t) % SPARSE_INDEX_SPACE for t in tokenize(text))
    indices = sorted(counts)
    return indices, [counts[i] * (k1 + 1) / (counts[i] + k1) for i in indices]


def sparse_query_vector(text: str) -> tuple[list[int], list[float]]:
    indices = sorted({_hash(t) % SPARSE_INDEX_SPACE for t in tokenize(text)})
    return indices, [1.0] * len(indices)


@runtime_checkable
class IEmbedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing of tokens + token bigrams, L2-normalised. Deterministic across processes."""

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        feats = tokens + [f"{a} {b}" for a, b in pairwise(tokens)]
        for feat in feats:
            h = _hash(feat, "emb")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class LiteLLMEmbedder:
    """``litellm.aembedding`` with batching and retries."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        dim: int = 768,
        api_base: str | None = None,
        api_key: str | None = None,
        batch_size: int = 64,
        max_retries: int = 3,
        send_dimensions: bool | None = None,
        max_input_chars: int = 24_000,
    ) -> None:
        self.model = model
        self.dim = dim
        self.name = f"litellm:{model}"
        self.api_base = api_base
        self.api_key = api_key
        self.batch_size = batch_size
        self.max_retries = max_retries
        # Only OpenAI text-embedding-3-* (and compatible proxies) accept ``dimensions``.
        self.send_dimensions = ("text-embedding-3" in model) if send_dimensions is None else send_dimensions
        self.max_input_chars = max_input_chars

    async def _call(self, batch: list[str]) -> list[list[float]]:
        import litellm

        kwargs: dict[str, Any] = {"model": self.model, "input": batch}
        if self.send_dimensions:
            kwargs["dimensions"] = self.dim
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = await litellm.aembedding(**kwargs)
                data = sorted(resp.data, key=lambda d: d["index"] if isinstance(d, dict) else d.index)
                vectors = [list(d["embedding"] if isinstance(d, dict) else d.embedding) for d in data]
                if vectors and len(vectors[0]) != self.dim:
                    raise ValueError(f"embedding dim {len(vectors[0])} != configured {self.dim} for {self.model}")
                return vectors
            except ValueError:
                raise
            except Exception as exc:
                if attempt == self.max_retries:
                    raise
                logger.warning("embedding batch failed (%s), retry %d", exc, attempt + 1)
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [t[: self.max_input_chars] or " " for t in texts[i : i + self.batch_size]]
            out.extend(await self._call(batch))
        return out


def build_embedder(
    kind: str, model: str, dim: int, api_base: str | None = None, api_key: str | None = None
) -> IEmbedder:
    if kind == "hashing":
        return HashingEmbedder(dim)
    if kind == "litellm":
        return LiteLLMEmbedder(model=model, dim=dim, api_base=api_base, api_key=api_key)
    raise ValueError(f"unknown embedder: {kind!r} (expected 'litellm' or 'hashing')")
