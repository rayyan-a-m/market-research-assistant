"""Pydantic schemas shared across routers, providers, and the pipeline.

Two things are deliberately enforced here rather than deeper in the
pipeline: input bounds (cost control — see DESIGN_DECISIONS.md #4) and the
SSRF guard (security — every URL that reaches a router has already been
validated, so downstream code never has to re-check it). Enforcing both at
the request boundary rather than scattering `if len(urls) > 10` checks
through the codebase is the point.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.core.sanitize import sanitize_user_text
from app.core.security import is_https_url, is_safe_url

Verdict = Literal["supported", "partial", "unsupported", "low_confidence"]
RunStatus = Literal[
    "DISCOVERING", "AWAITING_APPROVAL", "PENDING", "PROCESSING",
    "COMPLETE", "FAILED", "CANCELLED",
]
SourceStatus = Literal["OK", "ERROR", "OK_PASTED", "OK_PDF_UPLOAD", "SKIPPED_BY_USER"]
SourceOrigin = Literal[
    "USER_SUPPLIED", "USER_APPROVED_DISCOVERED", "USER_PASTED_TEXT", "USER_UPLOADED_PDF"
]


def _validate_urls(urls: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(urls))  # preserves order, drops exact dupes
    for url in deduped:
        if not is_https_url(url):
            raise ValueError(f"URL must use https: {url}")
        safe, reason = is_safe_url(url)
        if not safe:
            raise ValueError(f"URL not allowed: {url} ({reason})")
    return deduped


class RunRequest(BaseModel):
    competitors: list[str] = Field(default_factory=list, max_length=5)
    topics: list[str] = Field(min_length=1, max_length=5)
    urls: list[str] = Field(min_length=1, max_length=10)
    context: str | None = Field(default=None, max_length=500)

    @field_validator("urls")
    @classmethod
    def urls_must_be_safe(cls, v: list[str]) -> list[str]:
        return _validate_urls(v)

    @field_validator("topics", "competitors")
    @classmethod
    def strip_blank_strings(cls, v: list[str]) -> list[str]:
        # Sanitized here, at the boundary, for the same reason the bounds and
        # the SSRF check are (DESIGN_DECISIONS #4): every downstream consumer
        # can then assume prompt-safe text without re-checking. Source content
        # was already sanitized on the way in; this closes the matching hole
        # for the strings the *user* contributes to the same prompt.
        return [sanitize_user_text(s.strip()) for s in v if s.strip()]

    @field_validator("context")
    @classmethod
    def sanitize_context(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = sanitize_user_text(v.strip())
        return cleaned or None


class DiscoveryCandidate(BaseModel):
    id: str
    url: str
    domain: str
    page_title: str | None = None
    rationale: str
    competitor: str
    ssrf_safe: bool = False
    decision: Literal["APPROVED", "REJECTED"] | None = None


class ApprovalDecision(BaseModel):
    candidate_id: str
    approved: bool


class ApproveRequest(BaseModel):
    decisions: list[ApprovalDecision]
    mechanism: Literal["user_action", "select_all", "reject_all", "skip"] = "user_action"


class SourcePasteRequest(BaseModel):
    source_id: str
    url: str
    content: str = Field(min_length=1, max_length=100_000)


class SourceSkipRequest(BaseModel):
    source_id: str


# --- Summarizer (Stage 3) structured-output target ---------------------------
# The summarizer produces DRAFT claims with no verification fields — that's
# the judge's job (Stage 4). Keeping the summarizer's schema free of
# verified/confidence/verdict prevents it from self-grading.


class DraftClaim(BaseModel):
    text: str
    source_url: str
    source_passage: str | None = None


class DraftTheme(BaseModel):
    title: str
    summary: str
    claims: list[DraftClaim]


class DraftReport(BaseModel):
    """What Gemini returns. `source_url` on every claim is constrained by the
    prompt to the labelled source URLs in context."""

    themes: list[DraftTheme]


# --- Final (post-judge) shapes ----------------------------------------------


class Claim(BaseModel):
    theme: str
    text: str
    source_url: str
    source_passage: str | None = None
    verified: bool
    confidence: float = Field(ge=0.0, le=1.0)
    verdict: Verdict
    judge_reason: str | None = None


class Theme(BaseModel):
    title: str
    summary: str
    claims: list[Claim]


class ResearchReport(BaseModel):
    """The final verified report, assembled from judged claims and returned
    by the API. Per-source attribution is enforced upstream at the summarizer's
    DraftReport schema (see DESIGN_DECISIONS #6 / #9)."""

    themes: list[Theme]


class RunSource(BaseModel):
    url: str
    status: SourceStatus
    error: str | None = None
    source_origin: SourceOrigin = "USER_SUPPLIED"


class ClaimChange(BaseModel):
    claim: Claim
    previous_verdict: Verdict


class RunChanges(BaseModel):
    """Diff of a run against the user's previous comparable run."""

    has_previous: bool
    previous_run_id: str | None = None
    new_claims: list[Claim] = Field(default_factory=list)
    changed: list[ClaimChange] = Field(default_factory=list)


class RunSummary(BaseModel):
    id: str
    status: RunStatus
    competitors: list[str]
    topics: list[str]
    created_at: datetime
    completed_at: datetime | None = None


class RunMetrics(BaseModel):
    duration_ms: int = 0
    sources_fetched: int = 0
    claims_total: int = 0
    claims_verified: int = 0
    themes: int = 0
    summarizer_model: str = ""
    judge_model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # Provider calls that actually reached a model, counted by the metering
    # middleware — so a run served partly from cache or cut short by the
    # budget guard reports what it really spent, not what it planned to.
    llm_calls: int = 0
    # Web-search API queries issued during discovery (0 for a re-run, which
    # reuses previously approved sources instead of searching again).
    search_calls: int = 0
    # Rough USD cost at each vendor's standard paid tier — see core/costs.py.
    # The app runs on free tiers, so the real charge is $0; this answers "what
    # would this run cost if billed at list price?" (embeddings excluded).
    estimated_cost_usd: float = 0.0
    # Claims the summarizer attributed to a URL that was never fetched, caught
    # by the structural attribution check and dropped before the judge stage.
    # Surfaced rather than swallowed: a non-zero value here is a signal about
    # the model, and hiding it would defeat the point of checking.
    claims_dropped_unattributed: int = 0


class RunDetail(RunSummary):
    input_urls: list[str] = Field(default_factory=list)
    context: str | None = None
    sources: list[RunSource]
    report: ResearchReport | None = None
    error_detail: str | None = None
    metrics: RunMetrics | None = None


def domain_of(url: str) -> str:
    return urlparse(url).hostname or url
