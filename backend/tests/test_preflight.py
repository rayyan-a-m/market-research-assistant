from __future__ import annotations

from app.config import Settings
from app.core.preflight import evaluate_gemini_models


def _all_good() -> dict[str, list[str]]:
    # matches the current config defaults (the -latest aliases)
    return {
        "models/gemini-pro-latest": ["generateContent"],
        "models/gemini-flash-latest": ["generateContent"],
        "models/gemini-embedding-001": ["embedContent"],
    }


def test_no_problems_when_all_models_present() -> None:
    settings = Settings(_env_file=None)  # defaults match _all_good()
    assert evaluate_gemini_models(_all_good(), settings) == []


def test_flags_retired_embedding_model_with_a_suggestion() -> None:
    # This is exactly the real failure: text-embedding-004 retired from the API.
    settings = Settings(_env_file=None, EMBEDDING_MODEL="models/text-embedding-004")
    problems = evaluate_gemini_models(_all_good(), settings)
    assert any("EMBEDDING_MODEL" in p and "text-embedding-004" in p for p in problems)
    # the suggestion should point at an available embedding model
    assert any("gemini-embedding-001" in p for p in problems)


def test_flags_model_that_exists_but_lacks_the_method() -> None:
    available = {
        "models/gemini-pro-latest": ["embedContent"],  # wrong: no generateContent
        "models/gemini-flash-latest": ["generateContent"],
        "models/gemini-embedding-001": ["embedContent"],
    }
    settings = Settings(_env_file=None)
    problems = evaluate_gemini_models(available, settings)
    assert any("SUMMARIZER_MODEL" in p and "generateContent" in p for p in problems)


def test_flags_unknown_summarizer_model() -> None:
    settings = Settings(_env_file=None, SUMMARIZER_MODEL="gemini-9-ultra")
    problems = evaluate_gemini_models(_all_good(), settings)
    assert any("SUMMARIZER_MODEL" in p and "gemini-9-ultra" in p for p in problems)
