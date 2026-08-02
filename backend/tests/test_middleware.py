"""Behaviour tests for the provider middleware layers.

These exercise the actual code paths against fakes at the provider boundary,
rather than asserting on construction — the lesson from the two runtime bugs
that construction-only tests missed (see tests/test_adapters.py).
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import (
    BudgetExceededError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.base import ChatProvider, ChatResult, EmbeddingProvider, Message, RetryPolicy
from app.providers.fallback import FallbackChatProvider
from app.providers.middleware import (
    BreakerState,
    BudgetGuardChatProvider,
    CachingEmbeddingProvider,
    CircuitBreakerChatProvider,
    EmbeddingCache,
    MeteredChatProvider,
    RetryingEmbeddingProvider,
    RunLedger,
)
from tests.fakes import FakeChatProvider


class CountingChatProvider(ChatProvider):
    """Returns a fixed token cost per call, or raises a queued exception."""

    def __init__(
        self,
        *,
        input_tokens: int = 10,
        output_tokens: int = 5,
        model: str = "fake-model",
        raises: list[Exception] | None = None,
    ) -> None:
        self.calls = 0
        self._input = input_tokens
        self._output = output_tokens
        self._model = model
        self._raises = raises or []

    async def complete(self, **kwargs: object) -> ChatResult[None]:
        self.calls += 1
        if self._raises:
            raise self._raises.pop(0)
        return ChatResult(
            text="ok", parsed=None, model=self._model,
            input_tokens=self._input, output_tokens=self._output,
        )


class CountingEmbeddingProvider(EmbeddingProvider):
    def __init__(self, *, raises: list[Exception] | None = None) -> None:
        self.batches: list[list[str]] = []
        self._raises = raises or []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self._raises:
            raise self._raises.pop(0)
        return [[float(len(t)), 1.0] for t in texts]


async def _call(provider: ChatProvider) -> ChatResult[None]:
    return await provider.complete(system="s", messages=[Message(role="user", content="u")])


# --- Metering ----------------------------------------------------------------


async def test_metered_accumulates_tokens_across_calls() -> None:
    ledger = RunLedger()
    provider = MeteredChatProvider(CountingChatProvider(), ledger, role="summarizer")

    await _call(provider)
    await _call(provider)

    assert ledger.calls == 2
    assert ledger.input_tokens == 20
    assert ledger.output_tokens == 10
    assert ledger.total_tokens == 30


async def test_metered_records_the_model_that_actually_answered() -> None:
    """When the fallback serves the call, the audit trail must name the
    fallback model — that divergence is the signal worth logging."""
    ledger = RunLedger()
    primary = CountingChatProvider(raises=[ProviderUnavailableError("down", provider="p")])
    fallback = CountingChatProvider(model="fallback-model")
    stack = MeteredChatProvider(
        FallbackChatProvider(primary=primary, fallback=fallback), ledger, role="summarizer"
    )

    await _call(stack)

    assert ledger.per_call == [("fallback-model", 10, 5)]


# --- Budget ------------------------------------------------------------------


async def test_budget_guard_allows_calls_below_the_cap() -> None:
    ledger = RunLedger()
    inner = CountingChatProvider()
    provider = BudgetGuardChatProvider(
        MeteredChatProvider(inner, ledger, role="judge"), ledger, max_tokens_per_run=100
    )
    for _ in range(3):
        await _call(provider)
    assert inner.calls == 3


async def test_budget_guard_refuses_once_the_cap_is_reached() -> None:
    ledger = RunLedger()
    inner = CountingChatProvider(input_tokens=30, output_tokens=20)  # 50/call
    provider = BudgetGuardChatProvider(
        MeteredChatProvider(inner, ledger, role="judge"), ledger, max_tokens_per_run=60
    )

    await _call(provider)  # 50 spent, still under 60
    await _call(provider)  # crosses to 100 — allowed, cap is checked pre-call
    with pytest.raises(BudgetExceededError):
        await _call(provider)

    assert inner.calls == 2, "no provider call should be made once the budget is gone"


async def test_budget_of_zero_disables_the_cap() -> None:
    ledger = RunLedger()
    inner = CountingChatProvider()
    provider = BudgetGuardChatProvider(
        MeteredChatProvider(inner, ledger, role="judge"), ledger, max_tokens_per_run=0
    )
    for _ in range(5):
        await _call(provider)
    assert inner.calls == 5


async def test_budget_error_does_not_trigger_the_fallback() -> None:
    """The ordering guarantee: because the budget layer is outside the
    fallback, exhausting the budget stops the run rather than spending one
    more call on the fallback model."""
    ledger = RunLedger()
    primary = CountingChatProvider(input_tokens=100, output_tokens=0)
    fallback = CountingChatProvider()
    stack = BudgetGuardChatProvider(
        MeteredChatProvider(
            FallbackChatProvider(primary=primary, fallback=fallback), ledger, role="judge"
        ),
        ledger,
        max_tokens_per_run=50,
    )

    await _call(stack)
    with pytest.raises(BudgetExceededError):
        await _call(stack)

    assert fallback.calls == 0


# --- Circuit breaker ---------------------------------------------------------


async def test_breaker_opens_after_consecutive_failures_and_fails_fast() -> None:
    inner = CountingChatProvider(
        raises=[ProviderUnavailableError("down", provider="p") for _ in range(3)]
    )
    breaker = CircuitBreakerChatProvider(inner, name="p", failure_threshold=3)

    for _ in range(3):
        with pytest.raises(ProviderUnavailableError):
            await _call(breaker)
    assert breaker.state is BreakerState.OPEN

    with pytest.raises(ProviderUnavailableError, match="circuit breaker open"):
        await _call(breaker)
    assert inner.calls == 3, "an open breaker must not reach the provider"


async def test_breaker_success_resets_the_failure_count() -> None:
    inner = CountingChatProvider(raises=[ProviderTimeoutError("slow", provider="p")])
    breaker = CircuitBreakerChatProvider(inner, name="p", failure_threshold=2)

    with pytest.raises(ProviderTimeoutError):
        await _call(breaker)
    await _call(breaker)  # success resets
    with pytest.raises(ProviderTimeoutError):
        await _call(CircuitBreakerChatProvider(
            CountingChatProvider(raises=[ProviderTimeoutError("slow", provider="p")]),
            name="p", failure_threshold=2,
        ))
    assert breaker.state is BreakerState.CLOSED


async def test_malformed_output_does_not_trip_the_breaker() -> None:
    """A bad generation is not a sick dependency. Tripping on it would take a
    model offline for being briefly stupid."""
    inner = CountingChatProvider(
        raises=[ProviderResponseError("bad schema", provider="p") for _ in range(5)]
    )
    breaker = CircuitBreakerChatProvider(inner, name="p", failure_threshold=2)

    for _ in range(5):
        with pytest.raises(ProviderResponseError):
            await _call(breaker)

    assert breaker.state is BreakerState.CLOSED
    assert inner.calls == 5, "every call should still have reached the provider"


async def test_rate_limit_does_trip_the_breaker() -> None:
    inner = CountingChatProvider(
        raises=[ProviderRateLimitError("429", provider="p") for _ in range(2)]
    )
    breaker = CircuitBreakerChatProvider(inner, name="p", failure_threshold=2)
    for _ in range(2):
        with pytest.raises(ProviderRateLimitError):
            await _call(breaker)
    assert breaker.state is BreakerState.OPEN


async def test_breaker_half_opens_after_cooldown_and_closes_on_success() -> None:
    inner = CountingChatProvider(raises=[ProviderUnavailableError("down", provider="p")])
    breaker = CircuitBreakerChatProvider(
        inner, name="p", failure_threshold=1, cooldown_seconds=0.01
    )

    with pytest.raises(ProviderUnavailableError):
        await _call(breaker)
    assert breaker.state is BreakerState.OPEN

    await asyncio.sleep(0.02)
    assert breaker.state is BreakerState.HALF_OPEN

    await _call(breaker)  # probe succeeds
    assert breaker.state is BreakerState.CLOSED


async def test_open_primary_breaker_routes_to_fallback_immediately() -> None:
    """The payoff of putting the breaker inside the fallback: once the primary
    is known-bad, the fallback answers without waiting on it."""
    primary = CountingChatProvider(
        raises=[ProviderUnavailableError("down", provider="p") for _ in range(2)]
    )
    fallback = CountingChatProvider(model="fallback-model")
    stack = FallbackChatProvider(
        primary=CircuitBreakerChatProvider(primary, name="primary", failure_threshold=2),
        fallback=CircuitBreakerChatProvider(fallback, name="fallback"),
    )

    await _call(stack)  # primary fails once -> fallback serves
    await _call(stack)  # primary fails again -> breaker opens, fallback serves
    result = await _call(stack)  # primary not called at all now

    assert result.model == "fallback-model"
    assert primary.calls == 2, "the open breaker should stop reaching the primary"
    assert fallback.calls == 3


# --- Embedding cache ---------------------------------------------------------


async def test_cache_serves_repeats_without_calling_the_provider() -> None:
    inner = CountingEmbeddingProvider()
    provider = CachingEmbeddingProvider(inner, EmbeddingCache(), namespace="m")

    first = await provider.embed(["alpha", "beta"])
    second = await provider.embed(["alpha", "beta"])

    assert first == second
    assert len(inner.batches) == 1, "the second call should be served entirely from cache"


async def test_cache_fetches_only_the_misses_and_preserves_order() -> None:
    """The re-run case: most documents are already known, so only the new one
    goes to the API — but the returned vectors must still line up with the
    input list."""
    inner = CountingEmbeddingProvider()
    provider = CachingEmbeddingProvider(inner, EmbeddingCache(), namespace="m")

    await provider.embed(["alpha", "gamma"])
    result = await provider.embed(["alpha", "beta", "gamma"])

    assert inner.batches[1] == ["beta"], "only the miss should be sent"
    assert result == [[5.0, 1.0], [4.0, 1.0], [5.0, 1.0]]
    assert len(result) == 3


async def test_cache_namespace_separates_models() -> None:
    """Two embedding models produce different vectors for the same text, so
    the model must be part of the key."""
    cache = EmbeddingCache()
    inner_a = CountingEmbeddingProvider()
    inner_b = CountingEmbeddingProvider()

    await CachingEmbeddingProvider(inner_a, cache, namespace="model-a").embed(["x"])
    await CachingEmbeddingProvider(inner_b, cache, namespace="model-b").embed(["x"])

    assert inner_b.batches == [["x"]], "a different model must not reuse the vector"


async def test_cache_evicts_least_recently_used() -> None:
    cache = EmbeddingCache(max_entries=2)
    provider = CachingEmbeddingProvider(CountingEmbeddingProvider(), cache, namespace="m")

    await provider.embed(["a"])
    await provider.embed(["b"])
    await provider.embed(["a"])  # touch "a" so "b" is now least-recent
    await provider.embed(["c"])  # evicts "b"

    assert len(cache) == 2
    inner = CountingEmbeddingProvider()
    await CachingEmbeddingProvider(inner, cache, namespace="m").embed(["a", "b"])
    assert inner.batches == [["b"]], "'a' should be cached, 'b' evicted"


async def test_empty_embed_short_circuits() -> None:
    inner = CountingEmbeddingProvider()
    provider = CachingEmbeddingProvider(inner, EmbeddingCache(), namespace="m")
    assert await provider.embed([]) == []
    assert inner.batches == []


# --- Embedding retry ---------------------------------------------------------


async def test_embedding_retries_transient_failures_then_succeeds() -> None:
    inner = CountingEmbeddingProvider(
        raises=[ProviderRateLimitError("429", provider="google")]
    )
    provider = RetryingEmbeddingProvider(
        inner, RetryPolicy(max_attempts=3, backoff_base_seconds=0.001)
    )

    result = await provider.embed(["x"])

    assert result == [[1.0, 1.0]]
    assert len(inner.batches) == 2


async def test_embedding_retry_gives_up_and_raises_the_last_error() -> None:
    inner = CountingEmbeddingProvider(
        raises=[ProviderRateLimitError(f"429-{i}", provider="google") for i in range(3)]
    )
    provider = RetryingEmbeddingProvider(
        inner, RetryPolicy(max_attempts=3, backoff_base_seconds=0.001)
    )

    with pytest.raises(ProviderRateLimitError, match="429-2"):
        await provider.embed(["x"])
    assert len(inner.batches) == 3


async def test_embedding_does_not_retry_a_malformed_response() -> None:
    inner = CountingEmbeddingProvider(
        raises=[ProviderResponseError("bad", provider="google")]
    )
    provider = RetryingEmbeddingProvider(
        inner, RetryPolicy(max_attempts=3, backoff_base_seconds=0.001)
    )

    with pytest.raises(ProviderResponseError):
        await provider.embed(["x"])
    assert len(inner.batches) == 1, "a malformed response won't fix itself on a retry"


# --- Composition -------------------------------------------------------------


async def test_layers_are_transparent_to_the_caller() -> None:
    """The whole stack still satisfies ChatProvider and returns the inner
    result unchanged — callers cannot tell it is wrapped."""
    ledger = RunLedger()
    inner = FakeChatProvider(parsed={"ok": True}, text="hello")
    stack: ChatProvider = MeteredChatProvider(
        BudgetGuardChatProvider(
            CircuitBreakerChatProvider(inner, name="fake"), ledger, max_tokens_per_run=1000
        ),
        ledger,
        role="summarizer",
    )

    result = await _call(stack)

    assert result.text == "hello"
    assert result.parsed == {"ok": True}
    assert inner.calls == 1
