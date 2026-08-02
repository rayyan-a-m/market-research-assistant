"""Stage 1 — content fetching.

crawl4ai renders each URL and returns clean markdown. Concurrency is
bounded by a semaphore (headless Chromium is memory-heavy). Every fetch is
re-checked against the SSRF guard as defence-in-depth, and all content is
sanitized before it leaves this stage.

A URL that can't yield usable content doesn't fail the run: the fetcher
pauses on an asyncio.Event (see unreachable.py) and asks the user to either
continue without that source or paste its text, falling through to SKIPPED
after a timeout. "Can't yield usable content" is broader than a hard fetch
error — a login/authwall page returns HTTP 200 with real markup, so we
*classify* the result (see `_classify_markdown`) instead of trusting success.

crawl4ai is imported lazily so the rest of the app (and its unit tests) can
import this module without pulling in Playwright/Chromium.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.core.sanitize import sanitize_source_content
from app.core.security import is_safe_url
from app.db import repository as repo
from app.pipeline.events import Emit
from app.pipeline.unreachable import clear_waiter, register_waiter, was_skipped

# High-precision phrases a login / authwall / bot-check page renders in place
# of the article. Kept specific (not bare "sign in") so a normal article with a
# "Sign in" header link isn't flagged on a single hit. A login page dominated
# by chrome can be *long* (LinkedIn's is ~KBs of forms, legal links, and a
# language picker), so length alone can't gate this — instead TWO distinct
# markers are treated as decisive at any length, because a genuine article
# almost never contains two different login-flow phrases. See _classify_markdown.
_AUTHWALL_MARKERS = (
    "sign in to continue",
    "log in to continue",
    "sign in to see",
    "sign in to view",
    "sign in to linkedin",
    "you must be logged in",
    "please log in to",
    "new to linkedin",
    "join to view",
    "agree & join",
    "welcome back",
    "forgot password",
    "continue with google",
    "continue with apple",
    "continue with facebook",
    "create your free account",
    "sign up to view",
    "this content is only available to",
    "authwall",
    "verify you are human",
    "enable javascript and cookies",
    "please enable javascript",
    "access to this page has been denied",
    "checking your browser before accessing",
)
# One marker is decisive only on a short page; two distinct markers are decisive
# at any length (a long login wall trips this, a real article doesn't).
_AUTHWALL_SHORT_PAGE = 3000


@dataclass
class FetchedSource:
    url: str
    source_id: str
    markdown: str
    origin: str


def _classify_markdown(md: str, min_chars: int) -> tuple[str | None, str]:
    """Decide whether fetched markdown is a usable source.

    Returns `(text, "ok")` when usable, or `(None, reason)` when the page
    should be treated as blocked — empty, too thin to research, or shaped like
    a login/authwall/bot-check page. The reason is surfaced to the user so the
    pause explains *why* rather than just "unreachable".
    """
    text = md.strip()
    if not text:
        return None, "the page returned no content"
    if len(text) < min_chars:
        return None, "the page returned almost no text — likely a login wall or empty stub"
    lowered = text.lower()
    hits = sum(1 for marker in _AUTHWALL_MARKERS if marker in lowered)
    if hits >= 2 or (hits >= 1 and len(text) < _AUTHWALL_SHORT_PAGE):
        return None, "the page looks like a login / authwall page, not the article"
    return text, "ok"


async def _crawl_one(crawler: Any, url: str, timeout: int, min_chars: int) -> tuple[str | None, str]:
    """Return `(clean_text, "ok")`, or `(None, reason)` if the fetch failed or
    the content isn't usable (see `_classify_markdown`)."""
    try:
        result = await asyncio.wait_for(crawler.arun(url=url), timeout=timeout)
    except TimeoutError:
        return None, "the page timed out"
    except Exception:  # noqa: BLE001 — any crawl failure (nav error, DNS, ...) means "blocked"
        return None, "the page could not be reached"
    if not getattr(result, "success", False):
        return None, "the page could not be reached"
    md: Any = getattr(result, "markdown", "") or ""
    if hasattr(md, "raw_markdown"):
        md = md.raw_markdown or ""
    return _classify_markdown(str(md), min_chars)


async def _fetch_one(
    crawler: Any,
    run_id: str,
    url: str,
    origin: str,
    sem: asyncio.Semaphore,
    settings: Settings,
    emit: Emit,
) -> FetchedSource | None:
    safe, reason = is_safe_url(url)
    if not safe:
        await repo.insert_run_source(
            run_id=run_id, url=url, status="ERROR", source_origin=origin,
            error=f"blocked by SSRF guard: {reason}",
        )
        await emit("progress", {"stage": "FETCHING", "url": url, "status": "blocked", "reason": reason})
        return None

    async with sem:
        markdown, reason = await _crawl_one(
            crawler, url, settings.fetch_timeout_seconds, settings.min_content_chars
        )

    if markdown is not None:
        clean = sanitize_source_content(markdown)
        source_id = await repo.insert_run_source(
            run_id=run_id, url=url, status="OK", source_origin=origin, content_markdown=clean,
        )
        await emit("progress", {"stage": "FETCHING", "url": url, "status": "done"})
        return FetchedSource(url=url, source_id=source_id, markdown=clean, origin=origin)

    # Blocked — insert an ERROR row (with the specific reason), then pause and
    # let the user choose: continue without this source, or paste its content.
    source_id = await repo.insert_run_source(
        run_id=run_id, url=url, status="ERROR", source_origin=origin, error=reason,
    )
    await emit(
        "unreachable",
        {"stage": "FETCHING", "url": url, "source_id": source_id, "reason": reason},
    )
    event = register_waiter(source_id)
    try:
        await asyncio.wait_for(event.wait(), timeout=settings.source_wait_timeout_seconds)
    except TimeoutError:
        await repo.update_run_source(source_id, status="SKIPPED_BY_USER", error="no response in time")
        await emit("progress", {"stage": "FETCHING", "url": url, "status": "skipped"})
        return None
    finally:
        skipped = was_skipped(source_id)
        clear_waiter(source_id)

    # The user chose to drop this source rather than supply content.
    if skipped:
        await repo.update_run_source(source_id, status="SKIPPED_BY_USER", error="skipped by user")
        await emit("progress", {"stage": "FETCHING", "url": url, "status": "skipped"})
        return None

    # Otherwise the paste endpoint wrote content + status; re-read it.
    content_map = await repo.get_source_content(run_id)
    recovered = content_map.get(url)
    if not recovered:
        await emit("progress", {"stage": "FETCHING", "url": url, "status": "skipped"})
        return None
    await emit("progress", {"stage": "FETCHING", "url": url, "status": "recovered"})
    # origin was updated to USER_PASTED_TEXT by the paste endpoint
    return FetchedSource(url=url, source_id=source_id, markdown=recovered, origin=origin)


def _make_crawler() -> Any:
    # Lazy import: keeps Playwright/Chromium out of module import.
    from crawl4ai import AsyncWebCrawler

    return AsyncWebCrawler()


async def fetch_sources(
    run_id: str,
    url_origins: list[tuple[str, str]],
    settings: Settings,
    emit: Emit,
) -> list[FetchedSource]:
    await emit("stage_start", {"stage": "FETCHING", "total_urls": len(url_origins)})
    sem = asyncio.Semaphore(settings.fetch_concurrency)
    crawler = _make_crawler()
    async with crawler:
        results = await asyncio.gather(
            *(_fetch_one(crawler, run_id, url, origin, sem, settings, emit) for url, origin in url_origins)
        )
    return [r for r in results if r is not None]
