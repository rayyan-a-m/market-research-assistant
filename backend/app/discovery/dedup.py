"""Deduplication for discovery candidates.

Two passes:
  1. exact URL match — normalize (drop scheme, trailing slash, common
     tracking query params) and drop URLs already supplied by the user or
     already seen among candidates.
  2. near-duplicate title — syndicated articles reprint the same headline on
     many domains; Jaro-Winkler over normalized titles catches them.
"""

from __future__ import annotations

from urllib.parse import urlparse

import jellyfish

from app.models.schemas import DiscoveryCandidate


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def _normalize_title(title: str | None) -> str:
    return " ".join((title or "").lower().split())


def deduplicate(
    candidates: list[DiscoveryCandidate],
    input_urls: list[str],
    *,
    title_threshold: float,
) -> list[DiscoveryCandidate]:
    seen_urls = {normalize_url(u) for u in input_urls}
    kept: list[DiscoveryCandidate] = []
    kept_titles: list[str] = []

    for cand in candidates:
        norm = normalize_url(cand.url)
        if norm in seen_urls:
            continue

        title = _normalize_title(cand.page_title)
        if title and any(
            jellyfish.jaro_winkler_similarity(title, kt) >= title_threshold for kt in kept_titles
        ):
            continue

        seen_urls.add(norm)
        kept.append(cand)
        if title:
            kept_titles.append(title)
    return kept
