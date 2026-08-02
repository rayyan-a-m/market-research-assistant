"""Stage 4 — hallucination judge.

Each claim is evaluated against the top actual chunks of its *original
source* — reusing the per-source embeddings the retriever already built, not
the passage the summarizer generated. That distinction is the whole point: a
summarizer that invents a supporting quote would have that quote confirm the
claim, so grading against the source is the only version that tests
grounding rather than internal consistency.

A claim whose best source-similarity is below the floor is marked
`low_confidence` without spending an LLM call. If the judge provider is
unavailable, the claim is marked unverified and the run continues.

The judge model is resolved by role from the factory, so which model grades
the summarizer's output is a config value rather than a call site here (see
ProviderFactory.judge). It defaults to a second, cheaper Gemini model.

That same `except ProviderError` path is what makes the run token budget
degrade gracefully rather than fail: `BudgetExceededError` is a
`ProviderError`, so a run that exhausts its budget mid-verification finishes
with its remaining claims marked unverified instead of erroring out. One
handler, two failure modes, because both mean the same thing to this stage.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from pydantic import BaseModel, Field

from app.config import Settings
from app.core.errors import ProviderError
from app.models.schemas import Claim, Verdict
from app.pipeline.retriever import SourceIndex, cosine_topk
from app.providers.base import ChatProvider, EmbeddingProvider, Message

logger = logging.getLogger(__name__)


class _JudgeVerdict(BaseModel):
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


_SYSTEM_PROMPT = """You verify whether a claim is supported by source passages.
You are given a CLAIM and the actual PASSAGES retrieved from the claim's cited
source document. Decide, using only the passages, whether the claim is:
- supported: the passages clearly state or directly imply the claim
- partial: the passages partly support it but overstate or omit key detail
- unsupported: the passages do not support the claim
Return a calibrated confidence (0-1) and a one-sentence reason. Judge only
against the passages provided; do not use outside knowledge."""


def _verdict_to_verified(verdict: Verdict) -> bool:
    return verdict == "supported"


async def _judge_one(
    claim: Claim,
    indexes: dict[str, SourceIndex],
    settings: Settings,
    judge: ChatProvider,
    embedder: EmbeddingProvider,
    sem: asyncio.Semaphore,
) -> Claim:
    index = indexes.get(claim.source_url)
    if index is None or index.embeddings.shape[0] == 0:
        return claim.model_copy(update={
            "verified": False, "confidence": 0.0, "verdict": "low_confidence",
            "judge_reason": "no retrievable content for the cited source",
        })

    claim_vec = np.array((await embedder.embed([claim.text]))[0], dtype=np.float32)
    top = cosine_topk(claim_vec, index.embeddings, 3)
    best = top[0][1] if top else 0.0
    if best < settings.judge_similarity_floor:
        return claim.model_copy(update={
            "verified": False, "confidence": round(best, 3), "verdict": "low_confidence",
            "judge_reason": "no closely-matching content found in the source document",
        })

    passages = "\n\n".join(index.chunks[i] for i, _ in top)
    user = f"CLAIM:\n{claim.text}\n\nPASSAGES (from {claim.source_url}):\n{passages}"

    try:
        async with sem:
            result = await judge.complete(
                system=_SYSTEM_PROMPT,
                messages=[Message(role="user", content=user)],
                response_model=_JudgeVerdict,
                max_tokens=512,
            )
    except ProviderError as exc:
        logger.warning("judge unavailable for claim (%s); marking unverified", exc)
        return claim.model_copy(update={
            "verified": False, "confidence": 0.0, "verdict": "low_confidence",
            "judge_reason": "judge unavailable — claim left unverified",
        })

    verdict = result.parsed
    if verdict is None:
        return claim.model_copy(update={
            "verified": False, "confidence": 0.0, "verdict": "low_confidence",
            "judge_reason": "judge returned no parseable verdict",
        })
    return claim.model_copy(update={
        "verified": _verdict_to_verified(verdict.verdict),
        "confidence": verdict.confidence,
        "verdict": verdict.verdict,
        "judge_reason": verdict.reason,
    })


async def judge_claims(
    claims: list[Claim],
    indexes: dict[str, SourceIndex],
    settings: Settings,
    judge: ChatProvider,
    embedder: EmbeddingProvider,
) -> list[Claim]:
    sem = asyncio.Semaphore(settings.judge_concurrency)
    results = await asyncio.gather(
        *(_judge_one(c, indexes, settings, judge, embedder, sem) for c in claims),
        return_exceptions=True,
    )
    judged: list[Claim] = []
    for original, outcome in zip(claims, results, strict=True):
        if isinstance(outcome, Claim):
            judged.append(outcome)
        else:  # unexpected error — degrade to unverified, don't drop the claim
            logger.warning("judge task errored: %s", outcome)
            judged.append(original.model_copy(update={
                "verified": False, "confidence": 0.0, "verdict": "low_confidence",
                "judge_reason": "verification error",
            }))
    return judged
