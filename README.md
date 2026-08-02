# Market Research Intelligence Assistant

A web application that collects, analyzes, and summarizes competitive
market intelligence from public sources — grouping findings into themes,
tracing every insight back to its source, and **verifying each claim
against the original source content** with a cross-model LLM judge.

> **Scope note, up front:** this is deliberately a *lightweight*
> application, scoped to the problem. The heavier production concerns you
> might expect — a durable ARQ/Redis worker, pgvector, an SSE replay
> buffer — are intentionally out of the current build and documented as a
> roadmap in [`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md), with what
> each one buys and when it's worth adding. Scoping to fit the requirement
> is itself a design decision, and it's treated as one.

---

## Problem statement

Product and go-to-market teams struggle to stay current on competitor
activity because relevant information is scattered across blogs,
newsrooms, announcements, and articles. Manually monitoring 5–10 sources
is time-consuming and inconsistent — and worse, when a human *or* an AI
summarizes those sources, there's no guarantee the summary is faithful
to what the source actually said.

This application lets a user provide competitor names, topics, and source
URLs, then fetches, analyzes, and summarizes the material into a
structured, source-traced report — where **every claim is independently
checked against the original scraped text**, and anything that can't be
grounded is flagged rather than presented as fact.

---

## Solution approach

A two-phase run, no background queue — the pipeline runs in-process and
streams progress to the browser over SSE.

**Phase A — Source discovery (with a human approval gate).** Before any
research, the system runs a web search per competitor and proposes
additional sources the user may not have known to include. Every
candidate passes the SSRF guard and is shown to the user, who approves,
rejects, or skips. **The system never adds a source silently** — this is
a trust mechanism, not a convenience feature: a user must be able to
vouch for every source behind every insight. Discovery is a **deterministic
service** (search → embedding-rank → dedup → SSRF filter), not an agent —
the judgement that matters is the user's approval, not an LLM's; ranking is
a rule, so it's coded as one (see [DESIGN_DECISIONS #11](DESIGN_DECISIONS.md)).

**Phase B — Research pipeline (four stages, streamed):**

1. **Input guard** — Pydantic bounds (`max_urls=10`, `max_topics=5`),
   SSRF guard, HTTPS-only, dedup.
2. **Fetch** — crawl4ai fetches URLs concurrently (bounded). A source that
   can't yield usable content isn't a silent failure — and "can't yield
   usable content" is broader than a hard error: a login/authwall page
   returns HTTP 200 with real markup, so the fetcher *classifies* the result
   and treats a thin/authwall page as blocked too. When a source is blocked,
   the run *pauses in place* and offers the user two choices — **paste the
   article text** (it re-enters the pipeline identically; downstream stages
   don't know or care how a source arrived) or **continue without that
   source**. (Uploading a PDF is a roadmap item — see
   [`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md).)
3. **Retrieve (in-memory RAG)** — each source is chunked, embedded, and
   retrieved **per-source** (top passages per topic, per URL separately)
   so a claim about Competitor A can never be attributed to Competitor B's
   page. Context blocks are ordered highest-relevance-first / second-last
   to mitigate "lost in the middle."
4. **Summarize + Judge** — Gemini produces a structured report
   (schema-enforced `source_url` on every claim, no prose parsing). Then a
   **separate judge model evaluates each claim against the original
   scraped text** — not against the passage
   Gemini generated. That distinction is the whole point: if the summarizer
   fabricates a convincing passage, judging it against itself would
   rubber-stamp the fabrication. Judging against the *source* catches it. The
   judge is a separate, cheaper model; a genuinely cross-*family* judge is
   ideal and available via one config change, but the default stays within the
   Gemini family for this scope — a stated compromise, detailed below.

Previous runs are persisted; the report is assembled from a normalized
`claims` table rather than a denormalized JSON blob.

---

## Build status (honest)

The application is built end-to-end. What that means precisely — because
honesty about what's been *runtime-verified* matters:

**Built + unit-tested + statically checked (150 tests, ruff + mypy + CI green):**
- **Provider abstraction** — interfaces + per-vendor adapters + registry
  factory (Strategy / Adapter / Factory), retries/timeouts/normalized
  errors at the adapter boundary. Gemini chat + embeddings, an optional
  OpenAI-compatible judge path, Serper web search, fallback composition,
  plus a middleware stack (metering, budget cap, circuit breaker, cache)
  composed as decorators of the same interfaces.
- **Pipeline** — fetch (crawl4ai + SSRF + sanitize + unreachable-source
  pause), in-memory per-source retrieval (chunk → embed → cosine →
  primacy/recency ordering), summarizer (structured output), judge (verify
  vs. original source, gated by a structural attribution check and then a
  similarity floor). Retrieval math and ordering are unit-tested.
