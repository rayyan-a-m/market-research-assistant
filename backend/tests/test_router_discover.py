"""Router-level test for POST /api/runs/discover.

Exercises the real request → validation → auth → discovery → response wiring
with the DB and discovery service stubbed, so it runs offline. Catches wiring
bugs (status transitions, response shape, input validation, auth) that the
pure-logic tests don't touch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.core.auth import current_user_id
from app.main import create_app
from app.models.schemas import DiscoveryCandidate


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app()
    app.dependency_overrides[current_user_id] = lambda: "test-user"

    async def fake_create_run(**kwargs: object) -> str:
        return "run-123"

    async def fake_set_run_status(*args: object, **kwargs: object) -> None:
        return None

    async def fake_discover(run_id, competitors, topics, input_urls, settings, search=None):  # type: ignore[no-untyped-def]
        return [
            DiscoveryCandidate(
                id="", url="https://techcrunch.com/x", domain="techcrunch.com",
                rationale="payments coverage for Stripe", competitor="Stripe", ssrf_safe=True,
            )
        ]

    async def fake_insert(run_id: str, candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
        return [c.model_copy(update={"id": "cand-1"}) for c in candidates]

    async def fake_record_search_calls(run_id: str, count: int) -> None:
        return None

    monkeypatch.setattr("app.db.repository.create_run", fake_create_run)
    monkeypatch.setattr("app.db.repository.set_run_status", fake_set_run_status)
    monkeypatch.setattr("app.db.repository.insert_discovery_candidates", fake_insert)
    monkeypatch.setattr("app.db.repository.record_search_calls", fake_record_search_calls)
    monkeypatch.setattr("app.discovery.service.discover", fake_discover)
    return TestClient(app)


def test_discover_happy_path_returns_awaiting_approval_with_candidates(client: TestClient) -> None:
    resp = client.post(
        "/api/runs/discover",
        json={"competitors": ["Stripe"], "topics": ["payments"], "urls": ["https://8.8.8.8/blog"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-123"
    assert body["status"] == "AWAITING_APPROVAL"
    assert body["discovery_skipped"] is False
    assert [c["id"] for c in body["candidates"]] == ["cand-1"]


def test_discover_rejects_ssrf_unsafe_url_with_422(client: TestClient) -> None:
    resp = client.post(
        "/api/runs/discover",
        json={"competitors": [], "topics": ["t"], "urls": ["https://169.254.169.254/"]},
    )
    assert resp.status_code == 422  # Pydantic SSRF validation at the boundary


def test_discover_requires_auth_when_not_disabled() -> None:
    app = create_app()
    # Force real auth (the local .env sets AUTH_DISABLED=true; override it)
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    resp = TestClient(app).post(
        "/api/runs/discover",
        json={"competitors": [], "topics": ["t"], "urls": ["https://8.8.8.8/"]},
    )
    assert resp.status_code == 401
