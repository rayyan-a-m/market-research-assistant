# Design Decisions

Each decision below lists the **choice**, the **alternative rejected**, the
**failure mode avoided**, and where the code and tests for it live. Everything
in this document is built and deployed; things that were considered and *not*
built are in the last section, and things deferred to a larger version are in
[`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md).

One meta-decision runs through several entries: scoping this as a lightweight
application, and deferring heavier production concerns — a durable queue,
pgvector, SSE replay — rather than building them speculatively. Choosing not
to build something is a design decision too, so those are argued here with the
same structure as the things that were built.

---

## Foundations: providers, boundaries and cross-cutting concerns

### 1. Provider abstraction: Strategy + Adapter + Factory

**Choice:** LLM/embedding/search access sits behind three interfaces
(`ChatProvider`, `EmbeddingProvider`, `SearchProvider`); per-vendor
adapters implement them; a registry `ProviderFactory` resolves a provider
by **role** (`summarizer`, `judge`, `discovery`, `embedding`,
`web_search`) from config.

**Rejected:** calling `ChatGoogleGenerativeAI(...)` / `ChatOpenAI(...)`
directly inside the summarizer and judge functions.

**Why:** the direct approach welds business logic to a vendor. Swapping
the judge model, or adding a provider, would mean editing pipeline code
and re-testing it. With the factory, it's a registration + a config value.
This wasn't hypothetical, and it got tested twice. The models first moved
from an early Claude/OpenAI plan to **Gemini** for summarization,
embeddings, and discovery ranking. Then the judge's original host (GitHub
Models) was retired mid-build and the judge had to move again. Both
migrations touched only the adapters, the factory, and config — no pipeline
code, no re-test of business logic. That's dependency inversion earning its
keep rather than being quoted: the pipeline depends on an interface it owns,
and the vendor SDKs depend on fitting it. The abstraction also gives one
obvious home for retries, timeouts, and error mapping, and later became the
seam the whole middleware stack hangs off (#5a).

**Status:** implemented, tested (`tests/test_provider_factory.py`,
`test_fallback.py`).

### 2. Normalized error hierarchy at the adapter boundary

**Choice:** adapters translate vendor exceptions (`google.api_core.*`,
`openai.*`, `httpx.*`) into `ProviderTimeoutError` /
`ProviderRateLimitError` / `ProviderUnavailableError` /
`ProviderResponseError`. Callers only ever catch `ProviderError`.

**Rejected:** letting vendor exceptions propagate and catching them in the
pipeline.

**Why:** otherwise every orchestration site needs to know all three
vendors' exception taxonomies, and adding a provider means auditing every
`try/except`. Normalizing at the boundary keeps failure handling as
design, not as scattered reaction — and makes "degrade, don't die"
(fallback provider, mark-unverified-and-continue) expressible in one place.

**Status:** implemented.

### 3. SSRF guard: pre-fetch IP resolution, IPv4 **and** IPv6

**Choice:** resolve each hostname before fetching; block private,
loopback, link-local (incl. Azure IMDS `169.254.169.254`), unspecified,
reserved, multicast — for both address families via `getaddrinfo`.

**Rejected:** (a) no guard; (b) `gethostbyname`, which is IPv4-only.

**Why:** a URL pointed at the Azure metadata endpoint would hand back the
container's managed-identity token. The IPv4-only version silently let an
IPv6 loopback literal through as "unresolvable" **and** would have
wrongly rejected legitimate public IPv6 hosts — a real bug the test suite
caught during development, now fixed and regression-tested.

**Status:** implemented, tested (`tests/test_security.py`).

### 4. Input bounds and SSRF enforced at the Pydantic boundary

**Choice:** `max_urls`, `max_topics`, `max_competitors`, HTTPS-only, dedup,
and the SSRF check all run in the request model's validators.

**Rejected:** `if len(urls) > 10: ...` checks inside route handlers.

**Why:** enforcing at the boundary means every downstream function can
assume clean, bounded, safe input — no defensive re-checking, no path
where an oversized or unsafe request reaches the pipeline. It's also the
cost-abuse backstop: a 50-URL × 20-topic request is rejected before it
spends a cent.

**Status:** implemented, tested (`tests/test_schemas.py`).

### 5. Sanitize all source text, regardless of how it arrived

**Choice:** one `sanitize_source_content` pass (injection-pattern
stripping + light PII redaction + truncation) applied to scraped, pasted,
and PDF-extracted text alike.

**Rejected:** trusting scraped/extracted content, or only sanitizing
scraped (not pasted) input.

**Why:** competitor pages are attacker-controlled; a CSS-hidden "ignore
previous instructions…" can survive markdown extraction and reach the
prompt. Pasted text is equally untrusted. Regex isn't a complete defense
(paired with a system-prompt guard as a second layer), but it's cheap and
uniform, and a false positive costs one chunk, not the document.

**Status:** implemented, tested (`tests/test_sanitize.py`).

### 5a. Cross-cutting provider concerns as composable middleware

**Choice:** metering, spend cap, circuit breaking, caching, and embedding
retry are decorators of the provider interfaces
(`app/providers/middleware.py`), composed once in the factory. Each
implements a provider interface and wraps one.

**Rejected:** (a) putting each concern where it's needed — token counting in
the pipeline, a cache in the retriever, breaker state in the adapters;
(b) a single "instrumented provider" class doing all of it.

**Why:** the seam already existed. `FallbackChatProvider` was a decorator of
`ChatProvider` from the start, which is the proof that the interface
composes; once one concern lives there, putting the next one anywhere else
is the inconsistent choice. (b) fails the open/closed test — every new
concern edits one growing class, and you can't have metering without a cap,
or caching in tests without a breaker.

The evidence that this was worth doing is that **it deleted code**. Token
accounting was a `usage: dict[str, int] | None` parameter threaded through
`run_pipeline` → `summarize()` → `judge_claims()` → `_judge_one()` and
mutated by hand at two call sites; it's now one layer and those four
signatures lost a parameter. A pattern that only ever adds indirection
wasn't earning its place.

Two orderings are load-bearing and both are pinned by tests, because they're
exactly what a later refactor reorders without noticing:

- **Breakers inside the fallback**, one per model. Outside, an open breaker
  refuses primary *and* fallback — backwards, since the primary being
  known-bad is precisely when the fallback matters. Inside, an open primary
  fails instantly and the fallback answers, making the breaker a latency win
  rather than an outage.
- **Budget guard outside the fallback.** Inside, an exhausted run would
  still burn one fallback call, making the cap advisory.

`BudgetExceededError` subclasses `ProviderError` so the judge's existing
degrade-don't-die path covers budget exhaustion with no new handling code —
a run that runs out mid-verification finishes partially verified rather than
failing.

**Status:** implemented, tested (`tests/test_middleware.py`,
`tests/test_provider_factory.py`).

### 5b. Deterministic content-hash cache, not a semantic one

**Choice:** the embedding cache keys on an exact `sha256(model, text)`, is
LRU-bounded, and is shared process-wide.

**Rejected:** a similarity-keyed ("semantic") cache that returns a stored
vector when the incoming text is close enough — the pattern in most agent
frameworks.

**Why:** a near-miss hit here would verify claim A against the passages
retrieved for claim B and report the result as verified. The one place in
this system where correctness is load-bearing is the grounding check, and a
probabilistic cache in front of it trades exactly the wrong thing for
latency. The available win doesn't need it either: the caching payoff in
this app is the "re-run with the same sources" flow, which re-embeds
*byte-identical* documents. Take the deterministic hit; decline the clever
risk. LRU-bounded because the cache outlives a run — an unbounded dict keyed
by document text is a memory leak with a slow fuse.

**Status:** implemented, tested (`tests/test_middleware.py`).

### 5c. Structural attribution check on summarizer output

**Choice:** a claim whose `source_url` is not in the set of URLs that were
actually fetched is dropped before the judge stage, and the count is
recorded in run metrics.

**Rejected:** (a) trusting the prompt instruction to copy URLs verbatim plus
the schema's string constraint; (b) flagging such claims instead of dropping
them.

**Why:** this fixed a real defect. Neither a prompt nor a string-typed field
can stop a model emitting a URL that was never fetched. Such a claim used to
reach the judge, miss its index lookup, and return `low_confidence` with
*"no retrievable content for the cited source"* — indistinguishable from a
genuinely thin source, while still rendering in the report as a clickable
link to a page that never backed it. (b) was rejected because the UI would
still show the fabricated link; the failure mode is the link, not the label.
The check is free set membership, so it belongs below every paid rung.
Recording the count keeps it observable: a model that starts inventing URLs
should show up in metrics, not be silently absorbed.

**Status:** implemented, tested (`tests/test_orchestrator.py`,
`tests/test_evals.py`).

### 5d. Injection guard on user-authored text, not only on sources

**Choice:** topics, competitors, and the analyst-guidance `context` string
are injection-stripped in the Pydantic validators, using a function separate
from the source-content sanitizer.

**Rejected:** (a) sanitizing only third-party source content, on the grounds
that the user isn't attacking themselves; (b) reusing
`sanitize_source_content` unchanged.

**Why:** the `context` field is interpolated into the summarizer prompt whose
system rules govern source attribution, so an injection there can make the
model fabricate attributions in a report the user then forwards to
colleagues who never saw the input — the blast radius is a downstream
reader, not the author. (b) was rejected because the threat models genuinely
differ: source text is fully untrusted and also gets PII redaction and a
length cap, while redacting PII out of a user's own guidance would be
destructive (they may legitimately want to name a person) and the length is
already bounded by the field. Enforcing at the boundary is the same
principle as #4 — downstream code assumes prompt-safe text without
re-checking.

**Status:** implemented, tested (`tests/test_schemas.py`).

### 5e. Correlation in ASGI middleware, auth and rate limits in dependencies

**Choice:** request-id assignment and structured JSON logging are ASGI
middleware; auth and rate limiting are FastAPI dependencies.

**Rejected:** (a) doing all four as dependencies; (b) doing all four as
middleware.

**Why:** the two mechanisms have genuinely different properties and the
split follows from that, rather than from taste. Middleware runs
unconditionally — including for 404s, 422s from request validation, and
unhandled exceptions, which are precisely the requests you most want to
correlate; a dependency-based id would be missing from every one of them.
But middleware can't return a typed value into a handler's signature, which
is exactly what `user_id: str = Depends(current_user_id)` gives, and it runs
on routes that don't need it. So: unconditional cross-cutting concerns as
middleware, per-route concerns that produce a value as dependencies.

A `ContextVar` (not a thread-local) holds the id, because under asyncio each
task gets its own copy — a thread-local would be shared across concurrently
running requests on the same thread. The reset lives in a `finally` so a
failed request can't leak its id into the next thing the loop runs.

**Status:** implemented, tested (`tests/test_observability.py`).

### 5f. Per-user sliding-window rate limit on the expensive endpoints

**Choice:** a deque of timestamps per user, pruned on read, guarding
`/discover` and `/start`; `429` with a `Retry-After` header.

**Rejected:** (a) no limit, on the grounds that input bounds already cap
cost; (b) a fixed-window counter; (c) reaching for Redis immediately.

**Why:** (a) confuses two different limits. Input bounds (#4) cap what a
*single request* can cost. Nothing capped how many requests one user could
issue, and the deployment runs on free-tier keys whose quotas are **shared
across all users** — so one person holding down "Re-run" degrades the
service for everyone else. That's the actual threat model here, and it isn't
malice, it's impatience.

(b) A fixed window allows a full burst at the end of one window and another
at the start of the next — 2× the intended rate across the boundary, which
is exactly when a user who just got throttled tries again. A sliding window
has no boundary to exploit.

(c) In-process is not a compromise here, it's *correct*: the backend already
runs as a single replica (ARCHITECTURE, "why in-process"), so a per-process
window is the real limit rather than an approximation. The honest risk is
that lifting the single-replica constraint without also moving this to Redis
would silently permit N× the rate — so the two are noted as one change in
[`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md) #1, not as independent
items.

