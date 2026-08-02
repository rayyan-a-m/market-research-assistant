"""Composable provider middleware — cross-cutting concerns as decorators of
the provider interfaces, not as code smeared through the pipeline.

Every class here implements a provider interface *and* wraps one, so they
compose in any combination and none of them knows what it is wrapping. That
is the Decorator pattern applied to the same seam the Strategy/Adapter/Factory
layer already established (`providers/base.py`), and it is why adding token
accounting, a spend cap, a circuit breaker, or a cache required no change to
`summarizer.py`, `judge.py`, or `retriever.py`.

The concrete payoff, visible in the diff that introduced this module: token
accounting used to be a `usage: dict[str, int] | None` parameter threaded
through `run_pipeline` -> `summarize()` -> `judge_claims()` -> `_judge_one()`,
mutated by hand at two call sites. It is now one layer, and those four
signatures lost a parameter. The pattern removed code rather than adding a
framework.

## Layer order (composed in `factory.py` — the order is load-bearing)

    Metered(                         <- outermost: sees whatever finally worked
      BudgetGuard(                   <- refuses before spending, not after
        Fallback(                    <- primary -> fallback across models
          primary  = CircuitBreaker(adapter A),
          fallback = CircuitBreaker(adapter B),
        )))

Two of those placements are deliberate and worth stating, because the obvious
alternative is wrong in each case:

- **CircuitBreaker sits *inside* Fallback, one per concrete adapter — not
  outside it.** A breaker's job is to stop hammering *a model that is
  failing*. Wrapped outside the fallback, an open breaker would refuse the
  primary *and* the fallback, which is precisely backwards: the moment the
  primary is known-bad is the moment the fallback is most needed. Inside, an
  open primary breaker fails fast, `FallbackChatProvider` catches that and
  routes to the fallback model immediately — turning the breaker into a
  latency *win* (skip three doomed retries and a timeout) instead of an
  outage.

- **BudgetGuard sits *outside* Fallback.** Inside, a run that had exhausted
  its budget would still burn a fallback call before giving up — the cap
  would be advisory. Outside, "no budget left" short-circuits the whole
  chain, which is what a cap means.

## Shared state between layers

`RunLedger` is the one piece of state layers share: `Metered` writes to it,
`BudgetGuard` reads it. Passing a small explicit object beats threading
accumulator parameters through business logic (what this replaced) and beats
module-level globals (which would silently merge concurrent runs' budgets).
One ledger per run, constructed by the orchestrator.

## Threading note

These layers mutate plain attributes with no `await` between the read and the
write, so under a single-threaded asyncio event loop the updates are atomic —
no lock is needed even though the judge stage runs claims concurrently
(`asyncio.gather` + a semaphore). This is a property of the *runtime*, not of
the code being obviously safe: the same classes driven from a thread pool
would need locking around the counters and the breaker transition. Stated
here because it is exactly the kind of assumption that is invisible until it
breaks.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.errors import (
    BudgetExceededError,
    ProviderError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from app.providers.base import (
    ChatProvider,
    ChatResult,
    EmbeddingProvider,
    Message,
    RetryPolicy,
    T,
)

logger = logging.getLogger(__name__)


# --- Shared per-run state ----------------------------------------------------


@dataclass
class RunLedger:
    """Token and call accounting for a single run, shared by the layers that
    write it (`MeteredChatProvider`) and the layer that reads it
    (`BudgetGuardChatProvider`). One instance per run — never a global, so two
    concurrent runs can't spend each other's budget."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    #: Populated per call for the audit trail: (model, input, output).
    per_call: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(self, result: ChatResult[Any]) -> None:
        self.calls += 1
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.per_call.append((result.model, result.input_tokens, result.output_tokens))


# --- Chat layers -------------------------------------------------------------


