"""Evaluation of the grounding guardrails against the labelled dataset.

Two tiers (see `evals/__init__.py`):

- The deterministic tier runs in CI. It pairs the free rungs with a
  deliberately **credulous** judge — one that marks everything supported — so
  the score isolates what the cheap rungs catch *on their own*. Anything they
  catch is caught for zero cost and cannot regress silently behind a model
  change.
- The model tier (`pytest -m eval`) scores the real judge.
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.providers.base import ChatProvider, ChatResult, Message
from app.providers.factory import ProviderFactory
from evals.dataset import CLAIMS, claims_by_defect
from evals.embedding import LexicalEmbeddingProvider
from evals.harness import apply_structural_rung, run_eval, score_similarities
from evals.trajectory import build_trajectory


class CredulousJudge(ChatProvider):
    """Marks every claim supported with full confidence.

    A pessimal judge on purpose: whatever the ladder still catches with this
    underneath it was caught by a rung that costs nothing and cannot drift
    when the model changes.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        self.calls += 1
        assert response_model is not None
        return ChatResult(
            text="",
            parsed=response_model(verdict="supported", confidence=1.0, reason="credulous"),
            model="credulous",
        )


def _eval_settings(**overrides: object) -> Settings:
    # NB: Settings fields are aliased to env-var names, so overrides must use
    # the ALIAS. Passing `judge_similarity_floor=...` is silently dropped by
    # `extra="ignore"` and you get the production default instead — which is
    # how the first run of this harness quietly measured the wrong threshold.
    #
    # Smaller chunks than production: the fixture documents are short, and the
    # lexical embedder's cosine scores depend on chunk length, so this keeps
    # claim-to-chunk comparisons on a sane scale.
    #
    # The 0.35 floor is calibrated for THIS embedder on THIS dataset, not
    # copied from production. Measured on the fixtures, faithful claims score
    # 0.487-0.623 and topically-unrelated ones 0.190-0.225; 0.35 sits in the
    # middle of that gap. The number is an artefact of a bag-of-words proxy —
    # what the tests below assert on is the separation itself, which is the
    # part that transfers.
    base: dict[str, object] = {
        "CHUNK_SIZE_CHARS": 400,
        "CHUNK_OVERLAP_CHARS": 50,
        "JUDGE_SIMILARITY_FLOOR": 0.35,
        "JUDGE_CONCURRENCY": 4,
    }
    return Settings(_env_file=None, GEMINI_API_KEY="unused", **{**base, **overrides})


# --- Deterministic tier (runs in CI) -----------------------------------------


def test_structural_rung_drops_every_fabricated_url() -> None:
    """The one rung that must be perfect: it's exact set membership, so
    anything less than 100% is a bug, not a tuning question."""
    fabricated = claims_by_defect("fabricated_url")
    assert fabricated, "dataset should plant at least one fabricated URL"

    survived, dropped = apply_structural_rung(CLAIMS)

    assert {c.text for c in dropped} == {c.text for c in fabricated}
    assert all(c.defect != "fabricated_url" for c in survived)


async def test_free_rungs_catch_what_they_should_without_an_llm_call() -> None:
    judge = CredulousJudge()
    score = await run_eval(
        settings=_eval_settings(),
        embedder=LexicalEmbeddingProvider(),
        judge_provider=judge,
        claims=CLAIMS,
    )
    print(score.format_report("deterministic tier (credulous judge)"))

    # Fabricated URLs and off-topic claims are structural/threshold decisions,
    # so the cheap rungs must get them right with no model involved at all.
    assert score.caught["fabricated_url"] == score.seen["fabricated_url"]
    assert score.caught["off_topic"] == score.seen["off_topic"]

    # ...and they must not achieve that by rejecting everything: with a judge
    # that says yes to all, every faithful claim should still pass.
    assert score.faithful_pass_rate == 1.0, (
        "the free rungs are rejecting claims that are actually grounded"
    )


async def test_faithful_claims_outrank_topically_unrelated_ones() -> None:
    """Threshold-independent, and therefore the durable assertion.

    A floor only means something if grounded claims genuinely score above
    ungrounded ones — if the two populations overlap, no cut-off can separate
    them and tuning the number is just moving the errors around. This asserts
    the separation directly, so it stays valid if the embedding model changes
    even though the calibrated 0.35 would not.
    """
    settings = _eval_settings()
    embedder = LexicalEmbeddingProvider()
    scored = await score_similarities(settings, embedder, CLAIMS)

    faithful = [scored[c.text] for c in CLAIMS if c.defect == "none"]
    unrelated = [
        scored[c.text] for c in CLAIMS if c.defect in {"off_topic", "cross_source"}
    ]

    assert min(faithful) > max(unrelated), (
        f"no threshold can separate these: lowest faithful {min(faithful):.3f} "
        f"is not above highest unrelated {max(unrelated):.3f}"
    )
    # And the configured floor should actually sit inside that gap.
    assert max(unrelated) < settings.judge_similarity_floor < min(faithful)


