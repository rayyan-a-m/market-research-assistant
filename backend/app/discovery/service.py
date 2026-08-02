"""Source discovery — a deterministic service, not an agent.

Given competitors + topics, it proposes additional sources the user may not
have thought to include, for the user to approve or reject. The steps are:
  1. build search queries per competitor
  2. run them via the SearchProvider (Brave, Serper fallback)
  3. rank results by embedding similarity to the topics
  4. de-duplicate (exact URL + near-duplicate title via Jaro-Winkler)
  5. drop anything that fails the SSRF guard
  6. attach a rule-generated rationale (no LLM call)

Why deterministic and not `create_agent` + tools: search and ranking are
rules, not reasoning — there's no decision an agent loop would make here
that a function doesn't. Keeping it deterministic makes discovery cheaper,
faster, and far easier to reason about and test. The human approval gate is
a first-class application flow (candidates -> DB -> user approves), which is
where the actual judgement lives. See DESIGN_DECISIONS.md #11.
"""

from __future__ import annotations

import logging

import numpy as np

from app.config import Settings
from app.core.security import is_safe_url
from app.discovery.dedup import deduplicate
from app.models.schemas import DiscoveryCandidate, domain_of
from app.providers.base import SearchProvider, SearchResult
from app.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)


def build_queries(competitors: list[str], topics: list[str]) -> list[tuple[str, str]]:
    """Return (competitor, query) pairs. A couple of angles per competitor:
    recent news and topic-specific coverage."""
    queries: list[tuple[str, str]] = []
    topic_str = " ".join(topics[:3])
    for c in competitors:
        queries.append((c, f"{c} {topic_str} news announcement"))
        queries.append((c, f"{c} {topic_str} launch OR partnership OR report"))
    return queries


def _rationale(domain: str, competitor: str, topics: list[str]) -> str:
    topic = topics[0] if topics else "market activity"
    return f"{domain} — {topic} coverage for {competitor}"


async def discover(
    run_id: str,
    competitors: list[str],
    topics: list[str],
    input_urls: list[str],
    settings: Settings,
    search: SearchProvider | None = None,
) -> list[DiscoveryCandidate]:
    """Produce ranked, deduplicated, SSRF-safe candidates. Never raises —
    discovery failure returns an empty list (non-blocking by design)."""
    if not competitors:
        return []
    search = search or ProviderFactory.web_search(settings)

    # 1-2. search
    raw: list[tuple[str, SearchResult]] = []
    for competitor, query in build_queries(competitors, topics):
        try:
            for result in await search.search(query, count=10):
                raw.append((competitor, result))
        except Exception as exc:  # noqa: BLE001 — one failed query shouldn't sink discovery
            logger.warning("discovery search failed for %r: %s", query, exc)

    if not raw:
        return []

    # 3. rank by topic similarity
    embedder = ProviderFactory.embedding(settings)
    try:
        topic_vecs = np.array(await embedder.embed(topics), dtype=np.float32)
        snippet_vecs = np.array(
            await embedder.embed([f"{r.title}. {r.snippet}" for _, r in raw]), dtype=np.float32
        )
        topic_centroid = topic_vecs.mean(axis=0)
        topic_centroid /= np.linalg.norm(topic_centroid) or 1.0
        norms = np.linalg.norm(snippet_vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        scores = (snippet_vecs / norms) @ topic_centroid
    except Exception as exc:  # noqa: BLE001 — ranking is best-effort; fall back to search order
        logger.warning("discovery ranking failed, using search order: %s", exc)
        scores = np.linspace(1.0, 0.0, num=len(raw))

    order = np.argsort(-scores)

    # 4-6. dedup, SSRF filter, rationale
    candidates: list[DiscoveryCandidate] = []
    for idx in order:
        competitor, result = raw[int(idx)]
        url = result.url
        safe = is_safe_url(url)[0]
        domain = domain_of(url)
        candidates.append(DiscoveryCandidate(
            id="",  # assigned on DB insert
            url=url,
            domain=domain,
            page_title=result.title or None,
            rationale=_rationale(domain, competitor, topics),
            competitor=competitor,
            ssrf_safe=safe,
        ))

    candidates = deduplicate(
        candidates, input_urls, title_threshold=settings.discovery_dup_title_threshold
    )
    return candidates[: settings.discovery_candidates_max]
