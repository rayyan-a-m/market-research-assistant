"""Pipeline orchestrator — runs Stages 0-4 for a run, emitting SSE events
and persisting the result. Invoked as the background task behind the
/runs/{id}/start SSE stream (see routers/runs.py + events.run_with_sse).

Providers come from the factory (resolved by role), so this module composes
the configured providers without importing any vendor SDK.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings
from app.core.costs import estimate_run_cost
from app.core.security import is_safe_url
from app.db import repository as repo
from app.models.schemas import Claim, DraftReport, RunMetrics
from app.pipeline import fetcher, judge, retriever, summarizer
from app.pipeline.cancel import clear_cancel, is_cancelled
from app.pipeline.events import Emit
from app.providers.factory import ProviderFactory
from app.providers.middleware import RunLedger

logger = logging.getLogger(__name__)


async def _aborted(run_id: str, emit: Emit) -> bool:
    """True if the run was cancelled — the endpoint already wrote CANCELLED to
    the DB, so we just stop and signal, leaving that status in place."""
    if is_cancelled(run_id):
        clear_cancel(run_id)
        await emit("cancelled", {"run_id": run_id, "status": "CANCELLED"})
        return True
    return False


def _normalize_url(url: str) -> str:
    """Fold the differences that don't change which document a URL names —
    scheme/host case and a trailing slash. Deliberately conservative: it
    resolves formatting noise, not a different path or query."""
    parsed = urlsplit(url.strip())
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def _draft_to_claims(
    report: DraftReport, allowed_urls: Iterable[str]
) -> tuple[list[Claim], dict[str, str], int]:
    """Flatten the summarizer's draft into interim Claim objects (with
    placeholder verdicts the judge will overwrite) + theme summaries.

    **Attribution is enforced here, structurally.** The prompt tells the model
    to copy a `source_url` verbatim from a `[Source:]` label and the schema
    constrains it to a string — but neither can stop the model from emitting a
    URL that was never fetched. Any claim citing a URL outside `allowed_urls`
    is dropped, not merely flagged.

    Why dropped rather than passed to the judge: the judge looks its index up
    by `claim.source_url`, so a fabricated URL misses and comes back
    `low_confidence` — the *same* outcome as a real source that happened to be
    thin. The two are not the same thing, and the fabricated one still reaches
    the UI as a clickable link to a page that never backed the claim. A free
    set-membership check separates them before that can happen, and is the
    cheapest rung of the guardrail ladder (see README).

    Returns the surviving claims, the summaries of themes that still have at
    least one claim, and the number dropped (recorded in run metrics so the
    drop is observable rather than silent).
    """
    canonical = {_normalize_url(u): u for u in allowed_urls}
    claims: list[Claim] = []
    theme_summaries: dict[str, str] = {}
    dropped = 0
    for theme in report.themes:
        kept_in_theme = 0
        for dc in theme.claims:
            source_url = canonical.get(_normalize_url(dc.source_url))
            if source_url is None:
                dropped += 1
                logger.warning(
                    "dropping claim citing an unfetched source: %r", dc.source_url
                )
                continue
            kept_in_theme += 1
            claims.append(Claim(
                theme=theme.title,
                text=dc.text,
                # store the canonical fetched URL, not the model's rendering of
                # it, so the report links to exactly what was retrieved
                source_url=source_url,
                source_passage=dc.source_passage,
                verified=False,
                confidence=0.0,
                verdict="low_confidence",
            ))
        if kept_in_theme:
            theme_summaries[theme.title] = theme.summary
    return claims, theme_summaries, dropped


async def _resolve_urls(run_id: str) -> list[tuple[str, str]]:
    """Build the (url, origin) fetch list: user-supplied + approved
    discovery, de-duplicated, SSRF-re-checked."""
    _, _, input_urls, _ = await repo.get_run_inputs(run_id)
    approved = await repo.get_approved_candidate_urls(run_id)
    seen: set[str] = set()
    url_origins: list[tuple[str, str]] = []
    for url in input_urls:
        if url not in seen and is_safe_url(url)[0]:
            seen.add(url)
            url_origins.append((url, "USER_SUPPLIED"))
    for url in approved:
        if url not in seen and is_safe_url(url)[0]:
            seen.add(url)
            url_origins.append((url, "USER_APPROVED_DISCOVERED"))
    return url_origins


async def run_pipeline(run_id: str, settings: Settings, emit: Emit) -> None:
    """Execute the research pipeline. Never raises — terminal state is always
    written to the run (COMPLETE or FAILED) and signalled over SSE."""
    started = time.monotonic()
    # One ledger per run: the shared state the metering layer writes and the
    # budget layer reads (providers/middleware.py). Per-run, not global, so
    # concurrent runs can't spend each other's budget.
    ledger = RunLedger()
    try:
        await repo.set_run_status(run_id, "PROCESSING")
        competitors, topics, _, guidance = await repo.get_run_inputs(run_id)

        url_origins = await _resolve_urls(run_id)
        if not url_origins:
            await _fail(run_id, emit, "no valid sources to research")
            return

        if await _aborted(run_id, emit):
            return

        # Stage 1 — fetch (pauses in place for unreachable sources)
        sources = await fetcher.fetch_sources(run_id, url_origins, settings, emit)
        if not sources:
            await _fail(run_id, emit, "no sources could be fetched")
            return
        if await _aborted(run_id, emit):
            return

        # Stage 2 — retrieve
        await emit("stage_start", {"stage": "PROCESSING", "sources": len(sources)})
        embedder = ProviderFactory.embedding(settings)
        indexes = await retriever.build_indexes(sources, settings, embedder)
        context = await retriever.build_context(topics, indexes, settings, embedder)
        if not context:
            await _fail(run_id, emit, "no usable content extracted from sources")
            return
        if await _aborted(run_id, emit):
            return

        # Stage 3 — summarize
        await emit("stage_start", {"stage": "SUMMARIZING"})
        draft = await summarizer.summarize(
            competitors=competitors, topics=topics, context=context, guidance=guidance,
            summarizer=ProviderFactory.summarizer(settings, ledger),
        )
        # `indexes` is keyed by the URLs that actually yielded retrievable
        # content, which is exactly the set a claim is allowed to cite.
        claims, theme_summaries, unattributed = _draft_to_claims(draft, indexes.keys())
        if unattributed:
            logger.warning(
                "run %s: dropped %d claim(s) citing unfetched sources", run_id, unattributed
            )
        if not claims:
            await _fail(run_id, emit, "summarizer produced no grounded claims")
            return

        if await _aborted(run_id, emit):
            return

        # Stage 4 — judge
        await emit("stage_start", {"stage": "JUDGING", "claims_total": len(claims)})
        judged = await judge.judge_claims(
            claims, indexes, settings, ProviderFactory.judge(settings, ledger), embedder
        )
        if await _aborted(run_id, emit):
            return

        await repo.replace_claims(run_id, judged, theme_summaries)
        verified = sum(1 for c in judged if c.verified)
        # Search hits were recorded at discovery time (a separate request); read
        # them back so the run's metrics carry the whole cost picture in one row.
        search_calls = await repo.get_search_calls(run_id)
        await repo.set_run_metrics(run_id, RunMetrics(
            duration_ms=int((time.monotonic() - started) * 1000),
            sources_fetched=len(sources),
            claims_total=len(judged),
            claims_verified=verified,
            themes=len(theme_summaries),
            summarizer_model=settings.summarizer_model,
            judge_model=settings.judge_model,
            input_tokens=ledger.input_tokens,
            output_tokens=ledger.output_tokens,
            llm_calls=ledger.calls,
            search_calls=search_calls,
            estimated_cost_usd=estimate_run_cost(ledger.per_call, search_calls),
            claims_dropped_unattributed=unattributed,
        ))
        await repo.set_run_status(run_id, "COMPLETE", completed=True)
        await emit("complete", {
            "run_id": run_id, "status": "COMPLETE",
            "claims_total": len(judged), "claims_verified": verified,
        })
    except Exception as exc:  # noqa: BLE001 — orchestrator is the top of the task; never crash silently
        logger.exception("pipeline failed for run %s", run_id)
        await _fail(run_id, emit, f"pipeline error: {exc}")


async def _fail(run_id: str, emit: Emit, detail: str) -> None:
    await repo.set_run_status(run_id, "FAILED", error_detail=detail, completed=True)
    await emit("error", {"run_id": run_id, "status": "FAILED", "detail": detail})
