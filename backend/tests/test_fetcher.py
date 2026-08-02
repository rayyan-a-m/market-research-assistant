"""Stage-1 fetcher: content classification + blocked-source recovery.

The classification tests pin the authwall/thin-content heuristic (the reason a
LinkedIn login page no longer sails through as a "source"). The recovery tests
drive `_fetch_one` end to end for the two choices a user has when a source is
blocked: continue without it, or paste its content.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import Settings
from app.pipeline import fetcher, unreachable


def _settings() -> Settings:
    return Settings(_env_file=None)  # min_content_chars default 250


# --- _classify_markdown (pure heuristic) --------------------------------------

def test_classify_accepts_real_article() -> None:
    kept, reason = fetcher._classify_markdown("This is a substantial article. " * 50, 250)
    assert kept is not None
    assert reason == "ok"


def test_classify_rejects_empty() -> None:
    kept, reason = fetcher._classify_markdown("   ", 250)
    assert kept is None
    assert "no content" in reason


def test_classify_rejects_thin_content() -> None:
    kept, reason = fetcher._classify_markdown("Short stub.", 250)
    assert kept is None
    assert "login wall" in reason or "stub" in reason


def test_classify_rejects_authwall_page() -> None:
    # Clears the thin-content floor but is shaped like a login wall.
    page = "Join to view " + ("profile details and connections. " * 20) + "sign in to continue"
    kept, reason = fetcher._classify_markdown(page, 50)
    assert kept is None
    assert "authwall" in reason or "login" in reason


def test_classify_ignores_single_marker_in_long_article() -> None:
    # A long article that merely contains ONE marker phrase is kept — one marker
    # is only decisive on a short page, so real content isn't misclassified.
    long_article = ("Detailed market analysis. " * 200) + " sign in to continue"
    kept, reason = fetcher._classify_markdown(long_article, 250)
    assert kept is not None
    assert reason == "ok"


def test_classify_rejects_long_login_wall_with_multiple_markers() -> None:
    # The LinkedIn case: a login page is *long* (forms, legal links, a language
    # picker) but carries several distinct login-flow phrases. Two is decisive
    # regardless of length — this is what slipped through the length-only gate.
    page = (
        "Welcome back. "
        + "English Español Français legal cookie policy user agreement " * 120
        + " New to LinkedIn? Join to view. Forgot password? Continue with Google."
    )
    assert len(page) > 3000  # long enough that a length-only check would miss it
    kept, reason = fetcher._classify_markdown(page, 250)
    assert kept is None
    assert "login" in reason or "authwall" in reason


# --- _fetch_one recovery flow -------------------------------------------------

class _FakeResult:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.success = True


class _FakeCrawler:
    def __init__(self, markdown: str) -> None:
        self._markdown = markdown

    async def arun(self, url: str) -> _FakeResult:
        return _FakeResult(self._markdown)


@pytest.fixture
def repo_fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, dict]], dict[str, str]]:
    updates: list[tuple[str, dict]] = []
    content: dict[str, str] = {}

    async def insert_run_source(**kw: Any) -> str:
        return "src-1"

    async def update_run_source(source_id: str, **kw: Any) -> None:
        updates.append((source_id, kw))

    async def get_source_content(run_id: str) -> dict[str, str]:
        return dict(content)

    monkeypatch.setattr(fetcher.repo, "insert_run_source", insert_run_source)
    monkeypatch.setattr(fetcher.repo, "update_run_source", update_run_source)
    monkeypatch.setattr(fetcher.repo, "get_source_content", get_source_content)
    # keep the test offline — no DNS/SSRF resolution
    monkeypatch.setattr(fetcher, "is_safe_url", lambda url: (True, ""))
    return updates, content


async def _drive(markdown: str, resolver: Any) -> tuple[Any, list[tuple[str, dict]]]:
    """Run _fetch_one against a blocked page, calling `resolver()` until it
    resolves the pause (simulating the user's paste/skip request)."""
    events: list[tuple[str, dict]] = []

    async def emit(name: str, data: dict) -> None:
        events.append((name, data))

    task = asyncio.create_task(
        fetcher._fetch_one(
            _FakeCrawler(markdown), "run-1", "https://x.test/a", "USER_SUPPLIED",
            asyncio.Semaphore(1), _settings(), emit,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if resolver():
            break
    return await task, events


async def test_blocked_source_skip_continues_without(
    repo_fakes: tuple[list[tuple[str, dict]], dict[str, str]],
) -> None:
    updates, _content = repo_fakes
    result, events = await _drive(
        "Please log in to continue.",  # thin → blocked
        lambda: unreachable.resolve_waiter("src-1", skip=True),
    )
    assert result is None  # source dropped, run continues without it
    assert any(name == "unreachable" for name, _ in events)
    assert any(kw.get("status") == "SKIPPED_BY_USER" for _sid, kw in updates)


async def test_blocked_source_paste_recovers(
    repo_fakes: tuple[list[tuple[str, dict]], dict[str, str]],
) -> None:
    _updates, content = repo_fakes

    def resolver() -> bool:
        # simulate the paste endpoint: write content, then wake (no skip)
        content["https://x.test/a"] = "Recovered full article body."
        return unreachable.resolve_waiter("src-1")

    result, events = await _drive("Please log in to continue.", resolver)
    assert result is not None
    assert result.markdown == "Recovered full article body."
    assert any(name == "unreachable" for name, _ in events)
