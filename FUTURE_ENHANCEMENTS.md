# Future Enhancements

The current application ([`ARCHITECTURE.md`](ARCHITECTURE.md)) is scoped as
a lightweight, single-process app that runs the research pipeline in-request
and streams progress over SSE. That's the right shape for the problem and
the expected load.

This document describes the architecture it's **designed to grow into** if
it needed to serve real, concurrent production traffic — and, importantly,
*what each enhancement buys and when it becomes worth the added
operational cost*. It is the roadmap and the "why / when" for each
enhancement.

The guiding principle: none of these are missing pieces. Each is a
deliberate deferral, and the current code is structured so each can be
adopted without rewriting business logic — the provider interfaces, the
role-based factory, and keeping pause state in one named module rather than
scattered through the pipeline were all chosen with these upgrades in mind.

That claim has since been tested. Three things listed here as future work —
per-call cost attribution, a caching layer, and the change-detection and
monitoring stretch goals — have been **built**, and each landed without
touching pipeline business logic: metering and caching went in as decorators
of the existing provider interfaces (`app/providers/middleware.py`), and
change detection was a diff query over the normalized `claims` table. They
are marked ✅ below and kept in this document, because "the seam worked"
is more informative than quietly deleting the entry.

---

## 1. Durable execution — ARQ worker + Redis

**Today:** the pipeline runs inside the HTTP request. If the container is
redeployed or restarted mid-run, that run is lost (its DB rows persist, so
it surfaces as a stalled run, not corrupted data).

**Enhancement:** move the pipeline into a dedicated always-on **ARQ worker
container** with a **Redis** broker. The HTTP server only enqueues jobs
and serves results. The worker heartbeats to `runs.last_heartbeat`; a cron
marks a run `FAILED` if the heartbeat goes stale, so a killed worker never
leaves a run stuck at `PROCESSING` forever.

**Buys:** durability across scale-to-zero, redeploys, and health-check
restarts; graceful cancellation via a Redis cancel key; horizontal scale
(many workers).

**Worth it when:** the app runs on infrastructure that recycles processes
under load, or runs get long/expensive enough that losing one on a deploy
is unacceptable. **Cost:** a second container + a managed Redis (~\$16/mo)
and a job model to operate.

> This is also what removes the current build's one honest constraint —
> the single-replica requirement. Today the unreachable-source pause holds
> its waiter state (an `asyncio.Event` map in `pipeline/unreachable.py`) in
> process memory, so the backend must run as one replica. Moving that pause
> state to Redis (a pub/sub or a key the paste/skip endpoint sets, that
> the fetch stage waits on) lets the backend scale out. Discovery approval
> already needs no in-memory state — it's plain request/response around DB
> rows — so nothing there blocks scaling.
>
> **Three other pieces of process-local state must move in the same change,
> not afterwards**, because each degrades quietly rather than loudly when
> replicated: the rate-limiter windows (`core/rate_limit.py`) — N replicas
> would permit N× the intended rate with nothing failing to signal it; the
> circuit-breaker state (`providers/middleware.py`) — each replica would have
> to learn independently that a model is down; and the embedding cache, which
> would simply hit less often (the benign one). Listing them here rather than
> discovering them later is the point: "make it stateless" is a single change
> touching four places, and the rate limiter is the one that would be a
> silent regression.
>
> There is also a **cheaper intermediate step** that removes the constraint
> without adding Redis at all: replace the `asyncio.Event` with a bounded
> poll on `run_sources.status`, so the pause becomes DB state exactly like
> the approval gate already is. It costs up to one poll interval of wake
> latency on a paste and buys multi-replica capability using infrastructure
> already paid for. The rate limiter would still need a shared store to be
> exact, but it degrades to per-replica limits rather than breaking.

---

## 2. Persistent vector store — pgvector

**Today:** retrieval is in-memory (embed a run's chunks, NumPy cosine
search), scoped to a single run. Sub-millisecond for ≤10 URLs, no extra
infra.

**Enhancement:** store chunks and embeddings in a **pgvector** table with
an `ivfflat` index, keyed by `url_hash + chunk_index`.

**Buys:** cross-run embedding reuse (if two users research the same URL,
it's embedded once); observable retrieval (similarity search is SQL,
inspectable and `EXPLAIN`-able); no in-process index memory pressure when
many runs are concurrent.

**Worth it when:** there's enough repeat-URL overlap across runs for reuse
to matter, or concurrency is high enough that per-run in-memory indexes
strain container memory. **Cost:** the pgvector extension + an index to
maintain.

---

## 3. Resilient streaming — SSE replay buffer + event IDs

**Today:** SSE streams from the held-open request with a heartbeat ping to
survive proxy idle timeouts. A single connection, nothing to reconnect to.

**Enhancement:** assign monotonic event IDs, buffer the last N events per
run in Redis (short TTL), and replay from `Last-Event-ID` on reconnect.

**Buys:** a browser that drops and auto-reconnects mid-run resumes exactly
where it left off instead of missing the progress it didn't see.

