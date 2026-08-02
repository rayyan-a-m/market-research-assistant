"""Normalized error types.

Vendor SDKs (google.api_core, openai, httpx) each raise their own exception
hierarchy. Callers above the provider/adapter layer should never need to
know which vendor raised what — they catch one of these instead. Each
adapter is responsible for translating vendor exceptions into these at
the boundary.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application-raised errors."""


class ProviderError(AppError):
    """Base class for errors originating from an external AI/search provider."""

    def __init__(self, message: str, *, provider: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.cause = cause


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the configured timeout."""


class ProviderRateLimitError(ProviderError):
    """The provider rejected the call due to rate limiting (HTTP 429)."""


class ProviderUnavailableError(ProviderError):
    """The provider returned a server-side error (5xx) or is unreachable."""


class ProviderResponseError(ProviderError):
    """The provider responded, but the response was malformed or failed
    schema validation (e.g. structured output didn't match the expected
    Pydantic model)."""


class BudgetExceededError(ProviderError):
    """The run has spent its token budget, so no further provider call will be
    made.

    Modelled as a `ProviderError` on purpose. It isn't a vendor failure, but
    it is raised at the provider boundary and means the same thing to every
    caller — "this call cannot be made" — so the existing degrade-don't-die
    handling covers it with no new code: the judge stage already catches
    `ProviderError` per claim and marks that claim unverified, which turns
    budget exhaustion into a partially-verified run instead of a failed one.
    The fallback decorator never sees it, because the budget layer is
    composed *outside* the fallback (see providers/middleware.py).
    """


class UnsafeUrlError(AppError):
    """A URL failed the SSRF guard and must not be fetched."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"URL not allowed: {url} ({reason})")
        self.url = url
        self.reason = reason
