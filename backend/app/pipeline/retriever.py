"""Stage 2 — in-memory retrieval (RAG-lite).

Each source is chunked and embedded once (Gemini). Retrieval runs
*per-source*: for every topic we take the top-k chunks from each source
separately, so a claim about one competitor can never be grounded in
another's page. The assembled context orders source blocks
highest-relevance-first / second-highest-last (primacy/recency, "Lost in
the Middle") and repeats the attribution rules at the end.

The per-source chunk embeddings computed here are reused by the judge
(judge.py), so a claim is verified against the same source vectors — no
second embedding pass over the sources.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.config import Settings
from app.core.sanitize import INJECTION_GUARD_SYSTEM_PROMPT_ADDENDUM
from app.pipeline.fetcher import FetchedSource
from app.providers.base import EmbeddingProvider


@dataclass
class SourceIndex:
    url: str
    chunks: list[str]
    embeddings: np.ndarray  # shape (n_chunks, dim), L2-normalized


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Character-window chunking with overlap. Simple and predictable;
    good enough for article-length sources and avoids a tokenizer dep."""
    if not text:
        return []
    step = max(1, size - overlap)
    chunks = [text[i : i + size] for i in range(0, len(text), step)]
    return [c for c in chunks if c.strip()]


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def cosine_topk(query: np.ndarray, matrix: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Top-k (index, score) by cosine similarity. `matrix` is assumed
    L2-normalized; `query` is normalized here."""
    if matrix.shape[0] == 0:
        return []
    q = query / (np.linalg.norm(query) or 1.0)
    scores = matrix @ q
    k = min(k, scores.shape[0])
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(int(i), float(scores[i])) for i in top]


async def build_indexes(
    sources: list[FetchedSource], settings: Settings, embedder: EmbeddingProvider
) -> dict[str, SourceIndex]:
    """Embed every source's chunks in one batched call."""
    per_source_chunks: dict[str, list[str]] = {}
    flat_chunks: list[str] = []
    owner: list[str] = []
    for src in sources:
        chunks = chunk_text(
            src.markdown, size=settings.chunk_size_chars, overlap=settings.chunk_overlap_chars
        )
        per_source_chunks[src.url] = chunks
        flat_chunks.extend(chunks)
        owner.extend([src.url] * len(chunks))

    if not flat_chunks:
        return {}

    vectors = np.array(await embedder.embed(flat_chunks), dtype=np.float32)
    vectors = _normalize(vectors)

    indexes: dict[str, SourceIndex] = {}
    cursor = 0
    for src in sources:
        n = len(per_source_chunks[src.url])
        indexes[src.url] = SourceIndex(
            url=src.url,
            chunks=per_source_chunks[src.url],
            embeddings=vectors[cursor : cursor + n],
        )
        cursor += n
    return indexes


def order_blocks_primacy_recency(scored: list[tuple[str, float, str]]) -> list[tuple[str, str]]:
    """`scored` = (url, max_relevance, block_text). Returns (url, block_text)
    ordered highest-relevance-first, second-highest-last, rest in the
    middle sorted desc — the "Lost in the Middle" mitigation."""
    ranked = sorted(scored, key=lambda t: t[1], reverse=True)
    if len(ranked) <= 2:
        return [(u, b) for u, _, b in ranked]
    first, last, *middle = ranked
    ordered = [first, *middle, last]
    return [(u, b) for u, _, b in ordered]


async def build_context(
    topics: list[str],
    indexes: dict[str, SourceIndex],
    settings: Settings,
    embedder: EmbeddingProvider,
) -> str:
    """Assemble the labelled, ordered context string for the summarizer."""
    if not indexes:
        return ""
    topic_vecs = _normalize(np.array(await embedder.embed(topics), dtype=np.float32))

    scored_blocks: list[tuple[str, float, str]] = []
    for url, index in indexes.items():
        selected: dict[int, float] = {}
        for tvec in topic_vecs:
            for idx, score in cosine_topk(tvec, index.embeddings, settings.retrieval_top_k):
                selected[idx] = max(selected.get(idx, 0.0), score)
        if not selected:
            continue
        ordered_idxs = sorted(selected, key=lambda i: selected[i], reverse=True)
        max_rel = max(selected.values())
        passages = "\n\n".join(index.chunks[i] for i in ordered_idxs)
        block = f"[Source: {url}]\n{passages}"
        scored_blocks.append((url, max_rel, block))

    ordered = order_blocks_primacy_recency(scored_blocks)
    body = "\n\n".join(block for _, block in ordered)

    reminder = (
        "\n\n---\nREMINDER (attribution rules):\n"
        "1. Use only the URL shown in the nearest [Source:] tag as source_url.\n"
        "2. Every claim must cite exactly one source_url from the sources above.\n"
        f"3. {INJECTION_GUARD_SYSTEM_PROMPT_ADDENDUM}"
    )
    return body + reminder
