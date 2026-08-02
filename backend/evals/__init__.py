"""Offline evaluation harness for the grounding guardrails.

Unit tests answer "does the judge stage do what the code says?". They cannot
answer "does the judge actually catch hallucinations?", because that is a
property of a model plus a prompt plus a threshold, not of control flow. This
package answers the second question with a small labelled dataset and a
scorer, so a prompt edit, a model swap, or a threshold change produces a
number that can be compared instead of a vibe.

Two tiers, mirroring the guardrail ladder itself (README):

- **Deterministic tier** — the free rungs (structural attribution, the
  embedding similarity floor). No API keys, no network, runs in CI on every
  commit. Scored against the same dataset.
- **Model tier** — the LLM judge itself, scored for hallucination-detection
  recall and over-strictness. Needs real keys, so it is gated behind
  `pytest -m eval` exactly like the existing `integration` marker.

Run them:

    pytest tests/test_evals.py      # deterministic tier only (default)
    pytest -m eval                  # adds the model tier; needs GEMINI_API_KEY
"""
