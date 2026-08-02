"""Data access for the four tables (runs, discovery_candidates,
run_sources, claims). One module of plain async functions over the pool —
the only place that writes SQL. Ownership is enforced here by carrying the
Clerk `user_id` into every run-scoped query, which is the operative access
control (see backend/db/schema.sql header on why RLS is defense-in-depth
rather than the active control).

UUIDs are selected as `::text` and returned as strings, so nothing above
this layer has to deal with driver-specific UUID objects.
"""

from __future__ import annotations

import json
from typing import Any

from app.db.pool import get_pool
from app.models.schemas import (
    Claim,
    DiscoveryCandidate,
    ResearchReport,
    RunDetail,
    RunMetrics,
    RunSource,
    RunStatus,
    RunSummary,
    Theme,
)

# ---------------------------------------------------------------- runs -----


async def create_run(
    *,
    user_id: str,
    competitors: list[str],
    topics: list[str],
    input_urls: list[str],
    context: str | None = None,
) -> str:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO runs (user_id, status, competitors, topics, input_urls, context)
        VALUES ($1, 'DISCOVERING', $2, $3, $4, $5)
        RETURNING id::text AS id
        """,
        user_id,
        competitors,
        topics,
        input_urls,
        context,
    )
    return row["id"]


async def set_run_status(
    run_id: str,
    status: RunStatus,
    *,
    error_detail: str | None = None,
    completed: bool = False,
    discovery_skipped: bool | None = None,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE runs
        SET status = $2,
            error_detail = COALESCE($3, error_detail),
            discovery_skipped = COALESCE($4, discovery_skipped),
            completed_at = CASE WHEN $5 THEN now() ELSE completed_at END
        WHERE id = $1::uuid
        """,
        run_id,
        status,
        error_detail,
        discovery_skipped,
        completed,
    )


async def set_run_metrics(run_id: str, metrics: RunMetrics) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE runs SET metrics = $2::jsonb WHERE id = $1::uuid",
        run_id,
        json.dumps(metrics.model_dump()),
    )


async def record_search_calls(run_id: str, count: int) -> None:
    """Persist the discovery search-query count into the run's metrics blob.

    Discovery runs in a separate request from the pipeline, so the count is
    stashed here (merged, not overwritten) and read back by the orchestrator
    when it writes the full metrics at the end of the run."""
    pool = get_pool()
    await pool.execute(
        "UPDATE runs SET metrics = coalesce(metrics, '{}'::jsonb) "
        "|| jsonb_build_object('search_calls', $2::int) WHERE id = $1::uuid",
        run_id,
        count,
    )


async def get_search_calls(run_id: str) -> int:
    """Read back the discovery search-query count (0 if never set, e.g. a
    re-run that skipped discovery)."""
    pool = get_pool()
    value = await pool.fetchval(
        "SELECT (metrics->>'search_calls')::int FROM runs WHERE id = $1::uuid",
        run_id,
    )
    return int(value or 0)


async def delete_run(run_id: str, user_id: str) -> bool:
    """Delete a run the user owns (cascades to sources/candidates/claims).
    Returns True if a row was deleted."""
    pool = get_pool()
    result = await pool.execute(
        "DELETE FROM runs WHERE id = $1::uuid AND user_id = $2", run_id, user_id
    )
    return result.rsplit(" ", 1)[-1] != "0"  # "DELETE N"


async def _run_row(run_id: str, user_id: str) -> Any:
    pool = get_pool()
    return await pool.fetchrow(
        """
        SELECT id::text AS id, status, competitors, topics, input_urls, context,
               discovery_skipped, created_at, completed_at, error_detail, metrics
        FROM runs
        WHERE id = $1::uuid AND user_id = $2
        """,
        run_id,
        user_id,
    )