**Status:** implemented, tested (`tests/test_rate_limit.py`).

### 6. App factory + dependency injection, not module-level singletons

**Choice:** `create_app()` and DI-provided settings/providers, rather than
a global `app` and module-level SDK clients.

**Rejected:** `app = FastAPI()` + top-level client instances read from
`os.environ`.

**Why:** DI is what lets tests construct an isolated app and substitute
fake settings/providers without monkeypatching the environment or the
network. Every test in the suite runs offline because of this.

**Status:** implemented.

---

## The pipeline, the data and the product

### 7. In-process pipeline instead of a durable ARQ/Redis worker

**Choice:** run the four-stage pipeline inside the request and stream over
SSE; handle the two pause points (approval, unreachable source) with
in-process coordination.

**Rejected:** a durable ARQ worker + Redis broker + heartbeat + requeue
(the [future enhancement](FUTURE_ENHANCEMENTS.md) #1).

**Why:** durability across a *process restart* is not one of the
assignment's success criteria, and a ~30s run doesn't justify a second
always-on container, a broker, and a job model. The one piece of in-memory
state that forces single-replica is the unreachable-source waiter map
(`pipeline/unreachable.py`); the honest cost — that constraint and loss of
an in-flight waiter on restart — is documented (ARCHITECTURE.md,
deployment.md §0) rather than hidden. The upgrade path (move the waiter
state to Redis) is [future enhancement](FUTURE_ENHANCEMENTS.md) #1.

**Status:** implemented (`pipeline/events.py`, `pipeline/unreachable.py`),
tested (`tests/test_router_cancel_delete.py`, `tests/test_fetcher.py`).

### 8. In-memory retrieval instead of pgvector

**Choice:** chunk + embed + cosine-search in NumPy, scoped to one run.

**Rejected:** pgvector with an `ivfflat` index and cross-run embedding
reuse.

**Why:** for a single run, an in-memory search over a few
hundred chunks is sub-millisecond and needs no extra infrastructure.
Cross-run reuse and a persistent vector index are real optimizations —
for a system with real traffic, which this scoping explicitly isn't.

**Status:** implemented (`pipeline/retriever.py`), tested
(`tests/test_retriever.py` — chunking, cosine ranking, ordering).

### 9. Per-source retrieval, not global

**Choice:** retrieve top passages per (topic, source URL) separately;
schema constrains each claim's `source_url` to a labelled source.

**Rejected:** a single global similarity search across all chunks.

**Why:** global retrieval can surface Competitor A's page as the best
match for a topic about B, and the summarizer then mis-attributes the
claim. Per-source retrieval makes cross-source attribution structurally
impossible, not just discouraged.

**Status:** implemented, tested (`tests/test_retriever_context.py`).

### 10. Cross-model judge against the original source

**Choice:** the judge evaluates each claim against the top actual chunks of
the original scraped text; a sub-threshold similarity short-circuits to
`low_confidence` with no LLM call.

**Rejected:** (a) judging the claim against the summarizer's own generated
passage; (b) parsing a verdict out of prose instead of structured output.

**Why:** judging a claim against a passage the summarizer wrote confirms the
summarizer's own hallucinations — if it invented the supporting quote, the
quote will support the claim. Judging against what the source *actually*
said is the only version that tests grounding rather than internal
consistency.

**The cross-family part is a stated compromise, not a clean win.** The ideal
is a judge from a different model family than the summarizer, because
same-family judging is biased toward finding its own lineage's output
plausible. For the scope of this application the default judge is a second,
cheaper Gemini model (`gemini-flash-latest` grading `gemini-pro-latest`) — a
cross-*model* check within the same family. It is weaker than a true
cross-family judge: shared pretraining lineage means shared blind spots. It is
kept because it still tests each claim against the *original source* (the
property that matters most) at zero added dependency, and because switching to
a genuinely different family is a config change, not a rewrite.

The abstraction is what keeps this a config decision rather than a permanent
one. Because the judge is resolved by role, a second adapter for
OpenAI-compatible endpoints (Groq, OpenRouter, OpenAI) is enough to move
verification to another family: set `OPENAI_API_KEY`, `OPENAI_BASE_URL` and a
matching `JUDGE_MODEL` and `ProviderFactory.judge()` routes there, with no
code change and no pipeline re-test. That adapter is in the codebase and
covered by `tests/test_provider_factory.py`; the deployed app leaves it unset,
and `/readyz` reports which path is live so the answer isn't a guess.

Two limitations stated openly: the judge is itself an LLM, so it catches
unsupported claims rather than guaranteeing correctness; and by default it
shares a family with the summarizer. How well it actually performs is not
left to assertion — it's measured (#16), and the same-family default is
exactly the kind of change the eval exists to keep honest.

**Status:** implemented (`pipeline/judge.py`), tested (`tests/test_judge.py`,
`tests/test_evals.py`).

### 11. Source discovery: a deterministic service with a mandatory human gate

**Choice:** propose extra sources via web search (search → embedding-rank →
dedup → SSRF filter → rule-based rationale); the user must approve, reject,
or skip; nothing is added silently.

**Rejected:** (a) no discovery; (b) discovery that auto-adds sources and
tells the user after; (c) building discovery as a `create_agent` +
`HumanInTheLoopMiddleware` agent.

**Why the human gate:** a report's trustworthiness depends on the user being
able to vouch for every source behind every insight. Silent auto-add breaks
that; "we also used these" after the fact gives no chance to reject a biased
or irrelevant source before it shapes the report. The gate is a trust
mechanism first, a feature second. Cost: one interaction step, mitigated by
a one-click Skip.

**Why deterministic, not an agent:** search and ranking are *rules*, not
reasoning — there is no decision in "query these terms, embed the snippets,
sort by similarity to the topics, drop duplicates and unsafe URLs" that an
agent loop would make better than a function. An agent here would add tool-
call latency, non-determinism, and cost for no benefit, and would make the
behaviour harder to test. The actual judgement in this flow is the *human*
approval, which is a plain application step (candidates → DB → user decides
→ pipeline). This is the "don't put an LLM where a rule works" principle
applied deliberately. (A checkpointed agent version is noted as an
alternative, not an improvement.)

**Status:** implemented (`discovery/service.py`, `discovery/dedup.py`),
tested (`tests/test_dedup.py`, `tests/test_router_discover.py`).

### 12. Blocked source → pause and let the user choose, not silent failure

**Choice:** when a source can't yield usable content, pause the run and offer
the user two choices — **paste the article text** (it re-enters the pipeline
identically) or **continue without that source**.

**Rejected:** (a) marking the source failed and moving on silently; (b)
trusting the HTTP status — a login/authwall page returns 200 with real markup,
so "the fetch succeeded" is not "we got the article."

**Why:** paywalled/gated pages are exactly the high-value sources a user
*can* access in their own browser, so silent failure starves the research
with no signal. And because a wall returns a valid-looking 200, the fetcher
*classifies* the result (`fetcher._classify_markdown`): empty, too-thin, or
authwall-shaped content is treated as blocked and surfaced with the reason,
rather than sailing through as a "source" whose only content is "please log
in." The user is then in control — supply the text, or explicitly drop the
source — instead of the run guessing. Origin is tracked for provenance but
invisible to the analysis stages.

> Uploading a PDF is deliberately deferred to keep the recovery UI to two
> clear choices; paste already covers the functional need. The backend upload
> endpoint remains, and richer file upload is [`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md) #8.

**Status:** implemented (`pipeline/fetcher.py`, `pipeline/unreachable.py`,
`routers/sources.py`), tested (`tests/test_fetcher.py` — including the
authwall classifier — and `tests/test_router_sources.py`).

### 13. Report derived from the `claims` table, no JSON blob

**Choice:** assemble the report by querying normalized `claims` rows.

**Rejected:** a denormalized `runs.result` JSON column.

**Why:** a JSON blob plus claim rows is two writes that can diverge if the
process dies between them. One normalized source of truth removes the
consistency split and makes change-detection a diff query.

**Status:** implemented (`db/schema.sql`, `db/repository.py`), tested
(`tests/test_changes.py`).

### 14. Clerk auth: Google OAuth + passwordless email code

**Choice:** Clerk for auth, with two sign-in methods — Google OAuth and a
passwordless email one-time code. No password.

**Rejected:** (a) email + **password**; (b) hand-rolled OAuth with authlib.

**Why:** reviewers and users won't all have a Google account, so Google-only
would lock them out — a passwordless email code lets anyone in with any
address. Password auth was still rejected on purpose: storing a password brings
reset flows, credential-stuffing defense, and breach liability, for no benefit
over a one-time code that Clerk generates and verifies. Rolling OAuth by hand
means owning PKCE, state, JWKS, and cookie security — each with a non-obvious
failure mode; Clerk delegates all of it. The backend is indifferent to the
method: it verifies the Clerk JWT and reads the user id either way.

**Status:** implemented (`core/auth.py`, `frontend/src/middleware.ts`),
tested (`tests/test_router_discover.py::test_discover_requires_auth_when_not_disabled`).

### 15. RLS as defense-in-depth; application layer as the operative control

**Choice:** ownership is enforced in router code by Clerk user id; RLS
policies exist but the service-role backend bypasses them.

**Rejected:** claiming RLS as the active data-isolation control.

**Why:** RLS with `auth.jwt()` only fires for Supabase-authenticated
roles, and the backend uses the service role by design. Saying "RLS
protects the data" would be false here. The precise, honest statement —
app-layer is operative, RLS is a latent second layer that activates if the
frontend ever queries Supabase directly via a Clerk→Supabase JWT — is in
the [`schema.sql`](backend/db/schema.sql) header.

**Status:** implemented. Ownership filtering is exercised by the router tests
(an unowned run is a 404, never a 403 that confirms it exists).

### 16. Evaluation harness for the guardrails

**Choice:** a labelled dataset of fictional sources with planted defects
(`backend/evals/`), scored in three tiers — a deterministic tier in CI, a
trajectory tier that checks *which rung* of the ladder resolves each defect,
and a model tier behind `pytest -m eval`.

**Rejected:** (a) relying on unit tests alone; (b) a single live-model eval
with no offline component.

**Why:** unit tests verify control flow, which is not the same question as
"does the judge catch hallucinations?" — that depends on a model, a prompt,
and a threshold. Without a number, every prompt edit and model swap is an
unmeasured change, and this app has already been through two forced model
migrations. (b) was rejected because an eval that needs keys and costs money
runs rarely, and one that runs rarely doesn't catch regressions; splitting
out the rungs that *can* be measured offline puts a real number in CI.

Two details worth defending. The dataset uses **invented facts about
fictional companies**, because a document about a real company lets a model
score well from pretraining rather than from the passages — measuring the
wrong thing. And the deterministic tier pairs the free rungs with a
deliberately **credulous** judge that approves everything, so the score
isolates what the cheap rungs catch alone: currently 60% of planted
hallucinations before any LLM call, at a 100% pass rate on faithful claims
(the second number exists to catch the degenerate way to maximise the
first — reject everything).

The **trajectory tier** (`evals/trajectory.py`) adds the routing dimension:
for each defect it asserts the *cheapest correct rung* resolves it — a
fabricated URL at the free structural check, an off-topic claim at the
similarity floor, a contradiction only at the LLM judge. Catching a structural
defect at the model would be a correctness pass but a cost regression, and
only a trajectory check sees that. It reuses the real ladder (no
reimplementation) and runs offline, so the routing guarantee is in CI.

**Status:** implemented (`backend/evals/`, `tests/test_evals.py`).

---

## Agentic patterns considered and deliberately not used

This is an AI application, and the surrounding ecosystem supplies a
well-known kit of agent patterns — middleware stacks, semantic caching,
episodic memory, ReAct tool loops, GraphRAG, checkpointed human-in-the-loop.
Several are genuinely good. Adopting them all would have been the easier
choice to defend in a summary and the harder one to defend in review, so
each was taken on its merits. What follows is what was rejected and why,
because in an assignment about judgement the rejections carry as much
information as the adoptions.

**Adopted, adapted to fit:** the composable middleware pattern (#5a) — but
as decorators of the existing provider interfaces rather than by adopting an
agent framework's middleware base class, because the seam already existed
and the app has no agent loop to wrap. The cheap-deterministic-before-
expensive-LLM guardrail ordering (ARCHITECTURE, "guardrail ladder"). The
idea of prompt-version stamping, reduced to a constant recorded in run
metrics rather than a hosted prompt-management service.

**Semantic (similarity-keyed) cache — rejected.** Covered in #5b: a
near-miss hit in front of the grounding check verifies one claim against
another's passages. The deterministic cache captures this app's actual
repeat pattern anyway.

**Episodic / cross-session memory — rejected.** It personalises a
conversational assistant by retrieving a user's relevant past exchanges.
This product has no conversational surface: a run is a bounded job over a
source set the user chose. There is no accumulating dialogue to compress and
no personalisation the user hasn't already expressed directly in the run
inputs. It would add a vector store and a retrieval path to serve no user-
visible behaviour.

**GraphRAG / a second retrieval modality — rejected.** A knowledge graph
answers *how things relate* across a large, persistent corpus. Here the
corpus is at most ten pages, fetched fresh per run and discarded. There are
no cross-entity relationships to traverse that per-source retrieval doesn't
already cover, and building a graph over ten documents to query it once is
infrastructure with no reader.

**Autonomous agent loop for source discovery — rejected** (#11 covers the
core argument: search-and-rank is rules, not reasoning). Worth adding that
the agent version is not merely unnecessary but *more* work to make safe: a
tool loop needs a call cap, an action guardrail, and clarification handling
before it can be trusted, all to make a deterministic function
non-deterministic and harder to test.

**Hosted prompt management with versioned rollback — partially adopted.**
The valuable kernel is that a report should be reproducible: you should be
able to tell which prompt produced it. That's a version constant stamped
into run metrics. The rest — a vendor console, an indirection pointer, a
rollback workflow — solves a problem this app doesn't have, since no
non-engineer edits these prompts and a rollback is a redeploy.

**Conversation summarisation middleware — rejected.** It compresses growing
message history. Context here is bounded per run by the retriever, and never
grows across turns because there are no turns.

**A dedicated PII middleware layer — rejected.** `core/sanitize.py` already
redacts light PII from source content, which is where the risk is
(competitor pages carry executive contact details). Competitor blog posts
are not health or payment records; a separate layer would be ceremony.

**Checkpointed human-in-the-loop via a graph framework — rejected, with the
lesson kept.** Both HITL gates here (discovery approval, unreachable-source
paste) are already implemented, one of them entirely as DB state. The
transferable insight — *in-memory pause state only works if the same process
handles both the pause and the resume* — is exactly the constraint this
build documents rather than hides, and the fix doesn't require the
framework. See [`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md) #1.