class MeteredChatProvider(ChatProvider):
    """Records token usage and emits one structured audit line per call.

    Outermost layer, so it observes the call that actually succeeded —
    including one served by the fallback model, which is why the audit line
    logs `result.model` (what answered) rather than the configured model name
    (what we asked for). Those differ exactly when a fallback fired, and that
    divergence is the interesting signal.
    """

    def __init__(self, inner: ChatProvider, ledger: RunLedger, *, role: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self._role = role

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[T] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[T]:
        started = time.monotonic()
        result = await self._inner.complete(
            system=system, messages=messages, response_model=response_model,
            max_tokens=max_tokens,
        )
        self._ledger.record(result)
        logger.info(
            "llm_call",
            extra={
                "role": self._role,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "run_total_tokens": self._ledger.total_tokens,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return result


class BudgetGuardChatProvider(ChatProvider):
    """Refuses a call once the run has spent its token budget.

    The check is *pre-call* and therefore approximate — the request that
    crosses the line is allowed to finish, so actual spend can overshoot the
    cap by roughly one call. Enforcing exactly would mean counting input
    tokens before sending, which needs a tokenizer per model family and still
    can't predict output length. A cap that bounds a runaway to O(cap + one
    call) is the useful property; exactness here would be precision theatre.

    Raising `BudgetExceededError` (a `ProviderError`) is deliberate: callers
    already handle "this provider call cannot be made". The judge stage
    catches `ProviderError` per claim and marks it unverified, so exhausting
    the budget mid-verification degrades a run to partially-verified instead
    of failing it — the graceful path, for free, with no new handling code.
    Because this layer sits *outside* the fallback, the fallback never sees
    the exception and so never treats a budget stop as a provider outage.
    """

    def __init__(self, inner: ChatProvider, ledger: RunLedger, *, max_tokens_per_run: int) -> None:
        self._inner = inner
        self._ledger = ledger
        self._cap = max_tokens_per_run

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[T] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[T]:
        if self._cap > 0 and self._ledger.total_tokens >= self._cap:
            raise BudgetExceededError(
                f"run token budget exhausted ({self._ledger.total_tokens}/{self._cap})",
                provider="budget",
            )
        return await self._inner.complete(
            system=system, messages=messages, response_model=response_model,
            max_tokens=max_tokens,
        )


class BreakerState(StrEnum):
    CLOSED = "closed"      # calls pass through
    OPEN = "open"          # calls fail fast
    HALF_OPEN = "half_open"  # one probe allowed


class CircuitBreakerChatProvider(ChatProvider):
    """Stops calling a model that is failing, and lets the fallback take over
    immediately instead of after three retries and a timeout.

    State machine: CLOSED -(consecutive failures >= threshold)-> OPEN
    -(cooldown elapsed)-> HALF_OPEN -(probe succeeds)-> CLOSED, or
    -(probe fails)-> OPEN.

    **Not every failure trips it.** `ProviderResponseError` means the model
    answered and the answer failed schema validation — that is a bad
    generation, not a sick dependency, and retrying it against the same model
    is reasonable. Only timeouts, rate limits, and unavailability count
    toward the threshold. Tripping on malformed output would take a model
    offline for being briefly stupid, which is the wrong response to the wrong
    signal.

    One breaker instance per underlying adapter (see the module docstring on
    ordering), and per process — consistent with the single-replica
    deployment. Multi-replica would want this state in Redis, alongside the
    unreachable-source waiter map (FUTURE_ENHANCEMENTS #1).
    """

    #: Failures of these types indicate a sick dependency, not a bad answer.
    _TRIPPING_ERRORS = (ProviderUnavailableError,)

    def __init__(
        self,
        inner: ChatProvider,
        *,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self._inner = inner
        self._name = name
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """Current state, resolving an elapsed cooldown to HALF_OPEN."""
        if self._state is BreakerState.OPEN and (
            time.monotonic() - self._opened_at >= self._cooldown
        ):
            return BreakerState.HALF_OPEN
        return self._state

    def _on_success(self) -> None:
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False

    def _on_failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._failures >= self._threshold:
            if self._state is not BreakerState.OPEN:
                logger.warning(
                    "circuit breaker OPEN for %s after %d consecutive failures",
                    self._name, self._failures,
                )
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        response_model: type[T] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult[T]:
        state = self.state
        if state is BreakerState.OPEN:
            # Fail fast in the normalized taxonomy so the fallback layer above
            # treats it like any other unavailability and reroutes.
            raise ProviderUnavailableError(
                f"circuit breaker open for {self._name}", provider=self._name
            )
        if state is BreakerState.HALF_OPEN:
            if self._probe_in_flight:
                raise ProviderUnavailableError(
                    f"circuit breaker probing {self._name}", provider=self._name
                )
            # Check-and-set with no await between them: atomic under asyncio,
            # so exactly one coroutine becomes the probe.
            self._probe_in_flight = True
            self._state = BreakerState.HALF_OPEN

        try:
            result = await self._inner.complete(
                system=system, messages=messages, response_model=response_model,
                max_tokens=max_tokens,
            )
        except self._TRIPPING_ERRORS:
            self._on_failure()
            raise
        except ProviderResponseError:
            # A bad generation is not a sick dependency — don't count it.
            self._probe_in_flight = False
            raise
        except ProviderError:
            # Timeouts and rate limits do indicate the dependency is unhappy.
            self._on_failure()
            raise
        self._on_success()
        return result


# --- Embedding layers --------------------------------------------------------


class EmbeddingCache:
    """Bounded, content-addressed cache of embedding vectors.

    **Keyed by an exact hash of (model, text) — deliberately not a similarity
    match.** A semantic cache, which returns a stored vector when the query is
    *close enough*, is the wrong tool here: these embeddings feed the judge's
    grounding check, so a near-miss hit would verify claim A against the
    passages retrieved for claim B and silently report the result as
    verified. Caching is safe exactly where the input is identical, and this
    application's caching win comes from identical input anyway — the
    "re-run with the same sources" flow re-embeds byte-identical documents.
    Take the cheap deterministic hit; refuse the clever probabilistic one.

    LRU-bounded because it outlives a single run: an unbounded dict keyed by
    document text is a memory leak with a slow fuse in a long-lived process.
    """

    def __init__(self, *, max_entries: int = 4096) -> None:
        self._entries: OrderedDict[str, list[float]] = OrderedDict()
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(namespace: str, text: str) -> str:
        digest = hashlib.sha256(f"{namespace}\x00{text}".encode()).hexdigest()
        return digest

    def get(self, key: str) -> list[float] | None:
        vector = self._entries.get(key)
        if vector is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return vector

    def put(self, key: str, vector: list[float]) -> None:
        self._entries[key] = vector
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)  # evict least-recently-used

    def __len__(self) -> int:
        return len(self._entries)


class CachingEmbeddingProvider(EmbeddingProvider):
    """Serves known vectors from the cache and asks the inner provider only
    for the misses, preserving input order in the returned list.

    The partial-batch behaviour is the point: a re-run that shares nine of ten
    documents with a previous run sends one document to the API, not ten.
    """

    def __init__(self, inner: EmbeddingProvider, cache: EmbeddingCache, *, namespace: str) -> None:
        self._inner = inner
        self._cache = cache
        self._namespace = namespace

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        keys = [EmbeddingCache.key(self._namespace, t) for t in texts]
        resolved: dict[int, list[float]] = {}
        missing: list[int] = []
        for i, key in enumerate(keys):
            hit = self._cache.get(key)
            if hit is None:
                missing.append(i)
            else:
                resolved[i] = hit

        if missing:
            fetched = await self._inner.embed([texts[i] for i in missing])
            # strict=True: a provider returning a different number of vectors
            # than we asked for would otherwise misalign every vector after
            # the gap — a silent, near-undebuggable attribution bug. Fail loud.
            for i, vector in zip(missing, fetched, strict=True):
                resolved[i] = vector
                self._cache.put(keys[i], vector)

        # Indexing by position rather than filtering: if a slot were somehow
        # unfilled this raises, instead of quietly returning a short list that
        # the caller would zip against its inputs.
        return [resolved[i] for i in range(len(texts))]


class RetryingEmbeddingProvider(EmbeddingProvider):
    """Bounded retry with exponential backoff and jitter for embeddings.

    Why this exists as a decorator when the chat adapters retry *inside*
    themselves: the chat path uses LangChain's `.with_retry()`, which is
    aware of the SDK's own retryable states and sits correctly inside the
    structured-output binding — replacing it with a coarser outer retry would
    be a downgrade dressed as consistency. `GoogleGenerativeAIEmbeddings`
    exposes no equivalent, so embeddings had no retry at all, which mattered:
    a run embeds hundreds of chunks, so it is the stage most likely to trip a
    free-tier rate limit. Same layer, two mechanisms, each chosen for what the
    SDK underneath actually offers — and the asymmetry documented rather than
    papered over.
    """

    def __init__(self, inner: EmbeddingProvider, policy: RetryPolicy) -> None:
        self._inner = inner
        self._policy = policy

    async def embed(self, texts: list[str]) -> list[list[float]]:
        last: ProviderError | None = None
        for attempt in range(self._policy.max_attempts):
            try:
                return await self._inner.embed(texts)
            except ProviderResponseError:
                raise  # malformed response won't fix itself on a retry
            except ProviderError as exc:
                last = exc
                if attempt == self._policy.max_attempts - 1:
                    break
                # Full jitter: spreads a thundering herd of concurrent chunk
                # batches instead of retrying them all on the same beat.
                delay = self._policy.backoff_base_seconds * (2**attempt)
                await asyncio.sleep(random.uniform(0, delay))
                logger.warning(
                    "embedding attempt %d/%d failed (%s); retrying",
                    attempt + 1, self._policy.max_attempts, exc,
                )
        assert last is not None
        raise last
