"""Run lifecycle endpoints.

  POST /api/runs/discover        create a run + run source discovery
  GET  /api/runs                 list the user's runs
  GET  /api/runs/{id}            full run detail (with the report)
  GET  /api/runs/{id}/candidates discovery candidates for the approval gate
  POST /api/runs/{id}/approve    record approve/reject decisions
  POST /api/runs/{id}/start      run the pipeline, streaming progress over SSE

The SSE stream is a POST that returns text/event-stream and is authenticated
with the normal Clerk bearer dependency — the frontend consumes it via
fetch() + ReadableStream (not EventSource), so the JWT travels in the
Authorization header, never in the URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.changes import diff_claims
from app.config import Settings, get_settings
from app.core.auth import current_user_id
from app.core.rate_limit import limit_discovery, limit_runs
from app.db import repository as repo
from app.discovery import service as discovery
from app.models.schemas import (
    ApproveRequest,
    DiscoveryCandidate,
    RunChanges,
    RunDetail,
    RunRequest,
    RunSummary,
)
from app.pipeline import events, orchestrator
from app.pipeline.cancel import clear_cancel, is_cancelled, request_cancel

router = APIRouter(prefix="/api/runs", tags=["runs"])


class DiscoverResponse(BaseModel):
    run_id: str
    status: str
    discovery_skipped: bool
    candidates: list[DiscoveryCandidate]


class ApproveResponse(BaseModel):
    run_id: str
    status: str
    approved_count: int
    total_sources: int


class RerunResponse(BaseModel):
    run_id: str
    status: str
    reused_sources: int


@router.post("/discover", response_model=DiscoverResponse)
async def discover_sources(
    body: RunRequest,
    user_id: str = Depends(limit_discovery),
    settings: Settings = Depends(get_settings),
) -> DiscoverResponse:
    run_id = await repo.create_run(
        user_id=user_id,
        competitors=body.competitors,
        topics=body.topics,
        input_urls=[str(u) for u in body.urls],
        context=body.context,
    )
    await repo.set_run_status(run_id, "DISCOVERING")

    candidates = await discovery.discover(
        run_id, body.competitors, body.topics, [str(u) for u in body.urls], settings
    )
    # One search query per (competitor, angle) — the deterministic count of
    # search-API hits, recorded now so the cost panel can show it later.
    await repo.record_search_calls(
        run_id, len(discovery.build_queries(body.competitors, body.topics))
    )
    safe = [c for c in candidates if c.ssrf_safe]
    stored = await repo.insert_discovery_candidates(run_id, candidates)
    stored_safe = [c for c in stored if c.ssrf_safe]

    # If the user hit Stop while discovery ran, honour it instead of advancing.
    if is_cancelled(run_id):
        clear_cancel(run_id)
        return DiscoverResponse(
            run_id=run_id, status="CANCELLED", discovery_skipped=True, candidates=[]
        )

    discovery_skipped = len(safe) == 0
    await repo.set_run_status(run_id, "AWAITING_APPROVAL", discovery_skipped=discovery_skipped)
    return DiscoverResponse(
        run_id=run_id,
        status="AWAITING_APPROVAL",
        discovery_skipped=discovery_skipped,
        candidates=stored_safe,
    )


@router.get("", response_model=list[RunSummary])
async def list_runs(user_id: str = Depends(current_user_id)) -> list[RunSummary]:
    return await repo.list_runs(user_id)


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: str, user_id: str = Depends(current_user_id)) -> RunDetail:
    detail = await repo.get_run_detail(run_id, user_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return detail


@router.get("/{run_id}/changes", response_model=RunChanges)
async def get_changes(run_id: str, user_id: str = Depends(current_user_id)) -> RunChanges:
    """Diff this run against the user's previous run with the same inputs —
    'what's new since last run' (stretch goal). Non-blocking: returns
    has_previous=false if there's no comparable prior run."""
    if await repo.get_run_owner_status(run_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    competitors, topics, _, _ = await repo.get_run_inputs(run_id)
    prior_id = await repo.find_prior_run(user_id, run_id, competitors, topics)
    if prior_id is None:
        return RunChanges(has_previous=False)

    new_claims, changed = diff_claims(
        await repo.get_claims(run_id), await repo.get_claims(prior_id)
    )
    return RunChanges(
        has_previous=True, previous_run_id=prior_id, new_claims=new_claims, changed=changed
    )


@router.get("/{run_id}/candidates", response_model=list[DiscoveryCandidate])
async def get_candidates(
    run_id: str, user_id: str = Depends(current_user_id)
) -> list[DiscoveryCandidate]:
    if await repo.get_run_owner_status(run_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return await repo.get_candidates(run_id, only_safe=True)


@router.post("/{run_id}/approve", response_model=ApproveResponse)
async def approve(
    run_id: str,
    body: ApproveRequest,
    user_id: str = Depends(current_user_id),
) -> ApproveResponse:
    run_status = await repo.get_run_owner_status(run_id, user_id)
    if run_status is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if run_status != "AWAITING_APPROVAL":
        raise HTTPException(status.HTTP_409_CONFLICT, f"run is {run_status}, not awaiting approval")

    decisions = {d.candidate_id: d.approved for d in body.decisions}
    await repo.set_candidate_decisions(run_id, decisions)
    await repo.set_run_status(run_id, "PENDING")

    approved_urls = await repo.get_approved_candidate_urls(run_id)
    _, _, input_urls, _ = await repo.get_run_inputs(run_id)
    return ApproveResponse(
        run_id=run_id,
        status="PENDING",
        approved_count=len(approved_urls),
        total_sources=len(set(input_urls) | set(approved_urls)),
    )


@router.post("/{run_id}/rerun", response_model=RerunResponse)
async def rerun(
    run_id: str,
    user_id: str = Depends(current_user_id),
) -> RerunResponse:
    """Create a new run reusing an existing run's inputs AND its previously
    approved discovered sources, skipping the discovery phase. The new run
    is left PENDING, ready for POST /start."""
    if await repo.get_run_owner_status(run_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    competitors, topics, input_urls, context = await repo.get_run_inputs(run_id)
    new_id = await repo.create_run(
        user_id=user_id,
        competitors=competitors,
        topics=topics,
        input_urls=input_urls,
        context=context,
    )
    reused = await repo.copy_approved_candidates(run_id, new_id)
    await repo.set_run_status(new_id, "PENDING")  # skip discovery/approval
    return RerunResponse(run_id=new_id, status="PENDING", reused_sources=reused)


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, user_id: str = Depends(current_user_id)) -> dict[str, str]:
    """Stop a run that's still in progress. Flags it for the pipeline to bail at
    its next stage boundary and marks it CANCELLED. No-op if already finished."""
    run_status = await repo.get_run_owner_status(run_id, user_id)
    if run_status is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if run_status in ("COMPLETE", "FAILED", "CANCELLED"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"run already {run_status.lower()}")

    request_cancel(run_id)
    await repo.set_run_status(run_id, "CANCELLED", completed=True)
    return {"run_id": run_id, "status": "CANCELLED"}


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: str, user_id: str = Depends(current_user_id)) -> Response:
    if not await repo.delete_run(run_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{run_id}/start")
async def start(
    run_id: str,
    user_id: str = Depends(limit_runs),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    run_status = await repo.get_run_owner_status(run_id, user_id)
    if run_status is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if run_status not in ("PENDING", "AWAITING_APPROVAL"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"run is {run_status}, cannot start")

    async def factory(emit: events.Emit) -> None:
        await orchestrator.run_pipeline(run_id, settings, emit)

    return StreamingResponse(
        events.run_with_sse(factory, heartbeat_seconds=settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