async def test_similarity_floor_resolves_off_topic_without_paying_the_judge() -> None:
    """The cost argument for the floor, measured rather than asserted in prose.

    The absolute floor value here is calibrated for the lexical embedder, not
    the production one — what transfers is the *behaviour*: an off-topic claim
    scores below the floor and short-circuits. Calibrating the real threshold
    is the model tier's job.
    """
    judge = CredulousJudge()
    off_topic = claims_by_defect("off_topic")
    score = await run_eval(
        settings=_eval_settings(),
        embedder=LexicalEmbeddingProvider(),
        judge_provider=judge,
        claims=off_topic,
    )

    assert score.llm_calls == 0
    assert judge.calls == 0
    assert score.resolved_without_llm == len(off_topic)


async def test_semantic_defects_are_not_caught_by_the_free_rungs() -> None:
    """Two things at once, both worth knowing.

    First, the harness can actually fail — an eval whose score can only go up
    is decoration. A contradicted claim uses the source's own vocabulary, so
    it sails past the similarity floor; with a credulous judge underneath,
    recall on it must read 0%.

    Second, that is the honest statement of what the cheap rungs are for.
    They catch *structural* and *topical* defects for free. Detecting that a
    claim says the opposite of its source is genuinely a language-understanding
    problem, and no threshold substitutes for the model tier.
    """
    contradicted = claims_by_defect("contradicted")
    assert contradicted

    judge = CredulousJudge()
    score = await run_eval(
        settings=_eval_settings(),
        embedder=LexicalEmbeddingProvider(),
        judge_provider=judge,
        claims=contradicted,
    )

    assert score.hallucination_recall == 0.0
    assert judge.calls == len(contradicted), "these must reach the judge, not short-circuit"


# --- Trajectory tier (runs in CI) --------------------------------------------


async def test_trajectory_routes_each_defect_to_the_cheapest_correct_rung() -> None:
    """Trajectory evaluation: every planted defect is resolved at the rung the
    ladder's design intends, and only fabricated URLs are resolved by the free
    structural check.

    Runs under the credulous judge because *routing* is independent of the
    verdict — which rung a claim reaches is deterministic, so this holds in CI
    without a live model.
    """
    score = await run_eval(
        settings=_eval_settings(),
        embedder=LexicalEmbeddingProvider(),
        judge_provider=CredulousJudge(),
        claims=CLAIMS,
    )
    report = build_trajectory(score)
    print(report.format_report("deterministic tier — trajectory"))

    # The never-bends invariant: structural is exact set membership, so nothing
    # but a fabricated URL may be resolved there.
    structural_defects = {c.defect for c in CLAIMS if score.stage_of.get(c.text) == "structural"}
    assert structural_defects == {"fabricated_url"}

    # Confident, individually-tested routings, re-expressed as a trajectory:
    for claim in CLAIMS:
        stage = score.stage_of[claim.text]
        if claim.defect == "off_topic":
            assert stage == "similarity_floor", f"{claim.text!r} left the free path"
        elif claim.defect == "contradicted":
            assert stage == "llm_judge", f"{claim.text!r} short-circuited before the judge"

    # Every rung is exercised — an eval that never reaches a stage isn't testing it.
    assert report.by_stage.get("structural", 0) > 0
    assert report.by_stage.get("similarity_floor", 0) > 0
    assert report.by_stage.get("llm_judge", 0) > 0

    # And the whole dataset stays on its expected path.
    assert report.match_rate == 1.0, report.format_report("FAILED")


# --- Model tier (pytest -m eval) ---------------------------------------------

#: Floors, not targets. Set below current measured performance so the suite
#: fails on a real regression rather than on model-to-model noise.
MIN_HALLUCINATION_RECALL = 0.70
MIN_FAITHFUL_PASS_RATE = 0.60


@pytest.mark.eval
async def test_live_judge_meets_the_quality_floor() -> None:
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")

    settings = Settings()  # real config: real models, real floor
    score = await run_eval(
        settings=settings,
        embedder=ProviderFactory.embedding(settings),
        judge_provider=ProviderFactory.judge(settings),
        claims=CLAIMS,
    )
    print(score.format_report(f"model tier — judge={settings.judge_model}"))

    assert score.hallucination_recall >= MIN_HALLUCINATION_RECALL, score.format_report("FAILED")
    assert score.faithful_pass_rate >= MIN_FAITHFUL_PASS_RATE, score.format_report("FAILED")