- **Guardrail ladder** — grounding enforced as a sequence of checks ordered
  by cost, so the expensive LLM judge only sees what the free checks
  couldn't decide. Table in [ARCHITECTURE.md](ARCHITECTURE.md).
- **Evaluation harness** — a labelled dataset of fictional sources with
  planted hallucinations, scored in three tiers: a deterministic tier in CI
  (no keys, no network), a **trajectory tier** that checks each defect is
  routed to the cheapest correct rung of the guardrail ladder, and a
  live-model tier behind `pytest -m eval`. Currently **60% of planted
  hallucinations are caught before any LLM call**, at a 100% pass rate on
  faithful claims. See [`backend/evals/`](backend/evals/).
- **Discovery** — deterministic search → rank → dedup (URL + Jaro-Winkler)
  → SSRF filter; dedup is unit-tested.
- **API** — runs router (discover/list/detail/candidates/approve/start-SSE),
  sources router (paste/skip recovery), asyncpg data layer, Clerk JWT auth
  (JWKS) with a local-dev bypass.
- **Frontend** — Clerk (Google-only) auth + protected routes, research form,
  approval gate, live SSE progress (fetch + ReadableStream), source-fallback
  (paste / continue-without), dashboard, run-detail report view with a per-run
  cost/usage panel. Typechecks, lints, builds.
- Security core (SSRF IPv4+IPv6, sanitizer), input bounds, Dockerfile, CI.

