"""Decorator providers: compose a primary + fallback of the *same*
interface so callers never know a fallback happened except via logging.

This is deliberately its own abstraction rather than relying solely on
LangChain's `.with_fallbacks()`, which only composes LangChain Runnables and
so can't fall back from, say, Brave Search to Serper — two unrelated REST
APIs behind no shared Runnable base. A
provider-level decorator works uniformly across every ChatProvider and
SearchProvider implementation, vendor SDK or plain HTTP alike, which is
the point of coding to the interface in the first place.

Both stages that use this degrade gracefully for outage/rate-limit
resilience: the summarizer falls back Gemini Pro -> Gemini Flash, and the
judge falls back primary -> fallback within whichever provider is
configured (Gemini by default, or an OpenAI-compatible provider if
OPENAI_API_KEY is set — see ProviderFactory.judge). If the judge provider
fails entirely, the judge stage marks claims unverified and the run
continues rather than crashing.
"""

from __future__ import annotations

import logging

from app.core.errors import ProviderError
from app.providers.base import ChatProvider, ChatResult, Message, SearchProvider, SearchResult, T

logger = logging.getLogger(__name__)


class FallbackChatProvider(ChatProvider):
    def __init__(self, *, primary: ChatProvider, fallback: ChatProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[T] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[T]:
        try:
            return await self._primary.complete(
                system=system, messages=messages, response_model=response_model, max_tokens=max_tokens
            )
        except ProviderError as exc:
            logger.warning(
                "primary chat provider failed (%s: %s), falling back", exc.provider, exc
            )
            return await self._fallback.complete(
                system=system, messages=messages, response_model=response_model, max_tokens=max_tokens
            )


class FallbackSearchProvider(SearchProvider):
    def __init__(self, *, primary: SearchProvider, fallback: SearchProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        try:
            return await self._primary.search(query, count=count)
        except ProviderError as exc:
            logger.warning(
                "primary search provider failed (%s: %s), falling back", exc.provider, exc
            )
            return await self._fallback.search(query, count=count)
