# Architecture

The current, as-built architecture. The scale-out version — durable
ARQ/Redis worker, pgvector, SSE replay — is a documented future
enhancement: see [`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md) for the
roadmap, with what each change buys and when it's worth adopting.

---

## Layered separation (the important part)

The system is organized so that responsibilities don't bleed across
layers. Each layer depends only on the one below it, through an interface:

```
┌─────────────────────────────────────────────────────────────┐
│  HTTP layer            app/routers/*                          │
│  thin: parse request, call orchestration, stream/return      │
│  knows nothing about which LLM vendor or how sources arrive   │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│  Orchestration          app/pipeline/* , app/discovery/*      │
│  the four-stage research pipeline + the discovery service      │
│  composes providers; owns the run lifecycle & SSE progress     │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│  Provider abstraction   app/providers/*                       │
│  ChatProvider / EmbeddingProvider / SearchProvider interfaces  │
│  + per-vendor adapters + a registry factory                    │
│  ← the ONLY layer that imports a vendor SDK                     │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│  Core + models          app/core/* , app/models/*             │
│  errors, SSRF guard, sanitizer, config, Pydantic schemas       │
│  pure functions / data — no I/O, trivially testable            │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│  Persistence            app/db/*  (Supabase / PostgreSQL)      │
└─────────────────────────────────────────────────────────────┘
```


---

## Provider abstraction (Strategy + Adapter + Factory)

```
                         ┌────────────────────┐
   pipeline / discovery  │   ProviderFactory   │  role-based resolution:
   ─── asks by ROLE ───► │  (registry)         │  summarizer() judge()
                         └─────────┬──────────┘  discovery() embedding()
                                   │              web_search()
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  ChatProvider              EmbeddingProvider           SearchProvider
  (interface)               (interface)                 (interface)
        ▲                          ▲                          ▲
   ┌────┴─────┐              ┌──────┘               ┌──────────┼──────────┐
   │          │              │                      │                     │
 Google   OpenAI         Google              Brave                 Serper
 adapter  adapter        embedding           adapter               adapter
(Gemini) (optional,      adapter (Gemini)
          any OpenAI-
          compatible)
   └────┬─────┘                                    └─────────┬─────────┘
        │                                                    │
 FallbackChatProvider (primary→fallback)      FallbackSearchProvider
 summarizer: gemini-pro-latest → gemini-flash-latest    search: Serper (default)
 judge:      gemini-flash-latest → gemini-2.5-flash             Brave adapter optional
```

> The OpenAI adapter is kept as an **opt-in cross-family judge path**: set
> `OPENAI_API_KEY` + `OPENAI_BASE_URL` (Groq, OpenRouter, OpenAI…) and the
> factory routes the judge there instead, with no code change. By default
> the judge runs on a second Gemini model — same family as the summarizer,
> which is a weaker check, and that trade-off is stated rather than hidden
> (see [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) #10). There is no
> Anthropic adapter; the app isn't configured for it, so shipping one would
> be dead code.

- **Strategy:** callers hold a `ChatProvider` and don't know the concrete
  class.
- **Adapter:** each vendor SDK is wrapped to fit the interface, including
  translating its exceptions to `app.core.errors`.
- **Factory:** `ProviderFactory` is the single place that imports concrete
  adapters and maps a **role** (from config) to a wired provider.
- **Fallback** is a decorator of the *same interface*, so it composes
  uniformly — including across two unrelated REST search APIs, which
  LangChain's own `.with_fallbacks()` can't do (it only composes
  Runnables). Both are used where each fits.

---

## Provider middleware stack

Fallback was the first decorator of the provider interfaces. Once there was
one, the rest of the cross-cutting concerns had an obvious home, and
`app/providers/middleware.py` is the result: each layer implements a
provider interface *and* wraps one, so none of them knows what it is
wrapping and they compose in any combination.

```
 ProviderFactory.summarizer(settings, ledger)
 ─────────────────────────────────────────────
   MeteredChatProvider          token accounting + one audit line per call
        │
   BudgetGuardChatProvider      refuses once the run's token cap is spent
        │
   FallbackChatProvider         primary model → fallback model
        │
        ├── CircuitBreakerChatProvider → Gemini adapter (primary)
        └── CircuitBreakerChatProvider → Gemini adapter (fallback)

 ProviderFactory.embedding(settings)
 ─────────────────────────────────────────────
   CachingEmbeddingProvider     exact content-hash cache, LRU-bounded
        │
   RetryingEmbeddingProvider    backoff + full jitter
        │
   Google embedding adapter
```

**Two placements are load-bearing**, and the obvious alternative is wrong in
each case:

- **Breakers sit *inside* the fallback, one per concrete model.** Wrapped
  outside, an open breaker would refuse the primary *and* the fallback —
  backwards, since the moment the primary is known-bad is the moment the
  fallback matters most. Inside, an open primary fails instantly and the
  fallback answers, so the breaker becomes a latency *win* (skip three
  doomed retries and a timeout) rather than an outage.
- **The budget guard sits *outside* the fallback.** Inside, a run that had
  exhausted its budget would still burn one fallback call before giving up.

Both placements are pinned by tests (`tests/test_provider_factory.py`),
because they're the kind of thing a later refactor reorders without
noticing.

**What this replaced.** Token accounting used to be a
`usage: dict[str, int] | None` parameter threaded through `run_pipeline` →
`summarize()` → `judge_claims()` → `_judge_one()` and mutated by hand at two
call sites. It's now one layer, and those four signatures lost a parameter.
The pattern removed code rather than adding a framework — which is the test
of whether a pattern was worth applying.

**Layers talk through a small shared object.** `RunLedger` is written by the
metering layer and read by the budget layer — one instance per run, created
by the orchestrator, so two concurrent runs can't spend each other's budget.
That's deliberately not a module-level global and deliberately not more
parameter threading.

**Degradation is inherited, not re-implemented.** `BudgetExceededError`
subclasses `ProviderError`, so the judge stage's existing
"provider unavailable → mark this claim unverified and continue" path
already covers budget exhaustion: a run that runs out of budget mid-
verification finishes partially verified instead of failing. One handler,
two failure modes, because they mean the same thing to that stage.

**Caching is deterministic on purpose.** The embedding cache keys on an
exact hash of `(model, text)` — not a similarity match. A semantic cache
that returns a stored vector when the query is *close enough* would, here,
verify claim A against the passages retrieved for claim B and report the
result as verified. The win in this app comes from identical input anyway
(the "re-run with the same sources" flow re-embeds byte-identical
documents), so the cheap deterministic hit is available without taking the
clever probabilistic risk.

**One honest asymmetry:** chat retries *inside* the adapter (LangChain's
`.with_retry()`, which understands the SDK's retryable states and sits
correctly inside the structured-output binding), while embeddings retry in a
decorator, because `GoogleGenerativeAIEmbeddings` exposes no equivalent.
Embeddings previously had no retry at all — which mattered, since a run
embeds hundreds of chunks and is the stage most likely to trip a free-tier
rate limit. Two mechanisms at one layer, each chosen for what the SDK
underneath actually offers, rather than false uniformity.

---

## Run lifecycle

```
  Phase A (discovery)                Phase B (research pipeline)
  ───────────────────                ───────────────────────────
  POST /api/runs/discover            POST /api/runs/{id}/start  (SSE)
        │                                  │
   DISCOVERING(more sources)           PROCESSING
   web search + rank + SSRF                │
        │                            Stage 0  input guard
   AWAITING_APPROVAL ◄── human       Stage 1  fetch ──┐ pause on blocked src:
        │        approval gate                │        await asyncio.Event,
   POST /api/runs/{id}/approve        (paste/skip ◄──┘ heartbeat, 10-min cap
        │                            Stage 2  retrieve (in-memory RAG)
    PENDING ──────────────────►      Stage 3  summarize (structured output)
                                     Stage 4  judge (vs. original source)
                                           │
                                     COMPLETE | FAILED
```

Discovery failure is **non-blocking**: if all searches fail, the run sets
`discovery_skipped=true`, shows an empty approval screen, and proceeds
with the user's original URLs. Discovery is never a run failure.

### Why in-process instead of a job queue

A durable ARQ + Redis worker would let a run survive a worker restart
(that's the [future enhancement](FUTURE_ENHANCEMENTS.md) #1). The current
build runs the pipeline in the request and streams over SSE. Two pause
points are handled without a queue:

- **Discovery approval** doesn't pause a running task at all: `/discover`
  writes candidates to the DB and returns; the run simply sits at
  `AWAITING_APPROVAL` until `/approve` writes the decisions and `/start`
  begins the pipeline. No in-memory state — this is plain request/response
  around DB rows.
- **Blocked source** (unreachable, or a classified login/authwall page)
  *does* pause the running fetch stage, on an `asyncio.Event` per source (see
  `pipeline/unreachable.py`), woken when `POST /sources/paste` or
  `POST /sources/skip` lands, with a heartbeat ping and a timeout cap.

**Cost of this choice:** the unreachable-source waiter map lives in process
memory, so the backend runs as a **single replica / single worker** (Azure
Container Apps `min=max=1`). A process restart loses an in-flight waiter
(the DB rows persist, so it surfaces as a stalled run, not data loss). This
is the one notable trade-off of the in-process approach. Scaling out later = move the waiter state to Redis (see
[`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md) #1).

---

## Retrieval: per-source isolation

A global similarity search over all chunks can return Competitor A's page
as the closest match for a topic about Competitor B, and the summarizer
then attributes B's claim to A's URL. Instead, retrieval runs **separately
per (topic, source URL)** and results are presented in labelled blocks:

```
[Source: https://competitor-a.com/newsroom]
  <top passages for each topic, from A only>

[Source: https://competitor-b.com/blog]
  <top passages for each topic, from B only>
```

The structured-output schema then constrains each claim's `source_url` to
one of these labelled URLs. Block ordering is highest-relevance-first,
second-highest-last (primacy/recency), with the injection guard repeated
at the end of the prompt.

---

## Hallucination judge: verify against source, not self

```
  For each claim produced by the summarizer (Gemini):
    1. load the ORIGINAL scraped markdown for claim.source_url
    2. embed the claim; semantic-search the top-3 actual chunks
    3. if best similarity < threshold → low_confidence, no LLM call
    4. the judge (a different model which is not same as Summarizer model) evaluates:
       does {claim} follow from {actual chunks}?  — no self-grading
    5. verdict ∈ supported | partial | unsupported | low_confidence
```

The claim is judged against **what the source actually said**, not
against the passage the summarizer wrote. If the summarizer hallucinated a
supporting passage, judging it against that passage would confirm the
hallucination; judging against the source catches it. **Honest limitation:
the judge is itself an LLM** — it reliably catches *unsupported* claims but
is not a guarantee of total factual correctness. This is stated in the UI
and the docs, not glossed over.

---

## The guardrail ladder: cheap and deterministic first

Grounding isn't one check, it's a sequence of them ordered by cost. Each
rung rejects what it can, so the expensive rung only ever sees what the
cheap rungs couldn't decide. Nothing here is novel individually — the
discipline is refusing to reach for the model until the free checks are
exhausted.

| # | Rung | Cost | Catches | Where |
|---|------|------|---------|-------|
| 1 | Input bounds + SSRF | free | oversized/abusive requests, internal-network URLs | `models/schemas.py` |
| 2 | Injection strip — sources **and** user text | free | adversarial instructions in scraped pages, pasted text, and the user's own guidance field | `core/sanitize.py` |
| 3 | Per-source retrieval | free | cross-source attribution, structurally | `pipeline/retriever.py` |
| 4 | Schema-constrained output | free | prose parsing; `source_url` is a field, not a regex capture | `models/schemas.py` |
| 5 | **Structural attribution check** | free | claims citing a URL that was never fetched | `pipeline/orchestrator.py` |
| 6 | Embedding similarity floor | cheap | claims with no matching content in their source | `pipeline/judge.py` |
| 7 | LLM judge, verdict + confidence | expensive | claims that contradict or overstate their source | `pipeline/judge.py` |

Rung 5 is worth calling out because its absence was a real defect. The
prompt instructs the model to copy a `source_url` verbatim from a
`[Source:]` label, and the schema constrains it to a string — but neither
can stop the model emitting a URL that was never fetched. Such a claim used
to reach the judge, miss its index lookup, and come back `low_confidence`
with the reason *"no retrievable content for the cited source"* — the same
outcome as a genuinely thin source, while still rendering in the report as a
clickable link to a page that never backed it. A set-membership test
separates the two cases for nothing, and the count lands in run metrics
(`claims_dropped_unattributed`) so a model that starts inventing URLs is
visible rather than silently absorbed.

Two rungs are enforced by *structure* rather than by asking the model
nicely — per-source retrieval (3) makes cross-source attribution
impossible rather than discouraged, and the attribution check (5) verifies
the model complied rather than trusting that it did.

---

## Evaluation: measuring the guardrails, not just testing them

Unit tests answer "does the judge stage do what the code says?". They can't
answer "does it actually catch hallucinations?" — that's a property of a
model, a prompt, and a threshold, none of which are control flow. So there
is a labelled dataset and a scorer in [`backend/evals/`](backend/evals/).

The dataset uses **fictional companies and invented facts**, deliberately: a
document about a real company would let a model score well from pretraining
knowledge instead of from the passages, which is the exact failure being
measured. Each claim is labelled with the *defect* it plants — overstated,
unsupported, contradicted, cross-source, fabricated URL, off-topic — so the
report says which kind of hallucination survives, not just how many.

Three tiers, mirroring the ladder:

- **Deterministic tier** (runs in CI, no keys, no network) pairs the free
  rungs with a deliberately **credulous** judge that marks everything
  supported. Whatever the ladder still catches under that was caught for
  zero cost and can't silently regress behind a model change. Current
  measurement: **60% of planted hallucinations caught before any LLM call,
  at a 100% pass rate on faithful claims** — i.e. not achieved by rejecting
  everything, which is the degenerate way to score well on recall.
- **Trajectory tier** (also in CI) checks *which rung* resolves each defect,
  asserting the ladder routes each to the cheapest correct one — a fabricated
  URL at the free structural check, an off-topic claim at the similarity floor,
  a contradiction only at the judge. Catching a structural defect at the model
  would be a correctness pass but a cost regression; only this tier sees that.
- **Model tier** (`pytest -m eval`, needs keys) scores the real judge on
  hallucination recall and over-strictness, with floors set below measured
  performance so it fails on regression rather than on model noise.

The headline metric is deliberately asymmetric. A missed hallucination
ships an unsupported claim wearing a "Supported" badge, damaging the one
thing the product sells. Over-strictness marks a true claim unverified — the
user sees a weaker report, not a wrong one. So recall is the number to hold,
and the faithful-pass-rate is tracked next to it purely to catch the
degenerate optimisation.

The offline tier uses a hashed bag-of-words embedder as a stand-in, since
the hash-derived vectors used elsewhere in tests have no meaningful notion
of distance and can't exercise a *threshold*. Its absolute scores are an
artefact of that proxy, so the durable assertion is threshold-independent:
faithful claims must outrank topically-unrelated ones with a gap the floor
sits inside. Calibrating the real threshold is the model tier's job.

---

## Observability and abuse control

**Request correlation.** An ASGI middleware assigns (or forwards) a
12-character request id, holds it in a `ContextVar`, and echoes it as
`X-Request-ID`. A logging filter stamps it onto every record, so a
`logger.info(...)` five frames deep inside the pipeline is correlated
without anything having to pass the id around. Logs are one flat JSON object
per line in production (indexable by an aggregator) and plain text locally,
because JSON is unreadable when tailing a dev server.

A `ContextVar` rather than a thread-local because the app is asyncio — each
task gets its own copy, so concurrent runs can't overwrite each other's id.
The reset happens in a `finally`, so a request that raises can't leak its id
into whatever the loop runs next.

**Why this is ASGI middleware and auth isn't.** Middleware runs
unconditionally, so 404s, 422s from request validation, and unhandled
exceptions all get an id — exactly the requests worth correlating.
Dependencies run per-route and can return a typed value, which is what auth
and rate limiting need. The two mechanisms are used for what each is for,
and registration order is set so the correlation layer is outermost.

**Rate limiting.** The deployment is public and backed by free-tier provider
keys whose quotas are shared across all users. Input bounds cap what one
*request* can cost; they don't cap how many requests one user can issue, so
a user clicking "Re-run" repeatedly could exhaust the shared Gemini quota
for everyone. A per-user sliding window (a deque of timestamps, pruned on
read) guards `/discover` and `/start`, returning `429` with `Retry-After`.

Sliding, not fixed: a fixed window permits a full burst either side of the
reset — double the intended rate at exactly the moment an impatient user
retries. In-process, which is *accurate* rather than approximate here
because the backend already runs single-replica; if that constraint is
lifted this must move to Redis in the same change, or N replicas silently
permit N× the rate.

---

## Data model

Four tables — `runs`, `discovery_candidates`, `run_sources`, `claims`
([`backend/db/schema.sql`](backend/db/schema.sql)). The report is derived
from the normalized `claims` table (no denormalized `result` JSON column),
which removes the write-consistency split between "report blob" and
"claim rows" and makes cross-run change detection a simple diff query.

Ownership is enforced in application code by Clerk user id; RLS policies
exist as defense-in-depth but do not fire for the service-role backend —
see the schema header for the precise, honest explanation.

---

## Streaming (SSE)

`POST /api/runs/{id}/start` returns a `text/event-stream`. The pipeline
runs as a background task pushing events onto a queue; the response drains
the queue and formats SSE frames (`pipeline/events.py`). A `: ping`
heartbeat every 15s keeps Azure's Envoy proxy from closing the idle
connection during a slow fetch or an unreachable-source pause. A replay
buffer and event-ID machinery (a [future enhancement](FUTURE_ENHANCEMENTS.md)
#3) aren't needed here — there's a single held-open connection with nothing
to reconnect to at these run durations.

**SSE auth:** the frontend consumes the stream with `fetch()` +
`ReadableStream`, *not* `EventSource` — so the Clerk JWT travels in the
`Authorization` header like every other request. There is no token in the
query string at all, which sidesteps the "JWT in every access log" problem
without needing a separate per-run stream token.

---

## Auth

Clerk with **Google OAuth only** (no email/password, no magic links).
Protected Next.js routes gate on the Clerk session; the FastAPI backend
verifies the Clerk JWT via a single DI dependency and resolves the user
id used for ownership checks. Rationale and the trade-off (excludes
non-Google users, acceptable for this audience) in
[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).
