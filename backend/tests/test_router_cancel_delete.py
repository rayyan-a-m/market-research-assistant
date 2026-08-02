"""Router tests for POST /{id}/cancel and DELETE /{id}."""

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
        return {"running": "PROCESSING", "done": "COMPLETE"}.get(run_id)

    async def set_status(*args: object, **kwargs: object) -> None:
        return None

    async def delete(run_id: str, user_id: str) -> bool:
        return run_id == "running"

    monkeypatch.setattr("app.db.repository.get_run_owner_status", owner_status)
    monkeypatch.setattr("app.db.repository.set_run_status", set_status)
    monkeypatch.setattr("app.db.repository.delete_run", delete)
    return TestClient(app)


def test_cancel_in_progress_run(client: TestClient) -> None:
    resp = client.post("/api/runs/running/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


def test_cancel_finished_run_conflicts(client: TestClient) -> None:
    assert client.post("/api/runs/done/cancel").status_code == 409


def test_cancel_unknown_run_404(client: TestClient) -> None:
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_delete_run_204(client: TestClient) -> None:
    assert client.delete("/api/runs/running").status_code == 204


def test_delete_unknown_run_404(client: TestClient) -> None:
    assert client.delete("/api/runs/nope").status_code == 404
