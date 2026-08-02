from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.core.preflight import check_database, check_serper, run_preflight

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only — deliberately does not call out to any provider.
    A provider outage should not make the container report unhealthy and
    get recycled; it should surface as a 5xx on the specific endpoint
    that needed the provider."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(settings: Settings = Depends(get_settings)) -> JSONResponse:
    """Deep readiness: actively validates every external dependency and reports
    a per-dependency status, plus the effective non-secret config. Returns 200
    when all pass, 503 when any fail — so a single `curl .../readyz` after a
    deploy tells you whether the keys, the DB, and CORS are wired correctly.

    Kept OFF the liveness path (`/healthz`) on purpose: this makes live calls
    (Gemini ListModels, one Serper query, a DB round-trip), so it must never be
    what Azure polls to decide whether to recycle the container. No secrets are
    included in the response.
    """
    gemini_problems = await run_preflight(settings)
    serper = await check_serper(settings.serper_api_key)
    database = await check_database() if settings.database_url else "DATABASE_URL not set"

    checks = {
        "gemini": "ok" if not gemini_problems else "; ".join(gemini_problems),
        "serper": "ok" if serper is None else serper,
        "database": "ok" if database is None else database,
    }
    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ready" if all_ok else "degraded",
            "checks": checks,
            # Non-secret config, so a FRONTEND_ORIGIN / env / model mismatch is
            # visible without shelling into the container.
            "config": {
                "env": settings.env,
                "frontend_origin": settings.frontend_origin,
                "summarizer_model": settings.summarizer_model,
                "judge_model": settings.judge_model,
                "embedding_model": settings.embedding_model,
                "cross_family_judge": bool(settings.openai_api_key),
            },
        },
    )
