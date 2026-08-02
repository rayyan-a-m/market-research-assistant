"""SearchProvider adapters for source discovery. Both wrap a plain REST
API over httpx rather than a vendor SDK — there isn't one — but they
implement the same interface for the same reason: the discovery agent's
tool calls `SearchProvider.search()` and never knows which API answered."""

from __future__ import annotations

import httpx

from app.core.errors import (
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.base import SearchProvider, SearchResult


class BraveSearchProvider(SearchProvider):
    _ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, *, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self._api_key}
        params: dict[str, str | int] = {"q": query, "count": count}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._ENDPOINT, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc), provider="brave_search", cause=exc) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc), provider="brave_search", cause=exc) from exc

        if response.status_code == 429:
            raise ProviderRateLimitError("Brave Search rate limited", provider="brave_search")
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"Brave Search returned {response.status_code}", provider="brave_search"
            )
        response.raise_for_status()

        data = response.json()
        results = data.get("web", {}).get("results", [])
        return [
            SearchResult(
                url=item["url"],
                title=item.get("title", ""),
                snippet=item.get("description", ""),
                published_at=item.get("page_age"),
            )
            for item in results
        ]


class SerperSearchProvider(SearchProvider):
    """Fallback search provider — used when Brave is unavailable or over
    its rate limit (see providers/fallback.py's FallbackSearchProvider)."""

    _ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, *, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        headers = {"X-API-KEY": self._api_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": count}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._ENDPOINT, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc), provider="serper", cause=exc) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc), provider="serper", cause=exc) from exc

        if response.status_code == 429:
            raise ProviderRateLimitError("Serper rate limited", provider="serper")
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"Serper returned {response.status_code}", provider="serper"
            )
        response.raise_for_status()

        data = response.json()
        results = data.get("organic", [])
        return [
            SearchResult(
                url=item["link"],
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                published_at=item.get("date"),
            )
            for item in results
        ]
