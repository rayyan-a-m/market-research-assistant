"""Change detection — diff a run's claims against a previous comparable run.

Pure logic (no DB), so it's unit-testable. Matching claims across runs is the
hard part: the LLM rephrases, so exact text match is useless. Two claims are
treated as "the same claim" when they cite the **same source_url** and their
normalized text is Jaro-Winkler-similar above a threshold — the same
near-duplicate technique used in discovery dedup. From that matching we derive:
  • new_claims — claims in the current run with no match in the previous one
  • changed    — matched claims whose verdict flipped (e.g. unsupported → supported)
"""

from __future__ import annotations

import jellyfish

from app.models.schemas import Claim, ClaimChange


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def diff_claims(
    current: list[Claim], previous: list[Claim], *, threshold: float = 0.85
) -> tuple[list[Claim], list[ClaimChange]]:
    new_claims: list[Claim] = []
    changed: list[ClaimChange] = []
    used: set[int] = set()

    for c in current:
        c_norm = _norm(c.text)
        match_idx: int | None = None
        best = threshold
        for i, p in enumerate(previous):
            if i in used or p.source_url != c.source_url:
                continue
            score = jellyfish.jaro_winkler_similarity(c_norm, _norm(p.text))
            if score >= best:
                best = score
                match_idx = i
        if match_idx is None:
            new_claims.append(c)
        else:
            used.add(match_idx)
            if previous[match_idx].verdict != c.verdict:
                changed.append(ClaimChange(claim=c, previous_verdict=previous[match_idx].verdict))

    return new_claims, changed