async def get_run_summary(run_id: str, user_id: str) -> RunSummary | None:
    row = await _run_row(run_id, user_id)
    if row is None:
        return None
    return RunSummary(
        id=row["id"],
        status=row["status"],
        competitors=list(row["competitors"]),
        topics=list(row["topics"]),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


async def list_runs(user_id: str, *, limit: int = 50) -> list[RunSummary]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id::text AS id, status, competitors, topics, created_at, completed_at
        FROM runs
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [
        RunSummary(
            id=r["id"],
            status=r["status"],
            competitors=list(r["competitors"]),
            topics=list(r["topics"]),
            created_at=r["created_at"],
            completed_at=r["completed_at"],
        )
        for r in rows
    ]


async def get_run_detail(run_id: str, user_id: str) -> RunDetail | None:
    row = await _run_row(run_id, user_id)
    if row is None:
        return None
    sources = await get_run_sources(run_id)
    report = await build_report(run_id)
    metrics = RunMetrics(**json.loads(row["metrics"])) if row["metrics"] else None
    return RunDetail(
        id=row["id"],
        status=row["status"],
        competitors=list(row["competitors"]),
        topics=list(row["topics"]),
        input_urls=list(row["input_urls"]),
        context=row["context"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        error_detail=row["error_detail"],
        sources=sources,
        report=report,
        metrics=metrics,
    )


async def get_run_owner_status(run_id: str, user_id: str) -> str | None:
    """Returns the run's status if owned by user_id, else None. Cheap
    ownership+state check for the approve/start/source endpoints."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT status FROM runs WHERE id = $1::uuid AND user_id = $2",
        run_id,
        user_id,
    )
    return row["status"] if row else None


async def get_run_inputs(run_id: str) -> tuple[list[str], list[str], list[str], str | None]:
    """(competitors, topics, input_urls, context) — used by the pipeline."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT competitors, topics, input_urls, context FROM runs WHERE id = $1::uuid",
        run_id,
    )
    if row is None:
        return [], [], [], None
    return list(row["competitors"]), list(row["topics"]), list(row["input_urls"]), row["context"]


# ------------------------------------------------- discovery_candidates ----


async def insert_discovery_candidates(
    run_id: str, candidates: list[DiscoveryCandidate]
) -> list[DiscoveryCandidate]:
    if not candidates:
        return []
    pool = get_pool()
    stored: list[DiscoveryCandidate] = []
    async with pool.acquire() as conn:
        for c in candidates:
            row = await conn.fetchrow(
                """
                INSERT INTO discovery_candidates
                    (run_id, url, domain, page_title, rationale, competitor, ssrf_safe)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
                RETURNING id::text AS id
                """,
                run_id,
                c.url,
                c.domain,
                c.page_title,
                c.rationale,
                c.competitor,
                c.ssrf_safe,
            )
            stored.append(c.model_copy(update={"id": row["id"]}))
    return stored


async def get_candidates(run_id: str, *, only_safe: bool = True) -> list[DiscoveryCandidate]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id::text AS id, url, domain, page_title, rationale, competitor,
               ssrf_safe, decision
        FROM discovery_candidates
        WHERE run_id = $1::uuid AND ($2 = false OR ssrf_safe = true)
        ORDER BY created_at
        """,
        run_id,
        only_safe,
    )
    return [
        DiscoveryCandidate(
            id=r["id"],
            url=r["url"],
            domain=r["domain"],
            page_title=r["page_title"],
            rationale=r["rationale"],
            competitor=r["competitor"],
            ssrf_safe=r["ssrf_safe"],
            decision=r["decision"],
        )
        for r in rows
    ]


async def set_candidate_decisions(run_id: str, decisions: dict[str, bool]) -> None:
    """decisions maps candidate_id -> approved(bool)."""
    if not decisions:
        return
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        for candidate_id, approved in decisions.items():
            await conn.execute(
                """
                UPDATE discovery_candidates
                SET decision = $3
                WHERE id = $1::uuid AND run_id = $2::uuid
                """,
                candidate_id,
                run_id,
                "APPROVED" if approved else "REJECTED",
            )


async def get_approved_candidate_urls(run_id: str) -> list[str]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT url FROM discovery_candidates
        WHERE run_id = $1::uuid AND decision = 'APPROVED' AND ssrf_safe = true
        """,
        run_id,
    )
    return [r["url"] for r in rows]


async def copy_approved_candidates(old_run_id: str, new_run_id: str) -> int:
    """Copy an old run's APPROVED discovery candidates into a new run (already
    approved), so a re-run reuses the discovered sources instead of running
    discovery again. Returns how many were copied."""
    pool = get_pool()
    result = await pool.execute(
        """
        INSERT INTO discovery_candidates
            (run_id, url, domain, page_title, rationale, competitor, ssrf_safe, decision)
        SELECT $2::uuid, url, domain, page_title, rationale, competitor, ssrf_safe, 'APPROVED'
        FROM discovery_candidates
        WHERE run_id = $1::uuid AND decision = 'APPROVED' AND ssrf_safe = true
        """,
        old_run_id,
        new_run_id,
    )
    return int(result.split()[-1]) if result else 0


# -------------------------------------------------------- run_sources ------