**Worth it when:** runs get long enough, or clients flaky enough, that
mid-run reconnects are common. At current run durations (~30s) it's
overkill. **Cost:** Redis (shared with #1) + event-buffer bookkeeping.

---

## 4. Operational depth — audit log, URL cache, cost attribution

**Partially built.** ✅ Per-run cost attribution and an embedding cache
exist; the persistent audit table and the URL cache remain future work.

**What's done:** the metering middleware
(`app/providers/middleware.py`) counts tokens and calls per run through a
`RunLedger` and emits one structured audit line per LLM call; the totals
persist in `runs.metrics` (`input_tokens`, `output_tokens`, `llm_calls`),
alongside the discovery **search-API hit count** and an **estimated dollar
cost** at standard paid rates (`app/core/costs.py`) — all surfaced in a per-run
cost/usage panel on the report page. A `BudgetGuard` layer enforces a per-run
token ceiling and degrades the run rather than failing it. An LRU embedding
cache keyed on an exact `(model, text)` hash makes the "re-run with the same
sources" flow re-embed nothing.

**Still future:** an `llm_audit_log` **table** (per-call model, token
counts, input/output hashes) — today the per-call detail is a log line and
only the aggregate is queryable, so "what exactly did the model see in run
X three weeks ago" isn't answerable from the database. And a `url_cache`
(24h TTL) so a re-fetched URL skips the crawl: currently the embedding cache
saves the embedding call on a re-run, but the page is still fetched.

**Worth it when:** the app has real users to bill or budget against, or
support cases that need a per-call paper trail.

---

## 5. Stretch features from the brief

- **Change detection** ✅ **built** — "what's new since last run" is a diff
  over `claims` rows (`app/changes.py`, `GET /api/runs/{id}/changes`, the
  `WhatsNew` component). It stayed a small feature precisely because the
  report is derived from the normalized `claims` table rather than a JSON
  blob (DESIGN_DECISIONS #13) — the data model decision paid for itself here.
- **Monitoring** ✅ **built, at application level** — per-run duration,
  sources fetched, claims total/verified, token counts, LLM call count,
  search-API hits, estimated cost, and dropped-attribution count persist to
  `runs.metrics` and render as a stats strip on the run detail page.
  Operationally there's `/healthz` for liveness and `/readyz` for a deep
  check of Gemini, Serper and Postgres plus the effective config, and every
  log line carries a request id (`X-Request-ID`) so one run's path through
  the pipeline can be pulled out of an aggregator.
- **Distributed tracing — still future.** The metrics above are per-run
  aggregates, not spans. LangChain's OpenTelemetry callbacks + LangSmith,
  with Azure Monitor ingesting OTEL traces, would give per-stage timing and
  cross-service correlation. Worth it when there are enough concurrent runs
  that "which stage is slow, for whom" stops being answerable by reading one
  run's metrics.

---

## 6. Evaluation — a larger dataset, end-to-end runs, CI gating

**Today:** a labelled dataset of fictional sources with planted defects
scores the guardrails in three tiers (`backend/evals/`, DESIGN_DECISIONS #16):
a deterministic tier in CI, a **trajectory tier** that checks each defect is
routed to the cheapest correct rung of the guardrail ladder
(`evals/trajectory.py`), and a live-model tier behind `pytest -m eval`.

**Enhancement:** grow the dataset well beyond its current size, stratify it
by defect type and source genre (press release, blog, analyst note,
paywalled excerpt); add **full end-to-end examples** that run the whole
pipeline (fetch → retrieve → summarize → judge) against fixture pages, not
just the verification slice; and run the model tier on a schedule with the
scores tracked over time rather than only asserted against a floor.

**Buys:** the ability to compare two judge models, or two prompt versions,
on evidence instead of impression — and enough samples that a few points of
movement is signal rather than noise. At the current dataset size the
headline recall number is directional, which is honest to state and is the
main limitation of the harness as built. The trajectory tier already runs
offline; a live end-to-end tier needs a fixture crawler and real keys, which
is why it's deferred.

**Worth it when:** prompts or models change often enough that regression
risk is real, or someone other than the author is editing them.

---

## 7. Organization sign-in — domain restriction and enterprise SSO

**Today:** Clerk with Google OAuth as the only sign-in method
(DESIGN_DECISIONS #14). Any Google account can sign in.

**Enhancement, in three tiers of increasing effort:**

1. **Org email already works if the org runs Google Workspace.** A
   `name@progress.com` address backed by Google Workspace *is* a Google
   account, so it signs in through the existing Google OAuth with no change —
   worth verifying before building anything, because it may already be done.
2. **Domain allowlist** — restrict sign-ups to one or more domains (e.g.
   `progress.com`) via Clerk's restrictions/allowlist settings. Low effort,
   no code change, keeps out personal Gmail accounts. This is the cheap
   near-term option if the audience is a single company.
3. **Enterprise SSO / SAML** — connect the org's IdP (Okta, Entra, Google
   Workspace SAML) so employees sign in through corporate SSO with
   provisioning/deprovisioning handled centrally. This is a Clerk
   **B2B/Enterprise** feature with real cost and per-connection setup.

**Buys:** (2) keeps the app scoped to a company's own users for free; (3)
adds centralized identity lifecycle and audit that an enterprise buyer
expects. The provider seam is unaffected — this is all auth configuration,
not application code.

**Worth it when:** the app moves from a personal/demo tool to something a
specific organization adopts (2), or an enterprise buyer requires SSO (3).

---

## 8. PDF upload for blocked sources

**Today:** when a source is blocked (unreachable or a login/authwall page),
the run pauses and the user can **paste the content** or **continue without
that source** (`components/SourceFallback.tsx`). A backend PDF-upload endpoint
exists (`POST /sources/upload`, MIME-validated, text-only `pdfplumber`
extraction, 10MB cap) but the UI leads with paste/skip.

**Enhancement:** surface PDF (and eventually DOCX/HTML file) upload in the
recovery UI, so a user with a saved copy of a paywalled article can drop the
file in rather than copy-pasting its text. The extracted text already flows
through the same sanitize pass as scraped content, so downstream stages need
no change.

**Buys:** a smoother recovery path for sources that live as documents rather
than reachable pages. Paste already covers the functional need, so this is
ergonomics, not capability — which is why it's deferred.

**Worth it when:** users routinely research sources they hold as files
(analyst PDFs, saved paywalled articles) rather than URLs.

