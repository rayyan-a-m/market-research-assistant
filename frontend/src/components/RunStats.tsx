import type { RunMetrics } from "@/lib/types";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-lg font-semibold tracking-tight">{value}</span>
      <span className="text-xs text-muted">{label}</span>
    </div>
  );
}

// Compact "Run stats" strip — the simple-monitoring surface. Everything here is
// captured during the pipeline run (duration, token usage, search hits, cost).
export function RunStats({ metrics }: { metrics: RunMetrics }) {
  const seconds = (metrics.duration_ms / 1000).toFixed(1);
  const tokens = metrics.input_tokens + metrics.output_tokens;
  // Older runs predate some of these metrics, so treat missing values as zero
  // rather than rendering "undefined".
  const dropped = metrics.claims_dropped_unattributed ?? 0;
  const cost = metrics.estimated_cost_usd ?? 0;
  const costLabel = cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
  return (
    <section className="rounded-2xl border border-border bg-surface p-5 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Run stats</h2>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Duration" value={`${seconds}s`} />
        <Stat label="Sources" value={String(metrics.sources_fetched)} />
        <Stat
          label="Claims verified"
          value={`${metrics.claims_verified}/${metrics.claims_total}`}
        />
        <Stat label="Themes" value={String(metrics.themes)} />
        <Stat label="Tokens (in/out)" value={`${metrics.input_tokens}/${metrics.output_tokens}`} />
        <Stat label="Total tokens" value={tokens.toLocaleString()} />
        <Stat label="LLM calls" value={String(metrics.llm_calls ?? 0)} />
        <Stat label="Search API hits" value={String(metrics.search_calls ?? 0)} />
        <Stat label="Est. cost" value={costLabel} />
        <Stat label="Model" value={metrics.summarizer_model} />
      </div>
      <p className="mt-3 text-xs text-muted">
        Est. cost is at standard paid rates — this app runs on free tiers, so the actual charge is
        $0. Embeddings excluded.
      </p>
      {dropped > 0 && (
        // Surfaced rather than swallowed: the summarizer cited a source that
        // was never fetched, and the attribution check dropped the claim
        // before it could render as a link to a page that never backed it.
        <p className="mt-4 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          {dropped} claim{dropped === 1 ? "" : "s"} discarded for citing a source that
          wasn&apos;t part of this run.
        </p>
      )}
    </section>
  );
}
