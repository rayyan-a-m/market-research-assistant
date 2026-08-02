"""Reference cost estimation (app/core/costs.py)."""

from __future__ import annotations

from app.core.costs import SEARCH_RATE_PER_QUERY, estimate_run_cost


def test_zero_usage_is_zero() -> None:
    assert estimate_run_cost([], 0) == 0.0


def test_search_only_cost() -> None:
    assert estimate_run_cost([], 5) == round(5 * SEARCH_RATE_PER_QUERY, 6)


def test_pro_costs_more_than_flash() -> None:
    pro = estimate_run_cost([("gemini-pro-latest", 1_000_000, 1_000_000)], 0)
    flash = estimate_run_cost([("gemini-flash-latest", 1_000_000, 1_000_000)], 0)
    assert pro == round(1.25 + 10.00, 6)
    assert flash == round(0.30 + 2.50, 6)
    assert pro > flash


def test_pinned_and_alias_model_names_resolve_the_same() -> None:
    alias = estimate_run_cost([("gemini-flash-latest", 500_000, 0)], 0)
    pinned = estimate_run_cost([("gemini-2.5-flash", 500_000, 0)], 0)
    assert alias == pinned


def test_unknown_model_falls_back_to_flash_tier() -> None:
    assert estimate_run_cost([("mystery-model", 1_000_000, 0)], 0) == round(0.30, 6)


def test_combined_tokens_and_search() -> None:
    cost = estimate_run_cost(
        [("gemini-pro-latest", 2_000_000, 500_000), ("gemini-flash-latest", 100_000, 50_000)],
        12,
    )
    expected = (
        2_000_000 / 1e6 * 1.25
        + 500_000 / 1e6 * 10.00
        + 100_000 / 1e6 * 0.30
        + 50_000 / 1e6 * 2.50
        + 12 * SEARCH_RATE_PER_QUERY
    )
    assert cost == round(expected, 6)
