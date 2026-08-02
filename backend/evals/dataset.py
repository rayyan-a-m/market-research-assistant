"""The labelled evaluation dataset.

Fictional companies and invented facts on purpose: a source document about a
real company would let a model score well from pretraining knowledge instead
of from the passages, which is the exact failure the judge exists to catch.
Every fact below appears nowhere but here, so the only way to verify a claim
is to actually read the source.

Each claim carries the *defect* it plants, not just an expected verdict —
that's what lets the scorer report which kind of hallucination survives,
which is more useful than a single aggregate number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Defect = Literal[
    "none",              # faithful to the source; should verify
    "overstated",        # true core, inflated scope/certainty
    "unsupported",       # not in the source at all
    "contradicted",      # the source says the opposite
    "cross_source",      # true, but attributed to the wrong source
    "fabricated_url",    # cites a URL that was never fetched
    "off_topic",         # semantically unrelated to anything in the source
]


@dataclass(frozen=True)
class EvalSource:
    url: str
    markdown: str


@dataclass(frozen=True)
class EvalClaim:
    text: str
    source_url: str
    defect: Defect

    @property
    def should_verify(self) -> bool:
        """A claim should end up `verified` only if it is faithful."""
        return self.defect == "none"


ORBITAL = EvalSource(
    url="https://eval.invalid/orbital-payments/blog",
    markdown="""# Orbital Payments launches Relay routing

Orbital Payments announced Relay on 14 March 2026 at its annual Vector
conference in Lisbon. Relay is a payment-routing layer that lets merchants
define fallback chains across up to four acquirers.

In its first quarter of availability Relay processed 2.3 million transactions
for 47 pilot merchants, all of them based in Portugal and Spain. Orbital says
authorization rates improved by 1.8 percentage points on average across the
pilot cohort.

Relay currently supports card payments only. Orbital has said that support for
SEPA direct debit is on the roadmap but has not committed to a date.

Pricing for Relay was not disclosed. Orbital confirmed that Relay is available
only to merchants already using Orbital's core acquiring product, and that
there are no plans to sell it standalone.
""",
)

NIMBUS = EvalSource(
    url="https://eval.invalid/nimbus-ledger/newsroom",
    markdown="""# Nimbus Ledger acquires Tessellate

Nimbus Ledger has acquired Tessellate, a reconciliation-tooling startup, for
an undisclosed sum. The deal closed on 2 June 2026. Tessellate's team of 19
engineers will join Nimbus Ledger's Dublin office.

Tessellate built automated ledger-matching software used by mid-market
accounting teams. Nimbus Ledger intends to fold Tessellate's matching engine
into its existing treasury product rather than continue selling it separately;
existing Tessellate customers will be migrated by the end of 2026.

Nimbus Ledger's CEO said the acquisition was about engineering capacity as
much as product, noting that reconciliation is "the least glamorous and most
requested" part of their roadmap.
""",
)

SOURCES: list[EvalSource] = [ORBITAL, NIMBUS]


CLAIMS: list[EvalClaim] = [
    # --- Faithful: these SHOULD verify. Missing them is over-strictness. ---
    EvalClaim(
        text="Orbital Payments announced Relay on 14 March 2026 at its Vector conference in Lisbon.",
        source_url=ORBITAL.url,
        defect="none",
    ),
    EvalClaim(
        text="Relay processed 2.3 million transactions for 47 pilot merchants in its first quarter.",
        source_url=ORBITAL.url,
        defect="none",
    ),
    EvalClaim(
        text="Relay lets merchants define fallback chains across up to four acquirers.",
        source_url=ORBITAL.url,
        defect="none",
    ),
    EvalClaim(
        text="Nimbus Ledger acquired Tessellate, with the deal closing on 2 June 2026.",
        source_url=NIMBUS.url,
        defect="none",
    ),
    EvalClaim(
        text="Tessellate's 19 engineers will join Nimbus Ledger's Dublin office.",
        source_url=NIMBUS.url,
        defect="none",
    ),

    # --- Overstated: true core, inflated. The subtlest and most common
    #     real-world failure — a summarizer rounding "pilot in two countries"
    #     up to "across Europe". ---
    EvalClaim(
        text="Relay is used by merchants across Europe, improving authorization rates by nearly 2%.",
        source_url=ORBITAL.url,
        defect="overstated",
    ),
    EvalClaim(
        text="Orbital Payments will add SEPA direct debit support to Relay later this year.",
        source_url=ORBITAL.url,
        defect="overstated",
    ),
    EvalClaim(
        text="Nimbus Ledger acquired Tessellate to expand its customer base in the mid-market.",
        source_url=NIMBUS.url,
        defect="overstated",
    ),

    # --- Unsupported: plausible, absent from the source. ---
    EvalClaim(
        text="Orbital Payments priced Relay at 0.3% per routed transaction.",
        source_url=ORBITAL.url,
        defect="unsupported",
    ),
    EvalClaim(
        text="Nimbus Ledger paid $45 million to acquire Tessellate.",
        source_url=NIMBUS.url,
        defect="unsupported",
    ),

    # --- Contradicted: the source states the opposite. ---
    EvalClaim(
        text="Relay is sold standalone to merchants who do not use Orbital's acquiring product.",
        source_url=ORBITAL.url,
        defect="contradicted",
    ),
    EvalClaim(
        text="Nimbus Ledger will continue to sell Tessellate's matching engine as a separate product.",
        source_url=NIMBUS.url,
        defect="contradicted",
    ),

    # --- Cross-source: true of the OTHER document. This is what per-source
    #     retrieval (DESIGN_DECISIONS #9) exists to prevent; the judge is the
    #     backstop if it ever leaks through. ---
    EvalClaim(
        text="Orbital Payments acquired a reconciliation-tooling startup called Tessellate.",
        source_url=ORBITAL.url,
        defect="cross_source",
    ),

    # --- Fabricated URL: must be dropped by the structural check before the
    #     judge is ever asked, and before it can render as a link. ---
    EvalClaim(
        text="Orbital Payments reported record quarterly revenue.",
        source_url="https://eval.invalid/never-fetched/press-release",
        defect="fabricated_url",
    ),

    # --- Off-topic: nothing in the source is remotely close, so the
    #     similarity floor should catch it without spending an LLM call. ---
    EvalClaim(
        text="The company opened a chain of vegetarian restaurants in Osaka.",
        source_url=ORBITAL.url,
        defect="off_topic",
    ),
]


def claims_by_defect(defect: Defect) -> list[EvalClaim]:
    return [c for c in CLAIMS if c.defect == defect]


def fetched_urls() -> list[str]:
    return [s.url for s in SOURCES]
