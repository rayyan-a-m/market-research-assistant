import type { RunSource } from "@/lib/types";

const ORIGIN_LABEL: Record<string, string> = {
  USER_SUPPLIED: "You supplied",
  USER_APPROVED_DISCOVERED: "Approved (discovered)",
  USER_PASTED_TEXT: "Pasted text",
  USER_UPLOADED_PDF: "PDF upload",
};

export function SourcesCard({ sources }: { sources: RunSource[] }) {
  if (sources.length === 0) return null;
  return (
    <section className="rounded-2xl border border-border bg-surface p-5 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
        Sources ({sources.length})
      </h2>
      <ul className="flex flex-col divide-y divide-border">
        {sources.map((s) => (
          <li
            key={s.url}
            className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0"
          >
            <a
              href={s.url}
              target="_blank"
              rel="noreferrer"
              className="truncate text-sm text-accent hover:underline"
              title={s.url}
            >
              {s.url}
            </a>
            <span className="shrink-0 text-xs text-muted">
              {ORIGIN_LABEL[s.source_origin] ?? s.source_origin}
              {s.status.startsWith("OK") ? "" : ` · ${s.status.toLowerCase()}`}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