async def insert_run_source(
    *,
    run_id: str,
    url: str,
    status: str,
    source_origin: str,
    content_markdown: str | None = None,
    error: str | None = None,
) -> str:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO run_sources (run_id, url, status, source_origin, content_markdown, error)
        VALUES ($1::uuid, $2, $3, $4, $5, $6)
        RETURNING id::text AS id
        """,
        run_id,
        url,
        status,
        source_origin,
        content_markdown,
        error,
    )
    return row["id"]


async def update_run_source(
    source_id: str,
    *,
    status: str,
    content_markdown: str | None = None,
    error: str | None = None,
    source_origin: str | None = None,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE run_sources
        SET status = $2,
            content_markdown = COALESCE($3, content_markdown),
            error = $4,
            source_origin = COALESCE($5, source_origin)
        WHERE id = $1::uuid
        """,
        source_id,
        status,
        content_markdown,
        error,
        source_origin,
    )


async def get_run_sources(run_id: str) -> list[RunSource]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT url, status, error, source_origin
        FROM run_sources WHERE run_id = $1::uuid
        ORDER BY id
        """,
        run_id,
    )
    return [
        RunSource(
            url=r["url"],
            status=r["status"],
            error=r["error"],
            source_origin=r["source_origin"],
        )
        for r in rows
    ]


async def get_source_content(run_id: str) -> dict[str, str]:
    """Map of source_url -> content_markdown for every usable source in a
    run. This is the judge's source of truth."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT url, content_markdown
        FROM run_sources
        WHERE run_id = $1::uuid AND content_markdown IS NOT NULL
        """,
        run_id,
    )
    return {r["url"]: r["content_markdown"] for r in rows}


async def find_source_for_url(run_id: str, url: str) -> str | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id::text AS id FROM run_sources WHERE run_id = $1::uuid AND url = $2 LIMIT 1",
        run_id,
        url,
    )
    return row["id"] if row else None


# ------------------------------------------------------------- claims ------


async def replace_claims(
    run_id: str, claims: list[Claim], theme_summaries: dict[str, str] | None = None
) -> None:
    """Write the run's claims in one transaction (idempotent: clears any
    prior claims for the run first, so a re-run doesn't duplicate)."""
    summaries = theme_summaries or {}
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM claims WHERE run_id = $1::uuid", run_id)
        for c in claims:
            await conn.execute(
                """
                INSERT INTO claims
                    (run_id, theme, theme_summary, text, source_url, source_passage,
                     verified, confidence, verdict, judge_reason)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                run_id,
                c.theme,
                summaries.get(c.theme, ""),
                c.text,
                c.source_url,
                c.source_passage,
                c.verified,
                c.confidence,
                c.verdict,
                c.judge_reason,
            )


async def get_claims(run_id: str) -> list[Claim]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT theme, text, source_url, source_passage, verified,
               confidence, verdict, judge_reason
        FROM claims WHERE run_id = $1::uuid
        """,
        run_id,
    )
    return [
        Claim(
            theme=r["theme"], text=r["text"], source_url=r["source_url"],
            source_passage=r["source_passage"], verified=r["verified"],
            confidence=r["confidence"], verdict=r["verdict"], judge_reason=r["judge_reason"],
        )
        for r in rows
    ]


async def find_prior_run(
    user_id: str, run_id: str, competitors: list[str], topics: list[str]
) -> str | None:
    """The user's most recent COMPLETE run with the SAME competitors + topics,
    created before this one — the natural baseline for 'what's new'."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT id::text AS id FROM runs
        WHERE user_id = $1 AND status = 'COMPLETE' AND id <> $2::uuid
          AND competitors = $3::text[] AND topics = $4::text[]
          AND created_at < (SELECT created_at FROM runs WHERE id = $2::uuid)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        user_id,
        run_id,
        competitors,
        topics,
    )
    return row["id"] if row else None


async def build_report(run_id: str) -> ResearchReport | None:
    """Assemble the themed report from claim rows. Returns None if the run
    has produced no claims yet."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT theme, theme_summary, text, source_url, source_passage, verified,
               confidence, verdict, judge_reason
        FROM claims
        WHERE run_id = $1::uuid
        ORDER BY theme, confidence DESC
        """,
        run_id,
    )
    if not rows:
        return None

    by_theme: dict[str, list[Claim]] = {}
    theme_summary: dict[str, str] = {}
    for r in rows:
        claim = Claim(
            theme=r["theme"],
            text=r["text"],
            source_url=r["source_url"],
            source_passage=r["source_passage"],
            verified=r["verified"],
            confidence=r["confidence"],
            verdict=r["verdict"],
            judge_reason=r["judge_reason"],
        )
        by_theme.setdefault(r["theme"], []).append(claim)
        theme_summary.setdefault(r["theme"], r["theme_summary"] or "")

    themes = [
        Theme(title=title, summary=theme_summary.get(title, ""), claims=claims)
        for title, claims in by_theme.items()
    ]
    return ResearchReport(themes=themes)
