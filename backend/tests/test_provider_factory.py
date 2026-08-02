"""Construction-only tests: these verify the registry resolves the right
adapter types and that the interface contract holds. They deliberately
never call `.complete()`/`.embed()`/`.search()` — that would require real
API keys and network access, which CI doesn't have. Adapter *behavior*
against a live API is a manual/integration concern, not a unit-test one.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.base import ChatProvider, EmbeddingProvider, SearchProvider
from app.providers.factory import ProviderFactory
from app.providers.fallback import FallbackChatProvider, FallbackSearchProvider
from app.providers.google_provider import GoogleChatProvider, GoogleEmbeddingProvider
from app.providers.middleware import (
    BudgetGuardChatProvider,
    CachingEmbeddingProvider,
    CircuitBreakerChatProvider,
    MeteredChatProvider,
    RetryingEmbeddingProvider,
    RunLedger,
)
from app.providers.openai_provider import OpenAIChatProvider
from app.providers.search_providers import SerperSearchProvider


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        GEMINI_API_KEY="test-gemini-key",
        BRAVE_SEARCH_API_KEY="test-brave-key",
        SERPER_API_KEY="test-serper-key",
    )


def test_unknown_chat_vendor_raises(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown chat provider vendor"):
        ProviderFactory.create_chat(vendor="does-not-exist", model="x", settings=settings)


def test_unknown_embedding_vendor_raises(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown embedding provider vendor"):
        ProviderFactory.create_embedding(vendor="does-not-exist", model="x", settings=settings)


def test_summarizer_composes_the_full_middleware_stack(settings: Settings) -> None:
    """The layer ORDER is the design decision, so it's what the test pins.

    Metered outermost (sees whatever finally answered, including a fallback),
    then BudgetGuard (refuses before spending, and outside the fallback so a
    stopped run doesn't burn one last call), then Fallback, with a breaker
    around EACH concrete adapter — inside the fallback, so an open primary
    reroutes to the fallback instead of blocking it.
    """
    provider = ProviderFactory.summarizer(settings, RunLedger())
    assert isinstance(provider, ChatProvider)

    assert isinstance(provider, MeteredChatProvider)
    budget = provider._inner
    assert isinstance(budget, BudgetGuardChatProvider)
    fallback = budget._inner
    assert isinstance(fallback, FallbackChatProvider)

    assert isinstance(fallback._primary, CircuitBreakerChatProvider)
    assert isinstance(fallback._fallback, CircuitBreakerChatProvider)
    assert isinstance(fallback._primary._inner, GoogleChatProvider)
    assert isinstance(fallback._fallback._inner, GoogleChatProvider)


def test_breaker_is_inside_the_fallback_not_outside(settings: Settings) -> None:
    """Regression guard for the one ordering mistake that would silently
    invert the fallback's purpose: a breaker wrapped OUTSIDE would refuse the
    primary and the fallback together, taking the whole role offline exactly
    when the fallback is most needed."""
    provider = ProviderFactory.summarizer(settings, RunLedger())
    assert not isinstance(provider, CircuitBreakerChatProvider)
    assert not isinstance(provider._inner, CircuitBreakerChatProvider)


def test_accounting_layers_are_omitted_without_a_ledger(settings: Settings) -> None:
    """No per-run context → no accounting layers, but the resilience layers
    still apply. Layers being individually optional is the property that makes
    decorating the interface worth doing."""
    provider = ProviderFactory.summarizer(settings)
    assert not isinstance(provider, MeteredChatProvider)
    assert not isinstance(provider, BudgetGuardChatProvider)
    assert isinstance(provider, FallbackChatProvider)
    assert isinstance(provider._primary, CircuitBreakerChatProvider)


def test_judge_defaults_to_gemini_when_no_openai_key(settings: Settings) -> None:
    # No OPENAI_API_KEY → judge runs on a second Gemini model (same-family).
    provider = ProviderFactory.judge(settings)
    assert isinstance(provider, FallbackChatProvider)
    assert isinstance(provider._primary._inner, GoogleChatProvider)
    assert isinstance(provider._fallback._inner, GoogleChatProvider)


def test_judge_uses_openai_compatible_provider_when_configured(settings: Settings) -> None:
    # OPENAI_API_KEY set (e.g. a Groq key) → judge routes to the OpenAI adapter
    # for a cross-family check, no code change.
    s = settings.model_copy(
        update={"openai_api_key": "test-groq-key", "openai_base_url": "https://api.groq.com/openai/v1"}
    )
    provider = ProviderFactory.judge(s)
    assert isinstance(provider, FallbackChatProvider)
    assert isinstance(provider._primary._inner, OpenAIChatProvider)
    assert not isinstance(provider._primary._inner, GoogleChatProvider)


def test_embedding_provider_is_cached_retrying_gemini(settings: Settings) -> None:
    provider = ProviderFactory.embedding(settings)
    assert isinstance(provider, EmbeddingProvider)
    # Cache outermost: a hit must not consume a retry budget.
    assert isinstance(provider, CachingEmbeddingProvider)
    assert isinstance(provider._inner, RetryingEmbeddingProvider)
    assert isinstance(provider._inner._inner, GoogleEmbeddingProvider)


def test_embedding_cache_is_shared_across_runs(settings: Settings) -> None:
    """The re-run flow is the whole point of the cache, so the instance has to
    outlive one run's provider."""
    first = ProviderFactory.embedding(settings)
    second = ProviderFactory.embedding(settings)
    assert isinstance(first, CachingEmbeddingProvider)
    assert isinstance(second, CachingEmbeddingProvider)
    assert first._cache is second._cache


def test_web_search_falls_back_to_serper_when_configured(settings: Settings) -> None:
    provider = ProviderFactory.web_search(settings)
    assert isinstance(provider, FallbackSearchProvider)


def test_web_search_skips_fallback_when_no_serper_key(settings: Settings) -> None:
    settings_no_serper = settings.model_copy(update={"serper_api_key": ""})
    provider = ProviderFactory.web_search(settings_no_serper)
    assert isinstance(provider, SearchProvider)
    assert not isinstance(provider, FallbackSearchProvider)


def test_web_search_uses_serper_alone_when_brave_unset_or_placeholder(settings: Settings) -> None:
    # Brave empty OR left as a <PLACEHOLDER> → Serper becomes the sole provider,
    # with no failing Brave attempt.
    for brave in ("", "<BSA-REPLACE_ME>"):
        s = settings.model_copy(update={"brave_search_api_key": brave})
        provider = ProviderFactory.web_search(s)
        assert isinstance(provider, SerperSearchProvider)
        assert not isinstance(provider, FallbackSearchProvider)
