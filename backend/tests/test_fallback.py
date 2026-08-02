from __future__ import annotations

import pytest

from app.core.errors import ProviderUnavailableError
from app.providers.base import (
    ChatProvider,
    ChatResult,
    Message,
    SearchProvider,
    SearchResult,
)
from app.providers.fallback import FallbackChatProvider, FallbackSearchProvider


class _FakeChatProvider(ChatProvider):
    def __init__(self, *, name: str, should_fail: bool = False) -> None:
        self.name = name
        self.should_fail = should_fail
        self.call_count = 0

    async def complete(self, *, system, messages, response_model=None, max_tokens=4096):  # type: ignore[override]
        self.call_count += 1
        if self.should_fail:
            raise ProviderUnavailableError("simulated outage", provider=self.name)
        return ChatResult(text=f"response from {self.name}", parsed=None, model=self.name)


class _FakeSearchProvider(SearchProvider):
    def __init__(self, *, name: str, should_fail: bool = False) -> None:
        self.name = name
        self.should_fail = should_fail
        self.call_count = 0

    async def search(self, query, *, count=10):  # type: ignore[override]
        self.call_count += 1
        if self.should_fail:
            raise ProviderUnavailableError("simulated outage", provider=self.name)
        return [SearchResult(url="https://example.com", title="t", snippet="s")]


@pytest.mark.asyncio
async def test_uses_primary_when_it_succeeds() -> None:
    primary = _FakeChatProvider(name="primary")
    fallback = _FakeChatProvider(name="fallback")
    provider = FallbackChatProvider(primary=primary, fallback=fallback)

    result = await provider.complete(system="s", messages=[Message(role="user", content="hi")])

    assert result.text == "response from primary"
    assert primary.call_count == 1
    assert fallback.call_count == 0


@pytest.mark.asyncio
async def test_falls_back_when_primary_raises_provider_error() -> None:
    primary = _FakeChatProvider(name="primary", should_fail=True)
    fallback = _FakeChatProvider(name="fallback")
    provider = FallbackChatProvider(primary=primary, fallback=fallback)

    result = await provider.complete(system="s", messages=[Message(role="user", content="hi")])

    assert result.text == "response from fallback"
    assert primary.call_count == 1
    assert fallback.call_count == 1


@pytest.mark.asyncio
async def test_propagates_error_when_both_fail() -> None:
    primary = _FakeChatProvider(name="primary", should_fail=True)
    fallback = _FakeChatProvider(name="fallback", should_fail=True)
    provider = FallbackChatProvider(primary=primary, fallback=fallback)

    with pytest.raises(ProviderUnavailableError):
        await provider.complete(system="s", messages=[Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_search_falls_back_on_provider_error() -> None:
    primary = _FakeSearchProvider(name="primary", should_fail=True)
    fallback = _FakeSearchProvider(name="fallback")
    provider = FallbackSearchProvider(primary=primary, fallback=fallback)

    results = await provider.search("payment orchestration")

    assert len(results) == 1
    assert primary.call_count == 1
    assert fallback.call_count == 1
