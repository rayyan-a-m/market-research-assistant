from __future__ import annotations

from app.models.schemas import DraftClaim, DraftReport, DraftTheme
from app.pipeline.summarizer import summarize
from tests.fakes import FakeChatProvider


async def test_summarize_returns_parsed_report() -> None:
    draft = DraftReport(
        themes=[DraftTheme(title="T", summary="S", claims=[DraftClaim(text="c", source_url="https://a.com")])]
    )
    provider = FakeChatProvider(parsed=draft)

    out = await summarize(
        competitors=["Stripe"],
        topics=["payments"],
        context="[Source: https://a.com]\nsome passage",
        summarizer=provider,
    )

    assert out.themes[0].title == "T"
    assert provider.calls == 1
    # attribution rule must be in the system prompt (primacy)
    assert "source_url" in (provider.last_system or "")


async def test_summarize_returns_empty_report_when_parse_fails() -> None:
    provider = FakeChatProvider(parsed=None)
    out = await summarize(competitors=[], topics=["t"], context="ctx", summarizer=provider)
    assert out.themes == []


async def test_summarize_includes_analyst_guidance_in_the_prompt() -> None:
    provider = FakeChatProvider(parsed=DraftReport(themes=[]))
    await summarize(
        competitors=[], topics=["t"], context="ctx",
        guidance="Focus on EMEA launches", summarizer=provider,
    )
    assert "Focus on EMEA launches" in provider.last_messages[0].content
