"""Shared test fixtures.

The rate limiter and the embedding cache both keep module-level state that
deliberately outlives a single request (that's the point of a sliding window
and of a cross-run cache). In tests that same persistence is cross-test
contamination: a router test that hits `/discover` eleven times across two
test functions would start getting 429s for reasons that have nothing to do
with what it's asserting.

Resetting them automatically is better than remembering to, because the
failure mode is order-dependent and only appears once the suite grows.
"""

from __future__ import annotations

import pytest

from app.core.rate_limit import reset_limiters
from app.providers.factory import ProviderFactory


@pytest.fixture(autouse=True)
def _isolate_process_level_state() -> None:
    reset_limiters()
    ProviderFactory._embedding_cache = None