**Not yet runtime-verified (needs live keys + deploy — that's your step):**
- End-to-end runs against the real Gemini / Serper APIs, a
  live crawl4ai browser, and a real Supabase database. These paths are
  written and type-checked but exercised for the first time when you deploy
  with real credentials (unit tests deliberately mock the network — CI has
  no keys). Follow [`deployment.md`](deployment.md) and the smoke-test in §8.

The layers a reviewer pokes at for correctness and security (providers,
SSRF, sanitizer, retrieval, dedup, validation) carry the unit tests; the
integration surface is verified on first deploy.

---

## Technology stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15 (App Router), TypeScript, Tailwind CSS |
| Backend | FastAPI, Python 3.12 |
| AI orchestration | LangChain (LCEL chains + structured output) |
| Summarizer | Google `gemini-pro-latest` (fallback: `gemini-flash-latest`) |
| Judge | Google `gemini-flash-latest` (fallback: `gemini-2.5-flash`); any OpenAI-compatible provider via config |
| Embeddings | Google `gemini-embedding-001` |
| Retrieval | In-memory (NumPy cosine), per-source isolation |
| Web scraping | crawl4ai |
| PDF extraction | pdfplumber |
| Discovery search | Serper.dev |
| Auth | Clerk (Google OAuth only) |
| Persistence | Supabase (PostgreSQL 16) |
| Backend hosting | Azure Container Apps (single replica) |
| Frontend hosting | Vercel |
| CI | GitHub Actions |

Model IDs and the rationale for each choice are in
[Design decisions](DESIGN_DECISIONS.md) and the stack table above.

---

## Repository structure

```
.
├── README.md                 ← this file
├── ARCHITECTURE.md           ← the current architecture in detail
├── DESIGN_DECISIONS.md       ← every decision, with the alternative rejected
├── deployment.md             ← full Azure + Vercel deploy + key generation
├── FUTURE_ENHANCEMENTS.md    ← the scale-out roadmap (ARQ/Redis, pgvector, …)
├── backend/
│   ├── app/
│   │   ├── main.py           ← FastAPI app factory + lifespan (db pool)
│   │   ├── config.py         ← pydantic-settings
│   │   ├── core/             ← errors, security (SSRF), sanitize, auth (Clerk)
│   │   ├── models/           ← Pydantic schemas (bounds + SSRF enforced)
│   │   ├── providers/        ← Strategy/Adapter/Factory for AI providers
│   │   ├── pipeline/         ← fetch → retrieve → summarize → judge + SSE
│   │   ├── discovery/        ← deterministic source discovery + dedup
│   │   ├── db/               ← asyncpg pool + repository (SQL)
│   │   └── routers/          ← HTTP layer (thin): runs, sources, health
│   ├── db/schema.sql         ← DDL: run once in Supabase (deployment.md §2.4)
│   ├── tests/
│   └── Dockerfile
└── frontend/
    └── src/
        ├── app/              ← App Router pages (research, dashboard, sign-in)
        ├── components/       ← ApprovalGate, SourceFallback, ReportView, …
        ├── middleware.ts     ← Clerk route protection
        └── lib/              ← typed API client (incl. fetch-SSE) + types
```

---

## Local build & run

**Prerequisites:** Node 20+, Python 3.12+, Docker (for the backend
container / crawl4ai's Chromium), and the API keys from
[`deployment.md`](deployment.md) §2.

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in keys — see deployment.md §2–3
python -m playwright install chromium   # for crawl4ai
uvicorn app.main:app --reload --port 8000
# API docs at http://localhost:8000/docs  (health: /healthz)
```

Run the checks the CI runs:

```bash
ruff check . && mypy app && pytest -q
```

### Frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local   # set NEXT_PUBLIC_API_URL + Clerk keys
npm run dev                        # http://localhost:3000
```

Deploying to Azure + Vercel: follow [`deployment.md`](deployment.md)
end to end.

---

## AI tools, models, and references

### Models

| Model | Provider | Role | Why this model |
|---|---|---|---|
| `gemini-pro-latest` | Google (AI Studio) | Summarizer | The quality-critical stage — see rationale below |
| `gemini-flash-latest` | Google (AI Studio) | Summarizer fallback | Same family, for outage/rate-limit resilience |
| `gemini-flash-latest` | Google (AI Studio) | Hallucination judge | A cheaper, different-*tier* model grading the Pro model's output. Honest caveat: same family — see the note below |
| `gemini-2.5-flash` | Google (AI Studio) | Judge fallback | Keeps verification available if the primary judge model is unavailable |
| `gemini-embedding-001` | Google (AI Studio) | Embeddings | Retrieval ranking, discovery ranking, and judge similarity |

### Model choice rationale

**Summarizer — why Gemini 2.5 Pro (is it the best for this task?):**
The summarizer is the single stage that determines output quality. Its job is
hard: read several labelled competitor sources, group findings into coherent
themes, and emit **schema-valid structured output** where every claim carries
the *correct* `source_url`. That demands three things together — strong
multi-source reasoning, reliable structured-output/tool-use adherence, and
long-context comprehension. Gemini **2.5 Pro** is Google's strongest model on
exactly those axes (and its large context window comfortably holds a
multi-source run), so it's the right default here. A weaker model at this
stage produces vague themes and mis-attributed claims — and while the
cross-family judge *catches* unsupported claims, you still want the generator
to make fewer errors and surface more real, well-grounded ones. So yes: for
the synthesis step, Pro is the best fit among Gemini's options.

**The honest trade-off:** Pro has the strictest free-tier rate limits and the
highest latency of the Gemini line. If a deployment cares more about
throughput / staying inside rate limits than about peak synthesis quality,
**Gemini Flash** is a legitimate primary — it's a one-env-var change
(`SUMMARIZER_MODEL=gemini-flash-latest`). Pro is the default because this is a
research tool where a correct, well-structured report matters more than
shaving a few seconds. Flash is also the built-in fallback for outage/rate-limit resilience.

**Embeddings — `gemini-embedding-001`:** Google's current production embedding
model on the AI Studio API; strong retrieval quality at effectively zero cost,
and it keeps the whole embed → retrieve → judge-similarity path on one
provider/key. (The older `text-embedding-004` has been retired from the API,
so `gemini-embedding-001` is the right current choice; it's a one-line
`EMBEDDING_MODEL` swap if Google ships a newer one.)

**Discovery uses no chat LLM at all** — it ranks candidates with
`gemini-embedding-001` similarity, not a generative model, because ranking is a
rule (see [DESIGN_DECISIONS #11](DESIGN_DECISIONS.md)). That's the "don't put
an LLM where a rule works" principle, and it's why there's no separate
"discovery model."

> **On the judge — a stated compromise, not a clean win.** The ideal is a
> judge from a *different model family*, because same-family judging is biased
> toward finding its own lineage's output plausible. For the scope of this
> application the default judge is a second, cheaper Gemini model — a
> cross-*model* check within the same family. It still tests grounding against
> the original source (the property that matters most), but it is weaker than
> a cross-family judge: shared pretraining means shared blind spots.
>
> The abstraction keeps this a config decision, not a rewrite: set
> `OPENAI_API_KEY` + `OPENAI_BASE_URL` (Groq, OpenRouter, OpenAI) and
> `JUDGE_MODEL`, and the factory routes the judge to a genuinely different
> family with **no code change**. If the judge provider fails, the stage marks
> claims *unverified* and the run continues. How well the judge actually
> performs isn't left to assertion — it's measured; see the evaluation harness
> in [`backend/evals/`](backend/evals/).

### Libraries

`langchain` / `langchain-google-genai` / `langchain-openai` (orchestration,
structured output, `.with_retry()` — the OpenAI package targets any
OpenAI-compatible endpoint, e.g. Groq / OpenRouter / OpenAI, for the optional
cross-family judge), `crawl4ai` (LLM-oriented
scraping), `pdfplumber` (PDF text), `jellyfish` (Jaro-Winkler dedup),
`asyncpg` (Postgres), `PyJWT` (Clerk JWT verification), `httpx` (search
APIs), `pydantic` / `pydantic-settings` (validation + config), `numpy`
(in-memory cosine retrieval).

The LCEL patterns this project uses (`.with_retry()`, `.with_fallbacks()`,
`.with_structured_output()`) — plus the agent/middleware patterns that were
reviewed but deliberately **not** adopted — are applied in the provider
adapters (`app/providers/`) and the composable middleware stack
(`app/providers/middleware.py`).

### Prompting approach

- **Schema-enforced attribution** — every claim's `source_url` is
  constrained to the labelled source URLs via structured output, so
  cross-source attribution is prevented at the schema level.
- **Judge against source truth** — each claim is evaluated against the
  top actual chunks of the *original scraped markdown*, with a
  similarity threshold below which the claim is marked `low_confidence`
  without spending an LLM call.
- **Primacy/recency context ordering** — highest-relevance source first,
  second-highest last (Liu et al., "Lost in the Middle", 2023).
- **Injection guard** — a system-prompt instruction to ignore embedded
  directives, layered on top of regex sanitization of scraped content.

### Generative AI usage disclosure

This project was built with assistance from **Claude (Anthropic)** for
architecture design, code scaffolding, and documentation drafting. All
code has been reviewed and is the author's responsibility; the test suite
is the mechanism that keeps AI-generated code honest (it caught a real
IPv4-only SSRF bug in the security guard during development). Nothing here
was accepted unread.

---

## Security design

| Threat | Mitigation | Status |
|---|---|---|
| SSRF (incl. Azure IMDS at 169.254.169.254) | Pre-fetch IP resolution blocklist, IPv4 + IPv6, on user URLs *and* discovery candidates | Implemented + tested |
| Prompt injection in scraped/pasted content | Regex sanitization + system-prompt guard, applied to all source text regardless of origin | Implemented + tested |
| PII leakage from competitor pages into reports | Light email/phone redaction in the same sanitize pass | Implemented + tested |
| LLM cost abuse | `max_urls`/`max_topics` enforced at the Pydantic boundary | Implemented + tested |
| Secrets in query strings / logs | SSE is a `fetch()` stream, so the Clerk JWT rides in the `Authorization` header — never in a URL or access log (no token in the query string at all) | Implemented |
| Cross-user data access | Every run-scoped query carries the Clerk user id; ownership checked in the router + repository (RLS is defense-in-depth only — see [`schema.sql`](backend/db/schema.sql) header) | Implemented |
| Malicious PDF upload | MIME validation + text-only `pdfplumber` extraction + 10MB cap | Implemented |
| Auth bypass | Clerk session JWT verified against Clerk's JWKS on every protected route; dev bypass hard-disabled when `ENV=production` | Implemented |

---

## Cost note

This build is designed to run at **≈ $0**: summarizer, judge, discovery
ranking, and embeddings all run on Google AI Studio's free tier. The only
structural cost driver is the judge — one call per claim — and it's gated
by a ladder of cheaper checks: a claim citing an unfetched URL is dropped
for free, and a claim with no closely-matching source content is resolved by
an embedding comparison, both before any LLM call. On the evaluation dataset
those free rungs resolve 6 of 15 claims. A per-run token budget
(`RUN_TOKEN_BUDGET`) is a hard backstop against a pathological run: on
exhaustion the run degrades to partially-verified rather than spending on. The free tiers are **rate-limited**,
so the real constraint is requests-per-minute, not dollars; hard input
bounds (`MAX_URLS=10`, `MAX_TOPICS=5`) keep a single run well inside them.
Every run records its own usage — token counts, LLM calls, **search-API
hits**, and an **estimated cost** at standard paid rates — surfaced in a
per-run cost/usage panel on the report page, so the free-tier headroom is
visible rather than guessed. Full breakdown in [`deployment.md`](deployment.md) §8.

---

## Design decisions

Every significant decision — with the **alternative that was rejected**
and the failure mode it avoids — is in
[**DESIGN_DECISIONS.md**](DESIGN_DECISIONS.md). The architecture in
detail is in [**ARCHITECTURE.md**](ARCHITECTURE.md).

## Live application

**URL:** _(populated after deploy — see [`deployment.md`](deployment.md))_
