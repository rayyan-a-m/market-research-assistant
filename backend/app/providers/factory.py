"""Registry-based factory: the only place in the codebase that imports
a concrete provider class or knows a vendor name string. Everything else
(routers, pipeline, discovery agent) asks for a provider by *role*
("summarizer", "judge", "discovery", "embedding", "web_search") and gets
back a `ChatProvider`/`EmbeddingProvider`/`SearchProvider`, wired to the
configured model/fallback from Settings.

Adding a new vendor is: write an adapter implementing the relevant
interface, register it under a name, done — no caller changes. That's
the concrete payoff of Strategy+Adapter+Factory over a hardcoded
`ChatGoogleGenerativeAI(...)` call sitting inside the summarizer function.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings
from app.providers.base import ChatProvider, EmbeddingProvider, RetryPolicy, SearchProvider
from app.providers.fallback import FallbackChatProvider, FallbackSearchProvider
from app.providers.google_provider import GoogleChatProvider, GoogleEmbeddingProvider
from app.providers.middleware import (
    BudgetGuardChatProvider,
    CachingEmbeddingProvider,
    CircuitBreakerChatProvider,
    EmbeddingCache,
    MeteredChatProvider,
    RetryingEmbeddingProvider,
    RunLedger,
)
from app.providers.openai_provider import OpenAIChatProvider
from app.providers.search_providers import BraveSearchProvider, SerperSearchProvider

ChatFactoryFn = Callable[[str, Settings], ChatProvider]
EmbeddingFactoryFn = Callable[[str, Settings], EmbeddingProvider]
SearchFactoryFn = Callable[[Settings], SearchProvider]


def _key_set(value: str) -> bool:
    """A key counts as configured only if it's non-empty and not a leftover
    `<PLACEHOLDER>` from the .env template — so an unfilled Brave key is
    treated as absent rather than sent as a bad token."""
    return bool(value) and not value.startswith("<")


class ProviderFactory:
    """Registry of vendor name -> constructor. Registered once at import
    time below; `create_*` methods are what callers actually use."""

    _chat_registry: dict[str, ChatFactoryFn] = {}
    _embedding_registry: dict[str, EmbeddingFactoryFn] = {}
    _embedding_cache: EmbeddingCache | None = None

    @classmethod
    def register_chat(cls, vendor: str, fn: ChatFactoryFn) -> None:
        cls._chat_registry[vendor] = fn

    @classmethod
    def register_embedding(cls, vendor: str, fn: EmbeddingFactoryFn) -> None:
        cls._embedding_registry[vendor] = fn

    @classmethod
    def create_chat(cls, *, vendor: str, model: str, settings: Settings) -> ChatProvider:
        if vendor not in cls._chat_registry:
            raise ValueError(
                f"unknown chat provider vendor: {vendor!r} (registered: {list(cls._chat_registry)})"
            )
        return cls._chat_registry[vendor](model, settings)

    @classmethod
    def create_embedding(cls, *, vendor: str, model: str, settings: Settings) -> EmbeddingProvider:
        if vendor not in cls._embedding_registry:
            raise ValueError(
                f"unknown embedding provider vendor: {vendor!r} "
                f"(registered: {list(cls._embedding_registry)})"
            )
        return cls._embedding_registry[vendor](model, settings)

    # --- Role-based resolution: this is what the rest of the app calls ---

    @classmethod
    def _wrap_chat(
        cls,
        *,
        vendor: str,
        model: str,
        fallback_model: str,
        settings: Settings,
        ledger: RunLedger | None,
        role: str,
    ) -> ChatProvider:
        """Assemble the middleware stack around a primary/fallback model pair.

        This is the single place the layer order is decided, which is the
        point of having a factory at all — `summarizer()` and `judge()` differ
        only in their models, not in their resilience or accounting policy, and
        nothing outside this file can compose them wrongly.

        Order (and why) is documented in providers/middleware.py. The short
        version: breakers go *inside* the fallback so an open primary routes to
        the fallback instead of blocking it; the budget guard goes *outside* so
        a stopped run doesn't burn one last fallback call.
        """
        def breaker(model_name: str) -> ChatProvider:
            return CircuitBreakerChatProvider(
                cls.create_chat(vendor=vendor, model=model_name, settings=settings),
                name=f"{vendor}:{model_name}",
                failure_threshold=settings.breaker_failure_threshold,
                cooldown_seconds=settings.breaker_cooldown_seconds,
            )

        stack: ChatProvider = FallbackChatProvider(
            primary=breaker(model), fallback=breaker(fallback_model)
        )
        # No ledger means no per-run accounting context (e.g. a preflight or a
        # test constructing a provider directly) — the resilience layers still
        # apply, the accounting ones are simply not composed. Layers being
        # individually optional is a property of decorating the interface.
        if ledger is not None:
            stack = BudgetGuardChatProvider(
                stack, ledger, max_tokens_per_run=settings.run_token_budget
            )
            stack = MeteredChatProvider(stack, ledger, role=role)
        return stack

    @classmethod
    def summarizer(cls, settings: Settings, ledger: RunLedger | None = None) -> ChatProvider:
        return cls._wrap_chat(
            vendor="google",
            model=settings.summarizer_model,
            fallback_model=settings.summarizer_fallback_model,
            settings=settings,
            ledger=ledger,
            role="summarizer",
        )

    @classmethod
    def judge(cls, settings: Settings, ledger: RunLedger | None = None) -> ChatProvider:
        # Cross-family judge if an OpenAI-compatible provider is configured
        # (Groq/OpenRouter/OpenAI), otherwise default to a second Gemini model.
        # Either way it's a config choice, not a code change — the payoff of
        # coding the judge to the ChatProvider interface.
        vendor = "openai" if settings.openai_api_key else "google"
        return cls._wrap_chat(
            vendor=vendor,
            model=settings.judge_model,
            fallback_model=settings.judge_fallback_model,
            settings=settings,
            ledger=ledger,
            role="judge",
        )

    @classmethod
    def embedding(cls, settings: Settings) -> EmbeddingProvider:
        """Embeddings get retry (the SDK offers none) then caching, outermost
        first: a cache hit should not be able to consume a retry budget, and a
        retried call should still populate the cache."""
        inner = cls.create_embedding(
            vendor="google", model=settings.embedding_model, settings=settings
        )
        retrying = RetryingEmbeddingProvider(
            inner,
            RetryPolicy(
                max_attempts=settings.provider_max_retries,
                timeout_seconds=settings.provider_timeout_seconds,
            ),
        )
        return CachingEmbeddingProvider(
            retrying, cls.embedding_cache(settings), namespace=settings.embedding_model
        )

    @classmethod
    def embedding_cache(cls, settings: Settings) -> EmbeddingCache:
        """Process-wide embedding cache, created once.

        Deliberately shared across runs rather than per-run — the win is the
        "re-run with the same sources" flow, where the documents are
        byte-identical to a previous run's. Process-scoped state is consistent
        with the single-replica deployment; a multi-replica version would move
        it to Redis alongside the unreachable-source waiter map
        (FUTURE_ENHANCEMENTS #1), at which point it also stops being lost on
        redeploy.
        """
        if cls._embedding_cache is None:
            cls._embedding_cache = EmbeddingCache(
                max_entries=settings.embedding_cache_max_entries
            )
        return cls._embedding_cache

    @classmethod
    def web_search(cls, settings: Settings) -> SearchProvider:
        brave = _key_set(settings.brave_search_api_key)
        serper = _key_set(settings.serper_api_key)
        if brave and serper:  # Brave primary, Serper fallback
            return FallbackSearchProvider(
                primary=BraveSearchProvider(api_key=settings.brave_search_api_key),
                fallback=SerperSearchProvider(api_key=settings.serper_api_key),
            )
        if serper:  # Serper alone — no Brave key needed
            return SerperSearchProvider(api_key=settings.serper_api_key)
        return BraveSearchProvider(api_key=settings.brave_search_api_key)


def _make_google(model: str, settings: Settings) -> ChatProvider:
    return GoogleChatProvider(
        model=model,
        api_key=settings.gemini_api_key,
        timeout_seconds=settings.provider_timeout_seconds,
        max_retries=settings.provider_max_retries,
    )


def _make_openai(model: str, settings: Settings) -> ChatProvider:
    # Generic OpenAI-compatible adapter: real OpenAI when base_url is empty, or
    # any compatible endpoint (Groq, OpenRouter, ...) when OPENAI_BASE_URL is set.
    return OpenAIChatProvider(
        model=model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout_seconds=settings.provider_timeout_seconds,
        max_retries=settings.provider_max_retries,
    )


def _make_google_embedding(model: str, settings: Settings) -> EmbeddingProvider:
    return GoogleEmbeddingProvider(
        model=model, api_key=settings.gemini_api_key, timeout_seconds=settings.provider_timeout_seconds
    )


ProviderFactory.register_chat("google", _make_google)
ProviderFactory.register_chat("openai", _make_openai)
ProviderFactory.register_embedding("google", _make_google_embedding)
