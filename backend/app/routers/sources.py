"""Blocked-source recovery endpoints.

When the fetcher pauses on a source it can't reach (or that returned a login
wall), the user resolves the pause one of two ways:

  • paste — supply the article text; sanitized like scraped content, so the
    pipeline never knows or cares how a source arrived.
  • skip  — drop the source and let the run continue without it.

Both wake the paused fetch stage via resolve_waiter. (A PDF-upload endpoint
also exists for pasted-content parity, but the UI leads with paste/skip;
richer upload is tracked as a future enhancement.)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.core.auth import current_user_id
from app.core.sanitize import MAX_CONTENT_CHARS, sanitize_source_content
from app.db import repository as repo
from app.models.schemas import SourcePasteRequest, SourceSkipRequest
from app.pipeline.unreachable import resolve_waiter

router = APIRouter(prefix="/api/runs", tags=["sources"])

_MAX_PDF_BYTES = 10 * 1024 * 1024


async def _require_owned_run(run_id: str, user_id: str) -> None:
    if await repo.get_run_owner_status(run_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")


@router.post("/{run_id}/sources/paste")
async def paste_source(
    run_id: str,
    body: SourcePasteRequest,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    await _require_owned_run(run_id, user_id)
    clean = sanitize_source_content(body.content)
    await repo.update_run_source(
        body.source_id,
        status="OK_PASTED",
        content_markdown=clean,
        source_origin="USER_PASTED_TEXT",
    )
    resolve_waiter(body.source_id)
    return {"source_id": body.source_id, "status": "OK_PASTED", "char_count": len(clean)}


@router.post("/{run_id}/sources/skip")
async def skip_source(
    run_id: str,
    body: SourceSkipRequest,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    """Drop a blocked source and let the run continue without it. Marking the
    row happens in the fetcher once it wakes, so the decision and the state
    change stay in one place."""
    await _require_owned_run(run_id, user_id)
    resolved = resolve_waiter(body.source_id, skip=True)
    return {"source_id": body.source_id, "status": "SKIPPED_BY_USER", "resolved": resolved}


@router.post("/{run_id}/sources/upload")
async def upload_source(
    run_id: str,
    source_id: str = Form(...),
    file: UploadFile = File(...),
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    await _require_owned_run(run_id, user_id)
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file must be a PDF")

    data = await file.read()
    if len(data) > _MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PDF exceeds 10MB limit")

    text = _extract_pdf_text(data)
    if not text.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no text layer found (scanned PDF?) — try the paste option instead",
        )

    clean = sanitize_source_content(text, max_chars=MAX_CONTENT_CHARS)
    await repo.update_run_source(
        source_id,
        status="OK_PDF_UPLOAD",
        content_markdown=clean,
        source_origin="USER_UPLOADED_PDF",
    )
    resolve_waiter(source_id)
    return {"source_id": source_id, "status": "OK_PDF_UPLOAD", "char_count": len(clean)}


def _extract_pdf_text(data: bytes) -> str:
    # Lazy import: pdfplumber pulls in a heavy stack; only needed on upload.
    import io

    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts)
