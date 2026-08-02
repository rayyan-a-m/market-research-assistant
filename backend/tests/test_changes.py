from __future__ import annotations

from app.changes import diff_claims
from app.models.schemas import Claim, Verdict


def _claim(text: str, url: str, verdict: Verdict = "supported") -> Claim:
    return Claim(
        theme="T", text=text, source_url=url, verified=verdict == "supported",
        confidence=0.9, verdict=verdict,
    )


def test_new_claim_is_detected() -> None:
    prev = [_claim("Stripe launched X", "https://a.com")]
    curr = [
        _claim("Stripe launched X", "https://a.com"),
        _claim("Adyen acquired Y", "https://b.com"),  # brand new
    ]
    new, changed = diff_claims(curr, prev)
    assert [c.text for c in new] == ["Adyen acquired Y"]
    assert changed == []


def test_verdict_flip_is_detected_despite_rephrasing() -> None:
    prev = [_claim("Stripe launched Capital for Platforms", "https://a.com", "unsupported")]
    curr = [_claim("Stripe launched Capital for Platforms.", "https://a.com", "supported")]  # rephrase + flip
    new, changed = diff_claims(curr, prev)
    assert new == []
    assert len(changed) == 1
    assert changed[0].previous_verdict == "unsupported"
    assert changed[0].claim.verdict == "supported"


def test_same_text_different_source_is_new() -> None:
    prev = [_claim("Payment orchestration is growing", "https://a.com")]
    curr = [_claim("Payment orchestration is growing", "https://b.com")]  # different source
    new, changed = diff_claims(curr, prev)
    assert len(new) == 1
    assert changed == []


def test_unchanged_claim_yields_nothing() -> None:
    prev = [_claim("Stripe launched X", "https://a.com", "supported")]
    curr = [_claim("Stripe launched X", "https://a.com", "supported")]
    new, changed = diff_claims(curr, prev)
    assert new == []
    assert changed == []
