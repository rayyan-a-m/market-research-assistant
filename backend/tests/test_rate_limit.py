from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.core.auth import current_user_id
from app.core.rate_limit import SlidingWindowRateLimiter, limit_runs, reset_limiters


@pytest.fixture(autouse=True)
def _clean_limiters() -> None:
    reset_limiters()


# --- The window itself -------------------------------------------------------


def test_allows_up_to_the_limit_then_refuses() -> None:
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60)
    assert all(limiter.check("u", now=100.0) is None for _ in range(3))
    retry = limiter.check("u", now=100.0)
    assert retry is not None and retry == pytest.approx(60.0)


def test_window_slides_rather_than_resetting_on_a_boundary() -> None:
    """The reason for a deque over a counter: a fixed window would allow a
    full burst either side of the reset, i.e. 2x the intended rate at exactly
    the moment an impatient user retries."""
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10)
    limiter.check("u", now=0.0)
    limiter.check("u", now=9.0)

    assert limiter.check("u", now=9.5) is not None  # both still in window

    # The first hit ages out at t=10; capacity returns one slot at a time,
    # not all at once.
    assert limiter.check("u", now=10.5) is None
    assert limiter.check("u", now=10.6) is not None


def test_retry_after_reflects_when_the_oldest_hit_expires() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=30)
    limiter.check("u", now=100.0)
    retry = limiter.check("u", now=110.0)
    assert retry == pytest.approx(20.0)


def test_users_are_limited_independently() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("alice", now=0.0) is None
    assert limiter.check("bob", now=0.0) is None, "bob must not inherit alice's usage"
    assert limiter.check("alice", now=0.0) is not None


# --- The dependency ----------------------------------------------------------


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.post("/start")
    def start(user_id: str = Depends(limit_runs)) -> dict[str, str]:
        return {"user_id": user_id}

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[current_user_id] = lambda: "user-1"
    return app


def _settings(**over: object) -> Settings:
    return Settings(
        _env_file=None,
        GEMINI_API_KEY="x",
        RATE_LIMIT_RUNS_PER_WINDOW=2,
        RATE_LIMIT_WINDOW_SECONDS=60,
        **over,
    )


def test_endpoint_returns_429_with_retry_after_once_over_the_limit() -> None:
    client = TestClient(_app(_settings()))

    assert client.post("/start").status_code == 200
    assert client.post("/start").status_code == 200

    blocked = client.post("/start")
    assert blocked.status_code == 429
    # Retry-After is what lets a client back off instead of retry-storming.
    assert int(blocked.headers["Retry-After"]) >= 1


def test_limiter_can_be_disabled_by_config() -> None:
    client = TestClient(_app(_settings(RATE_LIMIT_ENABLED=False)))
    for _ in range(10):
        assert client.post("/start").status_code == 200


def test_dependency_returns_the_user_id_so_routes_need_only_one_depends() -> None:
    client = TestClient(_app(_settings()))
    assert client.post("/start").json() == {"user_id": "user-1"}
