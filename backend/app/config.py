"""Application configuration, loaded once from the environment.

Every provider/adapter takes its config through this Settings object, injected
by FastAPI via `Depends(get_settings)`, rather than reading os.environ
directly. That's what lets tests substitute fake settings without
monkeypatching the environment, and it's the same reason the provider factory
takes a model *name* per role instead of each adapter hardcoding one:
depend on configuration passed in, not on a global read from underneath you.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # For local dev, read the repo-root master `.env` first, then let a
    # `backend/.env` (if present) override it. This means you can fill in the
    # single root `.env` and the backend picks it up directly — no copying.
    # (On Azure these files don't exist; real environment variables are used
    # and always take precedence over any .env file.)
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    env: str = Field(default="development", alias="ENV")

    # --- Provider credentials ---
    # One Gemini (Google AI Studio) key powers the whole pipeline: summarizer,
    # judge, discovery ranking, and embeddings. The judge runs on a second
    # Gemini model (see judge_model) — a cheaper cross-model check within the
    # same family, using this same key.
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    # Optional cross-family judge: set these to route the hallucination judge
    # through any OpenAI-compatible provider (Groq, OpenRouter, OpenAI, ...).
    # If OPENAI_API_KEY is unset, the judge runs on a second Gemini model
    # instead (see judge_model). `openai_base_url` empty = real OpenAI.
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    brave_search_api_key: str = Field(default="", alias="BRAVE_SEARCH_API_KEY")
    serper_api_key: str = Field(default="", alias="SERPER_API_KEY")

    # --- Model selection per role (name only; factory resolves the adapter) ---
    # Summarizer = Gemini Pro (the quality-critical stage: multi-source
    # reasoning + schema-valid structured output + correct attribution).
    # We use the `-latest` ALIASES on purpose: Google retires pinned versions
    # (gemini-2.5-pro became "not available to new users"), and the aliases
    # always point at the current model — so the app doesn't break when a
    # version is pulled. Trade-off: the model behind the alias can change;
    # acceptable here where staying functional beats version-pinning. Pin an
    # explicit version via env if you need output stability.
    summarizer_model: str = Field(default="gemini-pro-latest", alias="SUMMARIZER_MODEL")
    summarizer_fallback_model: str = Field(
        default="gemini-flash-latest", alias="SUMMARIZER_FALLBACK_MODEL"
    )
    # Judge = the hallucination checker. By default it runs on a second Gemini
    # model (gemini-flash-latest) with the SAME Gemini key — so no extra
    # provider is needed. Honest trade-off: same model family as the summarizer,
    # so it's a different-tier check (Pro output graded by Flash), not a truly
    # independent cross-family one. To restore a cross-family judge, set
    # OPENAI_API_KEY/OPENAI_BASE_URL (Groq/OpenRouter/OpenAI) and a matching
    # JUDGE_MODEL — the factory routes the judge there automatically, no code
    # change. If the judge provider errors, the stage marks claims unverified
    # and the run continues.
    judge_model: str = Field(default="gemini-flash-latest", alias="JUDGE_MODEL")
    judge_fallback_model: str = Field(default="gemini-2.5-flash", alias="JUDGE_FALLBACK_MODEL")
    embedding_model: str = Field(default="models/gemini-embedding-001", alias="EMBEDDING_MODEL")

    # --- Timeouts / retries (adapter-layer defaults; can be overridden per call) ---
    provider_timeout_seconds: float = Field(default=30.0, alias="PROVIDER_TIMEOUT_SECONDS")
    provider_max_retries: int = Field(default=3, alias="PROVIDER_MAX_RETRIES")

    # --- Provider middleware (see providers/middleware.py) ---
    # Hard ceiling on LLM tokens for a single run. Sized well above a normal
    # run (10 sources ≈ 40-60k tokens) so it never fires in ordinary use — it's
    # a backstop against a pathological run, not a throttle. 0 disables it.
    # Checked pre-call, so real spend can overshoot by about one call.
    run_token_budget: int = Field(default=250_000, alias="RUN_TOKEN_BUDGET")
    # Consecutive infrastructure failures against one model before its breaker
    # opens and the fallback model takes over immediately.
    breaker_failure_threshold: int = Field(default=3, alias="BREAKER_FAILURE_THRESHOLD")
    breaker_cooldown_seconds: float = Field(default=30.0, alias="BREAKER_COOLDOWN_SECONDS")
    # Embedding vectors cached process-wide, keyed by an exact hash of
    # (model, text). Bounds memory in a long-lived process; ~4k chunks is
    # several runs' worth of documents.
    embedding_cache_max_entries: int = Field(default=4096, alias="EMBEDDING_CACHE_MAX_ENTRIES")

    # --- Persistence / auth ---
    database_url: str = Field(default="", alias="DATABASE_URL")
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(default="", alias="SUPABASE_SERVICE_ROLE_KEY")
    clerk_secret_key: str = Field(default="", alias="CLERK_SECRET_KEY")
    # Local-dev escape hatch: when true AND env != production, protected
    # endpoints accept requests without a Clerk JWT and attribute them to
    # AUTH_DEV_USER_ID. Ignored entirely in production (see core/auth.py).
    auth_disabled: bool = Field(default=False, alias="AUTH_DISABLED")
    auth_dev_user_id: str = Field(default="dev-user", alias="AUTH_DEV_USER_ID")

    # --- CORS ---
    frontend_origin: str = Field(default="http://localhost:3000", alias="FRONTEND_ORIGIN")

    # --- Logging ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # JSON in production (aggregators index it); plain text locally, because
    # JSON is unreadable when tailing a dev server.
    json_logs: bool = Field(default=False, alias="JSON_LOGS")

    # --- Rate limiting (see core/rate_limit.py) ---
    # The public deployment runs on shared free-tier provider quotas, so the
    # limit is about protecting *other users* from one user's retry loop, not
    # about protecting the server. Sized generously: a real session is a
    # handful of runs, so these should never fire in normal use.
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_window_seconds: float = Field(default=60.0, alias="RATE_LIMIT_WINDOW_SECONDS")
    rate_limit_runs_per_window: int = Field(default=5, alias="RATE_LIMIT_RUNS_PER_WINDOW")
    rate_limit_discovery_per_window: int = Field(
        default=10, alias="RATE_LIMIT_DISCOVERY_PER_WINDOW"
    )

    # --- Input bounds (Stage 0 guard) ---
    max_urls: int = Field(default=10, alias="MAX_URLS")
    max_topics: int = Field(default=5, alias="MAX_TOPICS")
    max_competitors: int = Field(default=5, alias="MAX_COMPETITORS")
    max_context_chars: int = Field(default=500, alias="MAX_CONTEXT_CHARS")

    # --- Concurrency ---
    fetch_concurrency: int = Field(default=5, alias="FETCH_CONCURRENCY")
    judge_concurrency: int = Field(default=5, alias="JUDGE_CONCURRENCY")

    # --- Retrieval / pipeline tuning ---
    chunk_size_chars: int = Field(default=2000, alias="CHUNK_SIZE_CHARS")
    chunk_overlap_chars: int = Field(default=250, alias="CHUNK_OVERLAP_CHARS")
    retrieval_top_k: int = Field(default=5, alias="RETRIEVAL_TOP_K")
    fetch_timeout_seconds: int = Field(default=30, alias="FETCH_TIMEOUT_SECONDS")
    # A fetch that returns fewer than this many characters of usable text is
    # treated as blocked (login wall, cookie banner, empty stub) rather than a
    # real source — the run pauses and asks the user how to proceed. See
    # fetcher._classify_markdown. Heuristic, deliberately tunable.
    min_content_chars: int = Field(default=250, alias="MIN_CONTENT_CHARS")
    # Below this cosine similarity, the judge treats a claim as having no
    # matching source content and returns low_confidence without an LLM call.
    judge_similarity_floor: float = Field(default=0.40, alias="JUDGE_SIMILARITY_FLOOR")

    # --- Discovery ---
    discovery_candidates_max: int = Field(default=12, alias="DISCOVERY_CANDIDATES_MAX")
    discovery_dup_title_threshold: float = Field(
        default=0.92, alias="DISCOVERY_DUP_TITLE_THRESHOLD"
    )

    # --- Unreachable-source pause (see ARCHITECTURE.md, run lifecycle) ---
    source_wait_timeout_seconds: int = Field(default=600, alias="SOURCE_WAIT_TIMEOUT_SECONDS")
    sse_heartbeat_seconds: int = Field(default=15, alias="SSE_HEARTBEAT_SECONDS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
