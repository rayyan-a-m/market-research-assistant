import type { Claim, ResearchReport, Verdict } from "@/lib/types";

const VERDICT_STYLE: Record<Verdict, { label: string; className: string }> = {
  supported: { label: "Supported", className: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300" },
  partial: { label: "Partial", className: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300" },
  unsupported: { label: "Unsupported", className: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300" },
  low_confidence: { label: "Unverified", className: "bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300" },
};

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function VerdictBadge({ verdict, confidence }: { verdict: Verdict; confidence: number }) {
  const style = VERDICT_STYLE[verdict];
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}>
      {style.label} · {(confidence * 100).toFixed(0)}%
    </span>
  );
}

function ClaimRow({ claim }: { claim: Claim }) {
  return (
    <li className="border-t border-border py-3 first:border-t-0 first:pt-0">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm leading-relaxed">{claim.text}</p>
        <VerdictBadge verdict={claim.verdict} confidence={claim.confidence} />
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
        <a
          href={claim.source_url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-accent hover:underline"
        >
          {hostname(claim.source_url)} ↗
        </a>
        {claim.judge_reason && <span className="text-muted">· {claim.judge_reason}</span>}
      </div>
    </li>
  );
}

export function ReportView({ report }: { report: ResearchReport }) {
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      {report.themes.map((theme) => (
        <section
          key={theme.title}
          className="flex flex-col rounded-2xl border border-border bg-surface p-6 shadow-sm"
        >
          <h2 className="text-xl font-semibold tracking-tight">{theme.title}</h2>
          {theme.summary && (
            <p className="mt-1.5 text-sm leading-relaxed text-muted">{theme.summary}</p>
          )}
          <ul className="mt-4 flex-1 rounded-xl border border-border bg-background p-4">
            {theme.claims.map((claim, i) => (
              <ClaimRow key={i} claim={claim} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
