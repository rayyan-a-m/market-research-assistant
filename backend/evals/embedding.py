"""A deterministic lexical embedder, for the offline evaluation tier only.

`tests/fakes.py` provides hash-derived vectors — perfect for asserting that
plumbing is wired correctly, useless for evaluating a *similarity threshold*,
because the "distance" between two hash vectors is noise. Evaluating the
similarity floor offline needs an embedding where related text really is
closer than unrelated text.

This is a hashed bag-of-words vectorizer: tokens are hashed into a fixed
number of buckets and counted, with sublinear scaling. Shared vocabulary
produces high cosine similarity; disjoint vocabulary produces near-zero. That
is enough to check the floor's *behaviour* — that an off-topic claim scores
below an on-topic one and gets rejected without an LLM call.

What it is not: a stand-in for the production embedding model. It has no
notion of synonymy, so it under-scores a faithful paraphrase. That is exactly
why the floor's real calibration belongs in the `-m eval` tier against the
actual model, and why the offline tier asserts on *ordering and gating*
rather than on absolute score values.
"""

from __future__ import annotations

import hashlib
import math
import re

from app.providers.base import EmbeddingProvider

_TOKEN = re.compile(r"[a-z0-9]+")


class LexicalEmbeddingProvider(EmbeddingProvider):
    def __init__(self, *, dim: int = 512) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        counts: dict[int, float] = {}
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self.dim
            counts[bucket] = counts.get(bucket, 0.0) + 1.0
        # Sublinear term frequency: one word repeated twenty times shouldn't
        # dominate the direction of the vector.
        vector = [0.0] * self.dim
        for bucket, count in counts.items():
            vector[bucket] = 1.0 + math.log(count)
        return vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]
