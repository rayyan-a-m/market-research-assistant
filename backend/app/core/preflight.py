"""Startup preflight: validate configured models against the live API.

This exists because a whole class of failure — a model that's valid in config
but retired/unavailable on the provider's servers (e.g. Google removing
`text-embedding-004`) — cannot be caught by unit tests, which mock the
network. It only shows up as a mid-pipeline crash after real work is done.

`run_preflight` calls Gemini's ListModels once and checks that the configured
summarizer/embedding models exist and support the method we need, turning that
mid-run crash into a clear log line at boot ("EMBEDDING_MODEL 'x' not found —
available for embedContent: ..."). It never raises: a network/auth hiccup
returns an advisory note rather than blocking startup.

The pure decision logic (`evaluate_gemini_models`) is split from the network
fetch so it can be unit-tested offline with a canned model list.

Known limit: this is a *catalog* check (ListModels), so it catches a model
that's been removed from the catalog — but NOT one that's listed yet not
callable on your key (e.g. `gemini-2.5-pro` is listed but returns "no longer
available to new users" on generateContent). Only a real call catches that;
see the gated integration tests (`pytest -m integration`).
"""

from __future__ import annotations

import httpx

from app.config import Settings

_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _norm(name: str) -> str:
    return name if name.startswith("models/") else f"models/{name}"


def evaluate_gemini_models(available: dict[str, list[str]], settings: Settings) -> list[str]:
    """Pure logic: given {model_name: supported_methods}, return a list of
    human-readable problems with the configured models (empty = all good)."""
    problems: list[str] = []

    def _check(model: str, method: str, role: str) -> None:
        methods = available.get(_norm(model))
        if methods is None:
            suggestions = sorted(n for n, m in available.items() if method in m)
            hint = f" Available for {method}: {', '.join(suggestions[:5])}" if suggestions else ""
            problems.append(f"{role} '{model}' not found on this API key.{hint}")
        elif method not in methods:
            problems.append(f"{role} '{model}' does not support {method}.")

    _check(settings.summarizer_model, "generateContent", "SUMMARIZER_MODEL")
    _check(settings.summarizer_fallback_model, "generateContent", "SUMMARIZER_FALLBACK_MODEL")
    _check(settings.embedding_model, "embedContent", "EMBEDDING_MODEL")
    return problems


async def _list_gemini_models(api_key: str, *, timeout: float = 15.0) -> dict[str, list[str]]:
    params: dict[str, str | int] = {"key": api_key, "pageSize": 200}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_GEMINI_MODELS_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    return {m["name"]: m.get("supportedGenerationMethods", []) for m in data.get("models", [])}


async def run_preflight(settings: Settings) -> list[str]:
    """Validate the configured Gemini models against the live API. Returns a
    list of problems (empty = all good). Never raises."""
    if not settings.gemini_api_key:
        return []
    try:
        available = await _list_gemini_models(settings.gemini_api_key)
    except Exception as exc:  # noqa: BLE001 — advisory only, must not block startup
        return [f"could not verify Gemini models (ListModels failed: {exc})"]
    return evaluate_gemini_models(available, settings)
