from __future__ import annotations

from app.discovery.dedup import deduplicate, normalize_url
from app.models.schemas import DiscoveryCandidate


def _cand(url: str, title: str) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        id="", url=url, domain="d", page_title=title, rationale="r", competitor="c", ssrf_safe=True
    )


def test_normalize_url_strips_scheme_www_and_trailing_slash() -> None:
    assert normalize_url("https://www.example.com/path/") == "example.com/path"
    assert normalize_url("http://example.com/path") == "example.com/path"
    assert normalize_url("https://example.com/path/") == normalize_url("http://www.example.com/path")


def test_deduplicate_drops_urls_already_supplied_by_user() -> None:
    cands = [_cand("https://example.com/a", "Article A")]
    kept = deduplicate(cands, ["https://www.example.com/a/"], title_threshold=0.92)
    assert kept == []


def test_deduplicate_drops_exact_url_duplicates() -> None:
    cands = [_cand("https://example.com/a", "A"), _cand("https://www.example.com/a/", "A2")]
    kept = deduplicate(cands, [], title_threshold=0.92)
    assert len(kept) == 1


def test_deduplicate_drops_near_duplicate_titles() -> None:
    cands = [
        _cand("https://site1.com/x", "Stripe launches Capital for Platforms"),
        _cand("https://site2.com/y", "Stripe launches Capital for Platforms"),  # syndicated
    ]
    kept = deduplicate(cands, [], title_threshold=0.92)
    assert len(kept) == 1


def test_deduplicate_keeps_distinct_titles_and_urls() -> None:
    cands = [
        _cand("https://site1.com/x", "Stripe launches Capital"),
        _cand("https://site2.com/y", "Adyen expands embedded finance"),
    ]
    kept = deduplicate(cands, [], title_threshold=0.92)
    assert len(kept) == 2
