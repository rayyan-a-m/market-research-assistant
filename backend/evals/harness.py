"""Drives the real guardrail code over the eval dataset.

This calls the production functions — `_draft_to_claims` for the structural
rung and `judge.judge_claims` for the rest — rather than reimplementing their
logic. An eval that reimplements what it measures only ever tests itself.
"""

from __future__ import annotations

import numpy as np

from app.config import Settings
from app.models.schemas import Claim, DraftClaim, DraftReport, DraftTheme
from app.pipeline import judge as judge_stage
from app.pipeline.orchestrator import _draft_to_claims
from app.pipeline.retriever import SourceIndex, chunk_text, cosine_topk
from app.providers.base import ChatProvider, EmbeddingProvider
from evals.dataset import CLAIMS, SOURCES, EvalClaim
from evals.scorer import JudgeScore


async def build_eval_indexes(
    settings: Settings, embedder: EmbeddingProvider
) -> dict[str, SourceIndex]:
    """Chunk + embed the fixture sources the same way the retriever does."""
    indexes: dict[str, SourceIndex] = {}
    for source in SOURCES:
        chunks = chunk_text(
            source.markdown,
            size=settings.chunk_size_chars,
            overlap=settings.chunk_overlap_chars,
        )
        vectors = np.array(await embedder.embed(chunks), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        indexes[source.url] = SourceIndex(
            url=source.url, chunks=chunks, embeddings=vectors / norms
        )
    return indexes


async def score_similarities(
    settings: Settings, embedder: EmbeddingProvider, claims: list[EvalClaim]
) -> dict[str, float]:
    """Best source-similarity per claim — the raw number the floor thresholds.

    Exposed so a test can assert the *ranking* (faithful claims sit above
    topically-unrelated ones) rather than only a chosen cut-off. Ranking is
    the property that has to hold for any threshold to be meaningful, and it
    survives a change of embedding model; a specific floor value does not.
    """
    indexes = await build_eval_indexes(settings, embedder)
    scores: dict[str, float] = {}
    for claim in claims:
        index = indexes.get(claim.source_url)
        if index is None or index.embeddings.shape[0] == 0:
            scores[claim.text] = 0.0
            continue
        vector = np.array((await embedder.embed([claim.text]))[0], dtype=np.float32)
        top = cosine_topk(vector, index.embeddings, 3)
        scores[claim.text] = top[0][1] if top else 0.0
    return scores


def apply_structural_rung(claims: list[EvalClaim]) -> tuple[list[EvalClaim], list[EvalClaim]]:
    """Run the free attribution check over the dataset via the real
    `_draft_to_claims`. Returns (survived, dropped)."""
    draft = DraftReport(
        themes=[
            DraftTheme(
                title="eval",
                summary="eval",
                claims=[DraftClaim(text=c.text, source_url=c.source_url) for c in claims],
            )
        ]
    )
    kept, _, _ = _draft_to_claims(draft, [s.url for s in SOURCES])
    kept_texts = {c.text for c in kept}
    survived = [c for c in claims if c.text in kept_texts]
    dropped = [c for c in claims if c.text not in kept_texts]
    return survived, dropped


async def run_eval(
    *,
    settings: Settings,
    embedder: EmbeddingProvider,
    judge_provider: ChatProvider,
    claims: list[EvalClaim] | None = None,
) -> JudgeScore:
    """Score the full ladder: structural check, then similarity floor, then
    the LLM judge — exactly the order the pipeline applies them."""
    dataset = claims if claims is not None else CLAIMS
    score = JudgeScore()

    survived, dropped = apply_structural_rung(dataset)
    for claim in dropped:
        # A dropped claim never reaches the user, so it counts as caught, and
        # it cost nothing — the structural rung resolved it.
        score.record(
            claim, verified=False, used_llm=False, stage="structural",
            detail="dropped: unfetched source",
        )

    indexes = await build_eval_indexes(settings, embedder)
    interim = [
        Claim(
            theme="eval",
            text=c.text,
            source_url=c.source_url,
            verified=False,
            confidence=0.0,
            verdict="low_confidence",
        )
        for c in survived
    ]
    judged = await judge_stage.judge_claims(
        interim, indexes, settings, judge_provider, embedder
    )

    by_text = {c.text: c for c in judged}
    for claim in survived:
        result = by_text[claim.text]
        # `low_confidence` with no judge_reason from the model means the
        # similarity floor short-circuited before any LLM call.
        used_llm = result.judge_reason not in {
            "no closely-matching content found in the source document",
            "no retrievable content for the cited source",
        }
        score.record(
            claim,
            verified=result.verified,
            used_llm=used_llm,
            stage="llm_judge" if used_llm else "similarity_floor",
            detail=f"{result.verdict} ({result.confidence:.2f}): {result.judge_reason}",
        )
    return score
