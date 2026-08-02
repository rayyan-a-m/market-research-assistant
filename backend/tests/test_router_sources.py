"""Router tests for the blocked-source skip endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth import current_user_id
from app.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app()
    app.dependency_overrides[current_user_id] = lambda: "u"

    async def owner_status(run_id: str, user_id: str) -> str | None:
        return "PROCESSING" if run_id == "running" else None

    monkeypatch.setattr("app.db.repository.get_run_owner_status", owner_status)
    return TestClient(app)


def test_skip_source_ok(client: TestClient) -> None:
    resp = client.post("/api/runs/running/sources/skip", json={"source_id": "s1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "SKIPPED_BY_USER"
    # no waiter is registered in this unit context, so nothing was resolved
    assert resp.json()["resolved"] is False


def test_skip_source_unknown_run_404(client: TestClient) -> None:
    resp = client.post("/api/runs/nope/sources/skip", json={"source_id": "s1"})
    assert resp.status_code == 404
