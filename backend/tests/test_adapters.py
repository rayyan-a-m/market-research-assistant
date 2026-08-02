"""Regression tests for the chat adapters' runnable assembly.

These build the LangChain runnable exactly as `complete()` does — binding
structured output on the base chat model, then wrapping with retry — WITHOUT
making a network call. They exist because a construction-only factory test
missed a real bug: `.with_retry()` was applied first, producing a
`RunnableRetry` that has no `.with_structured_output()`, which only blew up at
runtime inside `complete()`. Building the runnable here catches that offline.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.providers.google_provider import GoogleChatProvider
from app.providers.openai_provider import OpenAIChatProvider


class _Sample(BaseModel):
    answer: str


def test_google_build_runnable_with_structured_output_does_not_raise() -> None:
    provider = GoogleChatProvider(model="gemini-2.5-pro", api_key="test-key")
    runnable = provider._build_runnable(max_tokens=512, response_model=_Sample)
    assert hasattr(runnable, "ainvoke")


def test_google_build_runnable_plain() -> None:
    provider = GoogleChatProvider(model="gemini-2.5-pro", api_key="test-key")
    runnable = provider._build_runnable(max_tokens=512, response_model=None)
    assert hasattr(runnable, "ainvoke")


def test_openai_build_runnable_with_structured_output_does_not_raise() -> None:
    # base_url points at any OpenAI-compatible endpoint (Groq/OpenRouter/...)
    provider = OpenAIChatProvider(
        model="llama-3.3-70b-versatile", api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
    )
    runnable = provider._build_runnable(max_tokens=512, response_model=_Sample)
    assert hasattr(runnable, "ainvoke")


def test_openai_build_runnable_plain() -> None:
    provider = OpenAIChatProvider(model="gpt-4o-mini", api_key="test-key")
    runnable = provider._build_runnable(max_tokens=512, response_model=None)
    assert hasattr(runnable, "ainvoke")
