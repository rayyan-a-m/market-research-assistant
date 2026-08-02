import type { RunChanges, Verdict } from "@/lib/types";

const VERDICT_LABEL: Record<Verdict, string> = {
  supported: "Supported",
  partial: "Partial",
  unsupported: "Unsupported",
  low_confidence: "Unverified",
};

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// "What's new since last run" — the change-detection surface. Only renders when
// there's a comparable prior run and something actually changed.
export function WhatsNew({ changes }: { changes: RunChanges }) {
  if (!changes.has_previous) return null;
  if (changes.new_claims.length === 0 && changes.changed.length === 0) {
    return (
      <section className="rounded-xl border border-border p-4 text-sm text-muted">
        No changes since your previous run with these inputs.
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-accent/40 bg-accent/5 p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-accent">
        What’s new since your last run
      </h2>

      {changes.new_claims.length > 0 && (
        <div className="mb-3">
          <h3 className="mb-1 text-xs font-semibold text-muted">
            New claims ({changes.new_claims.length})
          </h3>
          <ul className="flex flex-col gap-1.5 text-sm">
            {changes.new_claims.map((c, i) => (
              <li key={i}>
                {c.text}{" "}
                <a
                  href={c.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent hover:underline"
                >
                  {hostname(c.source_url)} ↗
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      {changes.changed.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold text-muted">
            Verdict changed ({changes.changed.length})
          </h3>
          <ul className="flex flex-col gap-1.5 text-sm">
            {changes.changed.map((ch, i) => (
              <li key={i}>
                {ch.claim.text}{" "}
                <span className="text-muted">
                  — {VERDICT_LABEL[ch.previous_verdict]} → {VERDICT_LABEL[ch.claim.verdict]}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
