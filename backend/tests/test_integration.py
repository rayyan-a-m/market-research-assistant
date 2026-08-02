"""Integration tests — real calls to the live provider APIs.

These are the ONLY tests that can catch a whole class of failure that unit
tests (which mock the network) and even the metadata preflight cannot: a
model that is *listed* in the catalog but not *callable* on your key. That's
exactly what happened with `gemini-2.5-pro` — ListModels showed it, but
generateContent returned "no longer available to new users". Only a real call
reveals that, so these tests make one tiny real call per configured model/role.

Deselected by default (see pyproject `addopts = -m 'not integration'`).
Run before deploy with real keys in the environment / .env:

    pytest -m integration

Each test skips (not fails) if the relevant key is absent, so running the
suite without keys is a no-op rather than a red build.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.providers.base import Message
from app.providers.factory import ProviderFactory

pytestmark = pytest.mark.integration


def _settings():  # type: ignore[no-untyped-def]
    return get_settings()


async def _assert_chat_model_callable(vendor: str, model: str) -> None:
    provider = ProviderFactory.create_chat(vendor=vendor, model=model, settings=_settings())
    result = await provider.complete(
        system="You are terse.", messages=[Message(role="user", content="Reply with the word OK.")]
    )
    assert result.text, f"{vendor}:{model} returned empty text"


async def test_summarizer_primary_model_is_callable() -> None:
    s = _settings()
    if not s.gemini_api_key:
        pytest.skip("no GEMINI_API_KEY")
    # Tests the configured PRIMARY directly (not the fallback wrapper), so a
    # broken/retired primary is caught rather than masked by the fallback.
    await _assert_chat_model_callable("google", s.summarizer_model)


async def test_summarizer_fallback_model_is_callable() -> None:
    s = _settings()
    if not s.gemini_api_key:
        pytest.skip("no GEMINI_API_KEY")
    await _assert_chat_model_callable("google", s.summarizer_fallback_model)


async def test_embedding_model_is_callable() -> None:
    s = _settings()
    if not s.gemini_api_key:
        pytest.skip("no GEMINI_API_KEY")
    vectors = await ProviderFactory.embedding(s).embed(["hello world"])
    assert vectors and len(vectors[0]) > 0


async def test_judge_primary_model_is_callable() -> None:
    s = _settings()
    # The judge routes to OpenAI-compatible if OPENAI_API_KEY is set, else Gemini.
    vendor = "openai" if s.openai_api_key else "google"
    if vendor == "google" and not s.gemini_api_key:
        pytest.skip("no GEMINI_API_KEY")
    await _assert_chat_model_callable(vendor, s.judge_model)


async def test_web_search_is_callable() -> None:
    s = _settings()
    if not (s.serper_api_key or s.brave_search_api_key):
        pytest.skip("no search API key")
    results = await ProviderFactory.web_search(s).search("Sitecore composable DXP", count=3)
    assert isinstance(results, list)  # returns without raising
