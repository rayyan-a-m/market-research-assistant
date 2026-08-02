# Market Research Intelligence Assistant

Collects, analyzes and summarizes competitive market intelligence from public
sources: groups findings into themes, traces every insight back to its source,
and checks each claim against the original scraped text with a separate judge
model before showing it to you.

| | |
|---|---|
| **Live app** | https://market-research-assistant-nine.vercel.app |
| **API** | https://market-intel-api.greendune-83286828.eastus.azurecontainerapps.io |
| **API health** | [`/healthz`](https://market-intel-api.greendune-83286828.eastus.azurecontainerapps.io/healthz) (liveness) · [`/readyz`](https://market-intel-api.greendune-83286828.eastus.azurecontainerapps.io/readyz) (checks Gemini, Serper and Postgres, and reports the effective config) |

Sign in with Google or with a one-time code sent to any email address.

---

## Problem

Product and go-to-market teams struggle to stay current on competitor activity
because the relevant information is scattered across blogs, newsrooms,
announcements and articles. Monitoring 5–10 sources by hand is slow and
inconsistent. The harder problem is the one that appears *after* you automate
it: when an LLM summarizes those sources, nothing guarantees the summary is
faithful to what the sources actually said, and a confident, well-formatted,
wrong report is worse than no report.

So this application does two jobs. It produces the structured summary, and it
independently verifies it. Anything that can't be grounded in a source is
flagged rather than presented as fact.

---

## How it works

A run happens in two phases. There is no background queue; the pipeline runs
in-process and streams progress to the browser over SSE.

### Phase A — source discovery, behind a human approval gate

Before any research, the system runs a web search per competitor and proposes
additional sources you might not have thought to include. Every candidate
passes the SSRF guard, and every candidate is shown to you to approve, reject
or skip. Nothing is ever added silently. That's a trust decision rather than a
UX one: a report is only worth forwarding if the person sending it can vouch
for every source behind it.

Discovery is deterministic — search, rank by embedding similarity, dedupe,
SSRF-filter — not an agent. Ranking is a rule, so it's written as one
([DESIGN_DECISIONS #11](DESIGN_DECISIONS.md)).

### Phase B — the research pipeline

1. **Input guard.** Pydantic bounds (`max_urls=10`, `max_topics=5`), SSRF
   check, HTTPS only, dedupe.
2. **Fetch.** crawl4ai fetches URLs concurrently. A source that can't yield
   usable content isn't a silent failure, and "can't yield usable content" is
   broader than "returned an error": a login wall answers with HTTP 200 and
   real markup, so the fetcher classifies the *result* instead of trusting the
   status code. When a source is blocked the run pauses in place and offers
   two choices — paste the article text, or continue without it. Pasted text
   re-enters the pipeline identically; nothing downstream knows or cares how a
   source arrived.
3. **Retrieve.** Each source is chunked, embedded and retrieved *per source*,
   so a claim about Competitor A can't be matched to Competitor B's page.
   Context blocks are ordered highest-relevance first and second-highest last,
   to work with rather than against the "lost in the middle" effect.
4. **Summarize and judge.** Gemini produces a structured report where every
   claim carries a `source_url` as a schema field, not as something parsed out
   of prose. A separate judge model then evaluates each claim against the
   original scraped text — not against the passage the summarizer wrote. That
   distinction is the point of the stage. A summarizer that invents a
   supporting quote would have that quote confirm its own claim; grading
   against the source catches it.

Runs are persisted, and the report is assembled from a normalized `claims`
table rather than a JSON blob.

---

## What's built

Everything above is built, deployed and running at the URL up top. Both
stretch goals from the brief are included: change detection (`/runs/{id}/changes`
diffs a run against the previous run with the same inputs) and monitoring
(`/readyz`, request-ID correlation, per-run cost and token metrics).

**155 tests, ruff, mypy, and a green CI on every push.** The tests run fully
offline — no keys, no network — which is a property of the dependency
injection rather than a happy accident.

- **Provider layer.** Three interfaces (`ChatProvider`, `EmbeddingProvider`,
  `SearchProvider`), per-vendor adapters, and a registry factory that resolves
  a provider by *role*. Retries, timeouts and error normalization live at the
  adapter boundary. On top of that, a middleware stack — metering, budget cap,
  circuit breaker, cache, embedding retry — composed as decorators of those
  same interfaces.
- **Pipeline.** Fetch (SSRF, sanitize, authwall classification, pause and
  recover), per-source retrieval (chunk, embed, cosine, primacy/recency
  ordering), summarizer, judge.
- **Guardrail ladder.** Grounding enforced as a sequence of checks ordered by
  cost, so the expensive model only sees what the free checks couldn't decide.
  Table in [ARCHITECTURE.md](ARCHITECTURE.md).
- **Evaluation harness.** A labelled dataset of fictional sources with planted
  hallucinations, scored in three tiers: a deterministic tier in CI, a
  trajectory tier that checks each defect is caught at the *cheapest correct*
  rung, and a live-model tier behind `pytest -m eval`. Currently **60% of
  planted hallucinations are caught before any LLM call, at a 100% pass rate
  on faithful claims**. See [`backend/evals/`](backend/evals/).
- **API.** Runs router (discover, list, detail, candidates, approve, start,
  rerun, cancel, delete, changes), source-recovery router, asyncpg data layer,
  Clerk JWT auth via JWKS with a local-dev bypass that is hard-disabled in
  production.
- **Frontend.** Clerk auth and protected routes, research form, approval gate,
  live SSE progress, blocked-source recovery, dashboard, and a run report with
  per-claim verdicts and a cost/usage panel.

---

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15 (App Router), TypeScript, Tailwind CSS |
| Backend | FastAPI, Python 3.12 |
| AI orchestration | LangChain (LCEL chains + structured output) |
| Summarizer | Google `gemini-pro-latest` (fallback `gemini-flash-latest`) |
| Judge | Google `gemini-flash-latest` (fallback `gemini-2.5-flash`) |
| Embeddings | Google `gemini-embedding-001` |
| Retrieval | In-memory NumPy cosine, isolated per source |
| Scraping | crawl4ai |
| PDF text | pdfplumber |
| Discovery search | Serper.dev |
| Auth | Clerk (Google OAuth + one-time email code) |
| Persistence | Supabase (PostgreSQL 16) |
| Backend hosting | Azure Container Apps (single replica) |
| Frontend hosting | Vercel |
| CI | GitHub Actions |

---

## Repository layout

```
.
├── ARCHITECTURE.md           the as-built architecture in detail
├── DESIGN_DECISIONS.md       every decision, with the alternative rejected
├── FUTURE_ENHANCEMENTS.md    the scale-out roadmap (ARQ/Redis, pgvector, …)
├── deployment.md             Azure + Vercel deploy, key generation, env vars
├── backend/
│   ├── app/
│   │   ├── main.py           app factory + lifespan (db pool)
│   │   ├── config.py         pydantic-settings
│   │   ├── core/             errors, SSRF, sanitize, auth, rate limit, observability
│   │   ├── models/           Pydantic schemas (bounds + SSRF enforced here)
│   │   ├── providers/        interfaces, adapters, factory, middleware
│   │   ├── pipeline/         fetch → retrieve → summarize → judge, + SSE
│   │   ├── discovery/        deterministic source discovery + dedup
│   │   ├── db/               asyncpg pool + repository
│   │   └── routers/          HTTP layer, kept thin
│   ├── evals/                labelled dataset + scorer + trajectory checks
│   ├── db/schema.sql         DDL — run once in Supabase
│   ├── tests/
│   └── Dockerfile
└── frontend/
    └── src/
        ├── app/              App Router pages
        ├── components/       ApprovalGate, SourceFallback, ReportView, …
        ├── middleware.ts     Clerk route protection
        └── lib/              typed API client (incl. fetch-SSE) + types
```

Each layer depends only on the one below it, through an interface. The
provider layer is the only place a vendor SDK is imported, so adding a
provider means writing an adapter and registering it — the pipeline above it
doesn't change and doesn't get re-tested. Keeping the modules that *use* a
model dependent on an interface rather than on `ChatGoogleGenerativeAI` is
what made two forced model migrations mid-build into config edits.

---

## Running it locally

**Prerequisites:** Node 20+, Python 3.12+, and the API keys described in
[`deployment.md`](deployment.md) §2. Docker is optional (only for running the
backend as a container).

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env               # fill in keys — deployment.md §2–3
python -m playwright install chromium   # crawl4ai's browser
uvicorn app.main:app --reload --port 8000
# docs at http://localhost:8000/docs · health at /healthz
```

Set `AUTH_DISABLED=true` in `.env` to run the API before Clerk is configured.
That flag is ignored when `ENV=production`.

Run what CI runs:

```bash
ruff check . && mypy app evals && pytest -q
```

### Frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local   # NEXT_PUBLIC_API_URL + Clerk keys
npm run dev                        # http://localhost:3000
```

Deploying your own copy: follow [`deployment.md`](deployment.md) end to end.

---

## AI models, tools and references

### Models

| Model | Role | Why |
|---|---|---|
| `gemini-pro-latest` | Summarizer | The quality-critical stage — see below |
| `gemini-flash-latest` | Summarizer fallback | Same family, for rate-limit and outage resilience |
| `gemini-flash-latest` | Hallucination judge | Cheaper, different tier, grading the Pro model's output |
| `gemini-2.5-flash` | Judge fallback | Keeps verification available if the primary judge is down |
| `gemini-embedding-001` | Embeddings | Retrieval ranking, discovery ranking, judge similarity |

All five run on Google AI Studio, from one key.

**Why Pro for the summarizer.** This is the stage that determines output
quality, and its job needs three things at once: multi-source reasoning
across several labelled competitor documents, reliable structured-output
adherence, and enough context to hold a multi-source run. Gemini 2.5 Pro is
the strongest of Google's line on those axes. A weaker model here produces
vague themes and mis-attributed claims, and while the judge catches
unsupported claims after the fact, you still want the generator making fewer
errors and surfacing more real ones.

The cost is that Pro carries the tightest free-tier rate limits and the
highest latency. If a deployment cares more about throughput than peak
synthesis quality, Flash is a legitimate primary and the switch is one
environment variable (`SUMMARIZER_MODEL=gemini-flash-latest`). Pro is the
default because for a research tool a correct report beats a fast one.

**Why `gemini-embedding-001`.** Google's current production embedding model
on the AI Studio API, strong retrieval quality at effectively zero cost, and
it keeps embed → retrieve → judge-similarity on one provider and one key. Its
predecessor `text-embedding-004` has been retired, which is exactly why the
model name is a config value.

**Discovery uses no chat model at all.** It ranks candidates by embedding
similarity. There is no decision in "query these terms, embed the snippets,
sort by similarity, drop duplicates and unsafe URLs" that an agent loop would
make better than a function, and an agent there would add latency,
non-determinism and cost while making the behaviour harder to test.

### On the judge — a real limitation, stated

The ideal hallucination judge comes from a *different model family* than the
summarizer, because same-family judging is biased toward finding its own
lineage's output plausible. This app ships a second, cheaper Gemini model: a
cross-*model*, same-family check. It still tests each claim against the
original source, which is the property that matters most, but shared
pretraining means shared blind spots, and it is weaker than a genuine
cross-family judge.

It stays that way for scope reasons — one provider, one key, no extra
dependency. What keeps it from being a permanent choice is that the judge is
resolved by role rather than constructed at the call site. A second adapter
for OpenAI-compatible endpoints (Groq, OpenRouter, OpenAI) is in the codebase
and tested; setting `OPENAI_API_KEY`, `OPENAI_BASE_URL` and a matching
`JUDGE_MODEL` routes verification to another family with no code change. The
deployed app doesn't use that path, and `/readyz` reports which one is live.

If the judge fails entirely, its claims are marked unverified and the run
continues. And how well it performs isn't asserted, it's measured — see
[`backend/evals/`](backend/evals/).

### Libraries

`langchain` and `langchain-google-genai` for orchestration, structured output
and `.with_retry()`; `langchain-openai` for the alternate judge adapter;
`crawl4ai` for scraping; `pdfplumber` for PDF text; `jellyfish` for
Jaro-Winkler dedup; `asyncpg` for Postgres; `PyJWT` for Clerk JWT
verification; `httpx` for the search APIs; `pydantic` and
`pydantic-settings`; `numpy` for in-memory cosine retrieval.

### Prompting approach

- **Attribution is a schema field.** Every claim's `source_url` is
  constrained by structured output to one of the labelled source URLs, so
  cross-source attribution is prevented at the schema level rather than
  requested in a prompt.
- **The judge reads source truth.** Each claim is evaluated against the top
  chunks of the original scraped markdown, with a similarity floor below
  which the claim is marked `low_confidence` without spending an LLM call.
- **Context ordering.** Highest-relevance source first, second-highest last
  (Liu et al., *Lost in the Middle*, 2023).
- **Injection guard.** A system-prompt instruction to ignore embedded
  directives, layered on top of regex sanitization of all source text.

### Use of generative AI in building this

Built with assistance from **Claude (Anthropic)** for architecture
discussion, code scaffolding and documentation drafting. The design decisions,
the review of every line, and the results are mine. The test suite is what
keeps AI-written code honest here — it caught a real IPv4-only bug in the
SSRF guard during development, where an IPv6 loopback literal was being
waved through as "unresolvable."

---

## Security

| Threat | Mitigation |
|---|---|
| SSRF, including Azure IMDS at 169.254.169.254 | Pre-fetch IP resolution blocklist, IPv4 and IPv6, on user URLs *and* discovery candidates |
| Prompt injection in scraped or pasted content | Regex sanitization plus a system-prompt guard, applied to all source text regardless of origin |
| Prompt injection in the user's own guidance field | Separately sanitized — the blast radius is a colleague who reads the forwarded report |
| PII leaking from competitor pages into reports | Light email/phone redaction in the same pass |
| LLM cost abuse | Input bounds at the Pydantic boundary, a per-run token budget, and a per-user sliding-window rate limit on the expensive endpoints |
| Secrets in URLs and access logs | SSE is consumed with `fetch()` + `ReadableStream`, not `EventSource`, so the JWT rides in the `Authorization` header and never appears in a query string |
| Cross-user data access | Every run-scoped query carries the Clerk user id; ownership is checked in the router and the repository |
| Malicious PDF upload | MIME validation, text-only extraction, 10MB cap |
| Auth bypass | Clerk session JWT verified against Clerk's JWKS on every protected route; the dev bypass is hard-disabled when `ENV=production` |
| Container compromise | The image runs as an unprivileged user — the process rendering attacker-controlled pages is the last one that should own the container |

Each row above is covered by tests except the last, which is a property of the
image: verified by building it and confirming the process runs as `appuser`
and can still drive headless Chromium.

> The PDF path is worth one note of precision: `POST /sources/upload` is
> implemented, authenticated and bounded, but the recovery UI deliberately
> offers only paste and skip, to keep that moment to two clear choices.
> Surfacing upload in the UI is [FUTURE_ENHANCEMENTS](FUTURE_ENHANCEMENTS.md) #8.

---

## Cost

The whole build is designed to run at roughly **$0**. Summarizer, judge,
discovery ranking and embeddings all sit on Google AI Studio's free tier.

The one structural cost driver is the judge, which runs once per claim, and
it's gated by cheaper checks first: a claim citing a URL that was never
fetched is dropped for free, and a claim with no closely-matching source
content is resolved by an embedding comparison. On the evaluation dataset
those free rungs settle 6 of 15 claims before any model is called.
`RUN_TOKEN_BUDGET` is a hard backstop against a pathological run; on
exhaustion the run degrades to partially verified rather than spending on.

Because the free tiers are rate-limited, the real constraint is
requests-per-minute rather than dollars, and the input bounds keep a single
run well inside them. Every run records its own token counts, LLM calls,
search-API hits and an estimated cost at standard paid rates, shown in a
panel on the report page — so the headroom is visible rather than guessed.

---

## Further reading

- [**ARCHITECTURE.md**](ARCHITECTURE.md) — layering, the provider middleware
  stack, the guardrail ladder, the run lifecycle, observability.
- [**DESIGN_DECISIONS.md**](DESIGN_DECISIONS.md) — every significant decision
  with the alternative that was rejected and the failure mode it avoids,
  including the agentic patterns that were considered and deliberately not
  used.
- [**FUTURE_ENHANCEMENTS.md**](FUTURE_ENHANCEMENTS.md) — what a scale-out
  version changes, what each change buys, and when it's worth adopting.
- [**deployment.md**](deployment.md) — deploying your own copy end to end.
