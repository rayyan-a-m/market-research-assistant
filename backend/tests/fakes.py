"""Fake providers for offline pipeline tests.

Because the pipeline codes to the ChatProvider / EmbeddingProvider interfaces
(and gets them via DI), we can inject deterministic fakes and exercise the
real orchestration/retrieval/judge logic with zero network — the same seam
that lets us swap Gemini for GitHub Models lets us swap in a stub for tests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from app.providers.base import ChatProvider, ChatResult, EmbeddingProvider, Message


class FakeChatProvider(ChatProvider):
    """Returns a canned parsed object; records how many times it was called."""

    def __init__(self, parsed: Any = None, text: str = "") -> None:
        self.parsed = parsed
        self.text = text
        self.calls = 0
        self.last_system: str | None = None
        self.last_messages: list[Message] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[Any] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[Any]:
        self.calls += 1
        self.last_system = system
        self.last_messages = messages
        return ChatResult(text=self.text, parsed=self.parsed, model="fake")


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, content-derived vectors — same text always maps to the
    same vector, different text to a different one."""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode()).digest()
            out.append([b / 255.0 for b in digest[: self.dim]])
        return out


class FixedEmbeddingProvider(EmbeddingProvider):
    """Always returns the same vector — lets a test control cosine similarity
    against a hand-built SourceIndex (used to exercise the judge's gate)."""

    def __init__(self, vector: Sequence[float]) -> None:
        self.vector = [float(x) for x in vector]
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]
