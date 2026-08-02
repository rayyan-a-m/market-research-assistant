from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only — deliberately does not call out to any provider.
    A provider outage should not make the container report unhealthy and
    get recycled; it should surface as a 5xx on the specific endpoint
    that needed the provider."""
    return {"status": "ok"}
