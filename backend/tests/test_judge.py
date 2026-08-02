from __future__ import annotations

import numpy as np

from app.config import Settings
from app.models.schemas import Claim
from app.pipeline.judge import _JudgeVerdict, judge_claims
from app.pipeline.retriever import SourceIndex
from tests.fakes import FakeChatProvider, FixedEmbeddingProvider


def _settings() -> Settings:
    return Settings(_env_file=None)  # judge_similarity_floor default 0.4


def _claim(source_url: str = "https://a.com") -> Claim:
    return Claim(
        theme="T", text="some claim", source_url=source_url,
        verified=False, confidence=0.0, verdict="low_confidence",
    )


def _index() -> dict[str, SourceIndex]:
    # two orthonormal chunk vectors
    return {
        "https://a.com": SourceIndex(
            url="https://a.com",
            chunks=["chunk one", "chunk two"],
            embeddings=np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32),
        )
    }


async def test_below_threshold_marks_low_confidence_without_calling_the_llm() -> None:
    judge = FakeChatProvider(parsed=_JudgeVerdict(verdict="supported", confidence=0.9, reason="x"))
    emb = FixedEmbeddingProvider([0, 0, 1])  # orthogonal to both chunks → cosine 0

    out = await judge_claims([_claim()], _index(), _settings(), judge, emb)

    assert out[0].verdict == "low_confidence"
    assert out[0].verified is False
    assert judge.calls == 0  # the gate skipped the LLM entirely


async def test_above_threshold_calls_llm_and_applies_verdict() -> None:
    judge = FakeChatProvider(parsed=_JudgeVerdict(verdict="supported", confidence=0.88, reason="grounded"))
    emb = FixedEmbeddingProvider([1, 0, 0])  # cosine 1.0 with chunk 0

    out = await judge_claims([_claim()], _index(), _settings(), judge, emb)

    assert judge.calls == 1
    assert out[0].verdict == "supported"
    assert out[0].verified is True
    assert out[0].confidence == 0.88
    assert out[0].judge_reason == "grounded"


async def test_claim_for_unknown_source_is_low_confidence_without_llm() -> None:
    judge = FakeChatProvider(parsed=_JudgeVerdict(verdict="supported", confidence=0.9, reason="x"))
    emb = FixedEmbeddingProvider([1, 0, 0])

    out = await judge_claims([_claim(source_url="https://missing.com")], _index(), _settings(), judge, emb)

    assert out[0].verdict == "low_confidence"
    assert judge.calls == 0
