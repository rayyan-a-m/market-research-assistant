"""Trajectory evaluation — *where* in the guardrail ladder each defect is
resolved, not just whether it was.

The judge score answers "did the ladder catch this?". The trajectory answers
"did it catch it at the cheapest rung that should have?". That second question
is the whole economic argument for the ladder: a structural defect resolved by
the model instead of by free set-membership is a correctness win but a cost
regression, and nothing in the plain recall number would show it.

`EXPECTED_STAGES` encodes the ladder's design intent per planted defect:

  structural       — a cited URL that was never fetched. Exact set membership,
                     so it must be caught here and nowhere else.
  similarity_floor — no chunk of the cited source is close enough. Caught by the
                     cosine floor with no LLM call.
  llm_judge        — on-topic enough to clear the floor, so only a language
                     model can see the defect (a contradiction reuses the
                     source's own vocabulary and always lands here).

Whether a defect is caught at the floor or the judge depends on how much source
vocabulary it carries, and that depends on the embedder — an `overstated` claim
that dips just below the floor is *caught for free*, which is a cost win, not a
correctness problem (faithful claims are separately guarded against false floor
rejection by `test_faithful_claims_outrank...`). So the borderline semantic
defects map to a set of acceptable rungs. Two invariants never bend: only a
`fabricated_url` may be resolved at the structural rung, and a `contradicted`
claim must reach the judge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.dataset import CLAIMS, Defect, EvalClaim
from evals.scorer import JudgeScore

Stage = str  # "structural" | "similarity_floor" | "llm_judge"

EXPECTED_STAGES: dict[Defect, set[Stage]] = {
    "none": {"llm_judge"},                        # faithful → must reach the judge to be verified
    "fabricated_url": {"structural"},             # the one exact, must-be-here rung
    "off_topic": {"similarity_floor"},            # below the floor, no LLM
    "cross_source": {"similarity_floor", "llm_judge"},
    "unsupported": {"similarity_floor", "llm_judge"},
    "overstated": {"similarity_floor", "llm_judge"},  # free catch if it dips below the floor
    "contradicted": {"llm_judge"},                # uses the source's own vocabulary
}


@dataclass
class TrajectoryReport:
    """Per-claim stage attribution plus aggregates."""

    #: (claim text, defect, actual stage, matched expectation)
    rows: list[tuple[str, Defect, Stage, bool]] = field(default_factory=list)
    #: stage -> number of claims resolved there
    by_stage: dict[Stage, int] = field(default_factory=dict)

    @property
    def match_rate(self) -> float:
        if not self.rows:
            return 1.0
        return sum(1 for *_, ok in self.rows if ok) / len(self.rows)

    @property
    def mismatches(self) -> list[tuple[str, Defect, Stage]]:
        return [(text, defect, stage) for text, defect, stage, ok in self.rows if not ok]

    def format_report(self, title: str) -> str:
        lines = [
            f"\n=== {title} ===",
            f"trajectory match rate : {self.match_rate:.0%} "
            f"({sum(1 for *_, ok in self.rows if ok)}/{len(self.rows)})",
            "",
            "resolved by rung:",
        ]
        for stage in ("structural", "similarity_floor", "llm_judge"):
            lines.append(f"  {stage:<16} {self.by_stage.get(stage, 0)}")
        if self.mismatches:
            lines.append("")
            lines.append("off the expected path:")
            for text, defect, stage in self.mismatches:
                expected = ", ".join(sorted(EXPECTED_STAGES[defect]))
                lines.append(f"  - [{defect}] resolved at {stage}, expected {expected}: {text[:50]}…")
        return "\n".join(lines)


def build_trajectory(score: JudgeScore, claims: list[EvalClaim] | None = None) -> TrajectoryReport:
    """Turn a scored pass into a trajectory report by pairing each claim's
    planted defect with the rung that actually resolved it (`score.stage_of`)."""
    dataset = claims if claims is not None else CLAIMS
    report = TrajectoryReport()
    for claim in dataset:
        stage = score.stage_of.get(claim.text, "llm_judge")
        matched = stage in EXPECTED_STAGES[claim.defect]
        report.rows.append((claim.text, claim.defect, stage, matched))
        report.by_stage[stage] = report.by_stage.get(stage, 0) + 1
    return report
