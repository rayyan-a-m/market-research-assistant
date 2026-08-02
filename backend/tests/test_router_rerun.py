"""Router test for POST /api/runs/{id}/rerun (reuse sources, skip discovery)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth import current_user_id
from app.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app()
    app.dependency_overrides[current_user_id] = lambda: "test-user"

    async def fake_owner_status(run_id: str, user_id: str) -> str | None:
        return "FAILED" if run_id == "old-1" else None

    async def fake_get_inputs(run_id: str):  # type: ignore[no-untyped-def]
        return (["Stripe"], ["payments"], ["https://a.com"], "focus EMEA")

    async def fake_create_run(**kwargs: object) -> str:
        return "new-1"

    async def fake_copy(old: str, new: str) -> int:
        return 3

    async def fake_set_status(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.db.repository.get_run_owner_status", fake_owner_status)
    monkeypatch.setattr("app.db.repository.get_run_inputs", fake_get_inputs)
    monkeypatch.setattr("app.db.repository.create_run", fake_create_run)
    monkeypatch.setattr("app.db.repository.copy_approved_candidates", fake_copy)
    monkeypatch.setattr("app.db.repository.set_run_status", fake_set_status)
    return TestClient(app)


def test_rerun_creates_new_pending_run_reusing_sources(client: TestClient) -> None:
    resp = client.post("/api/runs/old-1/rerun")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "new-1"
    assert body["status"] == "PENDING"
    assert body["reused_sources"] == 3


def test_rerun_returns_404_for_run_not_owned(client: TestClient) -> None:
    assert client.post("/api/runs/missing/rerun").status_code == 404
