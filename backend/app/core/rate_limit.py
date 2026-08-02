"""Per-user rate limiting on the expensive endpoints.

The deployed app is public, backed by free-tier provider keys with
per-minute quotas shared across every user. Input bounds (DESIGN_DECISIONS
#4) cap what *one* request can cost; nothing capped how many requests one
user could issue. A signed-in user could exhaust the shared Gemini quota for
everyone in under a minute — not by attacking, just by clicking "Re-run"
repeatedly.

**Sliding window, not fixed window.** A fixed window (a counter reset every
minute) permits a full burst at 11:59:59 and another at 12:00:00 — double
the intended rate across the boundary, which is exactly when an impatient
user retries. A deque of timestamps per user, pruned on read, has no
boundary to exploit: the window is always the last N seconds from *now*.

**Cost of the deque:** memory is O(users × limit) and only for users active
inside the window, since empty deques are dropped during pruning. At a limit
of 10 that is trivially small. Timestamps are `monotonic()`, so an NTP
correction or a DST change can't move the window.

**In-process, which is consistent with the deployment.** The backend already
runs as a single replica because of the unreachable-source waiter map
(ARCHITECTURE, "why in-process"), so a per-process limiter is the accurate
limit rather than an approximation. If that constraint is lifted, this must
move to Redis at the same time — otherwise N replicas silently permit N×
the intended rate. Noted in FUTURE_ENHANCEMENTS #1 as part of the same
change, because splitting them would be a quiet regression.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, status

from app.config import Settings, get_settings
from app.core.auth import current_user_id


class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Record a hit and return None if allowed, or the seconds to wait.

        Read-prune-append happens with no `await` in between, so under a
        single-threaded event loop it is atomic and needs no lock — the same
        reasoning as the provider middleware's counters, and equally a
        property of the runtime rather than of the code looking safe.
        """
        now = time.monotonic() if now is None else now
        window = self._hits[key]
        cutoff = now - self._window
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self._limit:
            retry_after = window[0] + self._window - now
            return max(retry_after, 0.0)

        window.append(now)
        if not window:  # pragma: no cover — defensive; keeps the dict from growing
            del self._hits[key]
        return None

    def reset(self) -> None:
        self._hits.clear()


#: Module-level so the window survives across requests. One limiter per
#: concern rather than one global: starting a run is far more expensive than
#: discovering sources, and they deserve different limits.
_run_limiter: SlidingWindowRateLimiter | None = None
_discovery_limiter: SlidingWindowRateLimiter | None = None


def _limiter_for(kind: str, settings: Settings) -> SlidingWindowRateLimiter:
    global _run_limiter, _discovery_limiter
    if kind == "run":
        if _run_limiter is None:
            _run_limiter = SlidingWindowRateLimiter(
                limit=settings.rate_limit_runs_per_window,
                window_seconds=settings.rate_limit_window_seconds,
            )
        return _run_limiter
    if _discovery_limiter is None:
        _discovery_limiter = SlidingWindowRateLimiter(
            limit=settings.rate_limit_discovery_per_window,
            window_seconds=settings.rate_limit_window_seconds,
        )
    return _discovery_limiter


def _enforce(kind: str, user_id: str, settings: Settings) -> None:
    if not settings.rate_limit_enabled:
        return
    retry_after = _limiter_for(kind, settings).check(user_id)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Try again in {retry_after:.0f}s.",
            # Retry-After lets a client back off correctly instead of
            # hammering; omitting it turns a 429 into a retry storm.
            headers={"Retry-After": str(max(1, int(retry_after + 0.5)))},
        )


def limit_runs(
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
) -> str:
    """Dependency for run-starting endpoints. Returns the user id, so a route
    can depend on this *instead of* `current_user_id` rather than both."""
    _enforce("run", user_id, settings)
    return user_id


def limit_discovery(
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
) -> str:
    _enforce("discovery", user_id, settings)
    return user_id


def reset_limiters() -> None:
    """Test hook — module-level windows would otherwise leak between tests."""
    global _run_limiter, _discovery_limiter
    _run_limiter = None
    _discovery_limiter = None
