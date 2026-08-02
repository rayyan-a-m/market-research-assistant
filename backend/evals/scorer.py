"""Scoring for the judge evaluation.

The metric that matters is deliberately asymmetric, because the two errors
are not equally bad:

- A **missed hallucination** ships an unsupported claim to the user wearing a
  "Supported" badge. That is the failure the whole verification stage exists
  to prevent, and it damages the thing the product sells — trust in the
  report.
- **Over-strictness** marks a true claim unverified. The claim still appears,
  labelled honestly as unverified; the user sees a weaker report, not a wrong
  one.

So `hallucination_recall` is the headline number and is what a regression
threshold should be set on. `faithful_pass_rate` is tracked alongside it to
catch the degenerate way to score a perfect recall: mark everything
unverified. A change that improves one while collapsing the other has not
improved anything.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from evals.dataset import Defect, EvalClaim


@dataclass
class JudgeScore:
    """Outcome counts over one pass of the dataset."""

    #: defect -> number of claims of that kind seen
    seen: dict[Defect, int] = field(default_factory=lambda: defaultdict(int))
    #: defect -> number the guardrails correctly refused to verify
    caught: dict[Defect, int] = field(default_factory=lambda: defaultdict(int))
    #: claims labelled faithful that were correctly verified
    faithful_verified: int = 0
    faithful_total: int = 0
    #: claims the guardrails resolved without spending an LLM call
    resolved_without_llm: int = 0
    llm_calls: int = 0
    #: claim text -> the ladder rung that resolved it ("structural",
    #: "similarity_floor", "llm_judge"). The raw signal the trajectory eval
    #: reads to check each defect is routed to the cheapest correct rung.
    stage_of: dict[str, str] = field(default_factory=dict)
    #: rung -> number of hallucinations caught there
    caught_at: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: (claim text, what happened) for every disagreement, so a failing run
    #: says *which* claim regressed rather than just moving a number
    failures: list[tuple[str, str]] = field(default_factory=list)

    def record(
        self, claim: EvalClaim, *, verified: bool, used_llm: bool, stage: str, detail: str
    ) -> None:
        self.seen[claim.defect] += 1
        self.stage_of[claim.text] = stage
        if used_llm:
            self.llm_calls += 1
        else:
            self.resolved_without_llm += 1
        if not claim.should_verify and not verified:
            self.caught_at[stage] += 1

        if claim.should_verify:
            self.faithful_total += 1
            if verified:
                self.faithful_verified += 1
            else:
                self.failures.append((claim.text, f"faithful claim not verified — {detail}"))
        else:
            if verified:
                self.failures.append(
                    (claim.text, f"{claim.defect} claim marked VERIFIED — {detail}")
                )
            else:
                self.caught[claim.defect] += 1

    @property
    def hallucination_total(self) -> int:
        return sum(n for defect, n in self.seen.items() if defect != "none")

    @property
    def hallucination_caught(self) -> int:
        return sum(n for defect, n in self.caught.items() if defect != "none")

    @property
    def hallucination_recall(self) -> float:
        """Of the claims that should NOT be verified, the fraction that
        weren't. The headline number."""
        if self.hallucination_total == 0:
            return 1.0
        return self.hallucination_caught / self.hallucination_total

    @property
    def faithful_pass_rate(self) -> float:
        """Of the claims that SHOULD be verified, the fraction that were.
        Guards against gaming recall by rejecting everything."""
        if self.faithful_total == 0:
            return 1.0
        return self.faithful_verified / self.faithful_total

    def format_report(self, title: str) -> str:
        lines = [
            f"\n=== {title} ===",
            f"hallucination recall : {self.hallucination_recall:.0%} "
            f"({self.hallucination_caught}/{self.hallucination_total} caught)",
            f"faithful pass rate   : {self.faithful_pass_rate:.0%} "
            f"({self.faithful_verified}/{self.faithful_total} verified)",
            f"llm calls            : {self.llm_calls} "
            f"({self.resolved_without_llm} resolved by cheaper rungs)",
            "",
            "by defect:",
        ]
        for defect in sorted(self.seen):
            if defect == "none":
                continue
            lines.append(f"  {defect:<16} {self.caught[defect]}/{self.seen[defect]} caught")
        if self.failures:
            lines.append("")
            lines.append("disagreements:")
            lines.extend(f"  - {text[:70]}… : {why}" for text, why in self.failures)
        return "\n".join(lines)
