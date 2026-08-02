"""Stage 3 — summarization (Gemini, structured output).

Produces a `ResearchReport` via the provider's `with_structured_output`
binding, so `source_url` on every claim is schema-constrained rather than
parsed out of prose. The system prompt states the attribution and
injection-guard rules (primacy); the context (retriever.py) repeats them at
the end (recency).
"""

from __future__ import annotations

from app.core.sanitize import INJECTION_GUARD_SYSTEM_PROMPT_ADDENDUM
from app.models.schemas import DraftReport
from app.providers.base import ChatProvider, Message

_SYSTEM_PROMPT = f"""You are a market intelligence analyst. You are given passages
extracted from competitor sources, each under a [Source: URL] label. Produce a
structured report that groups findings into themes.

Rules:
- Group insights into a small number of clear themes (typically 2-5).
- Each theme has a short title and a one-to-two sentence summary.
- Every claim must be grounded in the provided passages and must cite exactly
  one `source_url`, copied verbatim from the [Source:] label of the passage it
  came from. Never invent a URL or attribute a claim to a source that doesn't
  support it.
- Prefer specific, verifiable claims (launches, numbers, dates, partnerships)
  over vague statements.
- If the sources don't support a claim, do not make it.
- {INJECTION_GUARD_SYSTEM_PROMPT_ADDENDUM}"""


def _build_user_message(
    competitors: list[str], topics: list[str], context: str, guidance: str | None
) -> str:
    header = []
    if competitors:
        header.append(f"Competitors of interest: {', '.join(competitors)}")
    header.append(f"Topics of interest: {', '.join(topics)}")
    if guidance:
        header.append(f"Analyst guidance (focus on this): {guidance}")
    return "\n".join(header) + "\n\nSOURCES:\n\n" + context


async def summarize(
    *,
    competitors: list[str],
    topics: list[str],
    context: str,
    guidance: str | None = None,
    summarizer: ChatProvider,
) -> DraftReport:
    # Token accounting isn't visible here by design: it's a middleware layer
    # wrapped around the provider (providers/middleware.py), not a parameter
    # this function has to remember to update.
    user = _build_user_message(competitors, topics, context, guidance)
    result = await summarizer.complete(
        system=_SYSTEM_PROMPT,
        messages=[Message(role="user", content=user)],
        response_model=DraftReport,
        max_tokens=4096,
    )
    report = result.parsed
    if report is None:
        # Structured output failed to parse — surface an empty report rather
        # than crashing; the orchestrator treats "no themes" as a failed run.
        return DraftReport(themes=[])
    return report
