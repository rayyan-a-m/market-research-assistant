"""Reference cost estimation for a run.

These are *reference* prices at each vendor's standard paid tier, used only to
turn token/query counts into a rough dollar figure for the cost panel. The app
runs on free tiers where the actual charge is $0 — the estimate answers "what
would this run cost if billed at list price?", which is the useful planning
number when deciding whether the free tier is enough.

Two things are stated rather than silently folded into a wrong number:
  • Rates are approximate and pinned in one place; nothing in the pipeline
    depends on them, so they're safe to update as vendors change pricing.
  • Embeddings are excluded — they don't flow through the chat metering ledger,
    so their token counts aren't available here.
"""

from __future__ import annotations

# USD per 1M tokens (input, output), matched by substring so both the alias
# names (`gemini-pro-latest`) and pinned names (`gemini-2.5-pro`) resolve.
_CHAT_RATES: tuple[tuple[str, float, float], ...] = (
    ("pro", 1.25, 10.00),
    ("flash", 0.30, 2.50),
)
_DEFAULT_CHAT_RATE: tuple[float, float] = (0.30, 2.50)  # unknown model → flash tier

# USD per search query (Serper standard tier ≈ $1 / 1000 queries).
SEARCH_RATE_PER_QUERY = 0.001


def _chat_rate(model: str) -> tuple[float, float]:
    lowered = model.lower()
    for needle, rate_in, rate_out in _CHAT_RATES:
        if needle in lowered:
            return rate_in, rate_out
    return _DEFAULT_CHAT_RATE


def estimate_run_cost(
    per_call: list[tuple[str, int, int]], search_calls: int
) -> float:
    """Rough USD cost of a run from per-call `(model, input_tokens,
    output_tokens)` plus the number of search queries. Reference rates only."""
    total = search_calls * SEARCH_RATE_PER_QUERY
    for model, input_tokens, output_tokens in per_call:
        rate_in, rate_out = _chat_rate(model)
        total += (input_tokens / 1_000_000) * rate_in
        total += (output_tokens / 1_000_000) * rate_out
    return round(total, 6)
