from __future__ import annotations

from app.models.schemas import DraftClaim, DraftReport, DraftTheme
from app.pipeline.orchestrator import _draft_to_claims, _normalize_url

ALLOWED = ["https://a.com", "https://b.com"]


def test_draft_to_claims_flattens_themes_and_sets_placeholder_verdicts() -> None:
    draft = DraftReport(
        themes=[
            DraftTheme(
                title="Expansion",
                summary="Both expanded.",
                claims=[
                    DraftClaim(text="A launched X", source_url="https://a.com", source_passage="p"),
                    DraftClaim(text="B launched Y", source_url="https://b.com"),
                ],
            ),
            DraftTheme(
                title="Pricing",
                summary="Pricing shifts.",
                claims=[DraftClaim(text="A cut prices", source_url="https://a.com")],
            ),
        ]
    )
    claims, summaries, dropped = _draft_to_claims(draft, ALLOWED)

    assert [c.text for c in claims] == ["A launched X", "B launched Y", "A cut prices"]
    assert claims[0].theme == "Expansion"
    assert claims[2].theme == "Pricing"
    # verdict fields are placeholders the judge overwrites, not summarizer output
    assert all(
        c.verified is False and c.verdict == "low_confidence" and c.confidence == 0.0
        for c in claims
    )
    assert summaries == {"Expansion": "Both expanded.", "Pricing": "Pricing shifts."}
    assert dropped == 0


def test_draft_to_claims_empty() -> None:
    claims, summaries, dropped = _draft_to_claims(DraftReport(themes=[]), ALLOWED)
    assert claims == []
    assert summaries == {}
    assert dropped == 0


def test_draft_to_claims_drops_claim_citing_unfetched_source() -> None:
    """The core of the structural attribution check: a URL the model produced
    that was never fetched must not reach the report, where it would render as
    a clickable link to a page that never backed the claim."""
    draft = DraftReport(
        themes=[
            DraftTheme(
                title="Expansion",
                summary="Both expanded.",
                claims=[
                    DraftClaim(text="grounded", source_url="https://a.com"),
                    DraftClaim(text="fabricated", source_url="https://never-fetched.com/post"),
                ],
            )
        ]
    )
    claims, summaries, dropped = _draft_to_claims(draft, ALLOWED)

    assert [c.text for c in claims] == ["grounded"]
    assert dropped == 1
    assert summaries == {"Expansion": "Both expanded."}


def test_draft_to_claims_drops_theme_left_with_no_claims() -> None:
    """A theme whose every claim was fabricated shouldn't render as an empty
    card with a summary that nothing supports."""
    draft = DraftReport(
        themes=[
            DraftTheme(
                title="Real",
                summary="kept",
                claims=[DraftClaim(text="grounded", source_url="https://a.com")],
            ),
            DraftTheme(
                title="Invented",
                summary="dropped",
                claims=[DraftClaim(text="fabricated", source_url="https://nope.com")],
            ),
        ]
    )
    claims, summaries, dropped = _draft_to_claims(draft, ALLOWED)

    assert [c.text for c in claims] == ["grounded"]
    assert summaries == {"Real": "kept"}
    assert "Invented" not in summaries
    assert dropped == 1


def test_draft_to_claims_tolerates_formatting_noise_and_canonicalizes() -> None:
    """Trailing slash / host case differences name the same document, so the
    claim is kept — but stored under the URL that was actually fetched, so the
    judge's index lookup and the report's link both hit."""
    draft = DraftReport(
        themes=[
            DraftTheme(
                title="T",
                summary="s",
                claims=[
                    DraftClaim(text="c1", source_url="https://A.com/"),
                    DraftClaim(text="c2", source_url="https://b.com"),
                ],
            )
        ]
    )
    claims, _, dropped = _draft_to_claims(draft, ALLOWED)

    assert dropped == 0
    assert [c.source_url for c in claims] == ["https://a.com", "https://b.com"]


def test_normalize_url_does_not_fold_distinct_documents() -> None:
    """The normalization must not be so aggressive that it accepts a URL
    pointing at a different page."""
    assert _normalize_url("https://a.com/x") != _normalize_url("https://a.com/y")
    assert _normalize_url("https://a.com?q=1") != _normalize_url("https://a.com?q=2")
    assert _normalize_url("https://a.com") != _normalize_url("https://other.com")
