"""Deep readiness endpoint + dependency checks.

The endpoint is the "validate my keys after a deploy" tool: it aggregates the
per-dependency checks and maps all-ok → 200, any-failure → 503. These tests
stub the checks (no network) and assert the aggregation, the status code, and
that no secrets leak into the body.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.core.preflight import check_serper
from app.main import create_app


def _client_with(**overrides: object) -> TestClient:
    """A TestClient whose settings are injected, not inherited.

    `_env_file=None` matters here. `/readyz` short-circuits the database check
    when `DATABASE_URL` is unset, so with ambient settings this suite passes on
    a developer machine (which has a repo-root .env) and 503s in CI (which
    doesn't) — the test would be asserting on whose laptop it ran. Injecting
    settings through the same DI seam the app uses in production is the fix.
    """
    app = create_app()
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://user:pw@localhost:5432/test",
        SERPER_API_KEY="test-key",
        **overrides,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def ok_preflight(settings: object) -> list[str]:
        return []

    async def ok_serper(api_key: str) -> None:
        return None

    async def ok_database() -> None:
        return None

    monkeypatch.setattr("app.routers.health.run_preflight", ok_preflight)
    monkeypatch.setattr("app.routers.health.check_serper", ok_serper)
    monkeypatch.setattr("app.routers.health.check_database", ok_database)
    return _client_with()


def test_readyz_all_ok_returns_200(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"gemini": "ok", "serper": "ok", "database": "ok"}
    # config surfaces FRONTEND_ORIGIN so a CORS mismatch is diagnosable
    assert "frontend_origin" in body["config"]


def test_readyz_reports_503_and_names_the_failing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok_preflight(settings: object) -> list[str]:
        return []

    async def bad_serper(api_key: str) -> str:
        return "auth failed (HTTP 403) — check SERPER_API_KEY"

    async def ok_database() -> None:
        return None

    monkeypatch.setattr("app.routers.health.run_preflight", ok_preflight)
    monkeypatch.setattr("app.routers.health.check_serper", bad_serper)
    monkeypatch.setattr("app.routers.health.check_database", ok_database)

    resp = _client_with().get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["gemini"] == "ok"
    assert "403" in body["checks"]["serper"]


async def test_check_serper_reports_missing_key_without_network() -> None:
    assert await check_serper("") == "SERPER_API_KEY not set"


def test_readyz_body_carries_no_secret_values(client: TestClient) -> None:
    # The config block must expose only non-secret fields.
    config = client.get("/readyz").json()["config"]
    assert set(config) == {
        "env",
        "frontend_origin",
        "summarizer_model",
        "judge_model",
        "embedding_model",
        "cross_family_judge",
    }
