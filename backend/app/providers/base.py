"""Provider interfaces (the Strategy side of Strategy+Adapter+Factory).

Nothing above this layer — routers, the pipeline, the discovery agent —
imports `langchain_google_genai`, `langchain_openai`, or an HTTP client for
Brave/Serper directly. They depend on these three abstract interfaces
and get a concrete implementation from `ProviderFactory` (factory.py).
Swapping Gemini for a different chat model, or Brave for Serper, is a
registry entry, not a change to business logic.

Each concrete adapter (google_provider.py, openai_provider.py, ...) is
responsible for:
  - translating its vendor SDK's calls into these method signatures
  - enforcing its own timeout
  - translating vendor exceptions into the normalized errors in
    app.core.errors, so callers only ever catch ProviderError subclasses
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ChatResult(Generic[T]):
    text: str
    parsed: T | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    published_at: str | None = None


class ChatProvider(ABC):
    """A chat/completion model, optionally with structured output."""

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[T] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[T]:
        """Raises ProviderTimeoutError, ProviderRateLimitError,
        ProviderUnavailableError, or ProviderResponseError on failure —
        never a raw vendor SDK exception."""
        raise NotImplementedError


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        raise NotImplementedError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    timeout_seconds: float = 30.0
    backoff_base_seconds: float = field(default=1.0)
