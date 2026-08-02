"""App factory — `create_app()` rather than a module-level `app = FastAPI()`
side effect, so tests can construct isolated instances.

The asyncpg pool is opened/closed in the lifespan. If DATABASE_URL is unset
(e.g. a bare import in a unit test), the pool simply isn't opened — the
health endpoint and pure-logic tests don't need it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.observability import RequestContextMiddleware, configure_logging
from app.core.preflight import run_preflight
from app.db.pool import close_pool, init_pool
from app.routers import health, runs, sources

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.database_url:
        await init_pool(settings.database_url)
    # Log the effective non-secret config once at boot. This is the line that
    # turns a "Failed to fetch" CORS mystery into an obvious FRONTEND_ORIGIN
    # mismatch — the value the browser must match is right here in the logs.
    logger.info(
        "startup_config",
        extra={
            "env": settings.env,
            "frontend_origin": settings.frontend_origin,
            "summarizer_model": settings.summarizer_model,
            "judge_model": settings.judge_model,
            "embedding_model": settings.embedding_model,
            "search_provider": "serper" if settings.serper_api_key else "none",
            "cross_family_judge": bool(settings.openai_api_key),
            "auth": "disabled"
            if (settings.auth_disabled and settings.env != "production")
            else "clerk",
            "db_configured": bool(settings.database_url),
        },
    )
    # Preflight: validate configured models against the live API and log any
    # problem clearly at boot (never fatal). This is the antidote to a model
    # being valid in config but retired on the provider's side.
    for problem in await run_preflight(settings):
        logger.error("preflight: %s", problem)
    try:
        yield
    finally:
        await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)
    app = FastAPI(
        title="Market Research Intelligence Assistant API",
        version="1.0.0",
        docs_url="/docs" if settings.env != "production" else None,
        lifespan=lifespan,
    )

    # Order matters: Starlette runs middleware in reverse registration order,
    # so registering the request-context middleware LAST makes it outermost —
    # it sees the request first and the response last, and therefore tags CORS
    # preflights and error responses too. That's the whole reason correlation
    # lives in ASGI middleware instead of a dependency.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Without this the browser can't read the header, so a user reporting
        # a bug has no id to quote.
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(runs.router)
    app.include_router(sources.router)

    return app


app = create_app()
