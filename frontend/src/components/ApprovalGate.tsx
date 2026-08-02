"use client";

import { useState } from "react";
import type { DiscoveryCandidate } from "@/lib/types";

interface Props {
  candidates: DiscoveryCandidate[];
  discoverySkipped: boolean;
  onSubmit: (
    decisions: { candidate_id: string; approved: boolean }[],
    mechanism: "user_action" | "select_all" | "reject_all" | "skip",
  ) => void;
  busy?: boolean;
}

// The trust mechanism: the system proposes sources but never adds one
// silently. The user approves, rejects, or skips before anything is fetched.
export function ApprovalGate({ candidates, discoverySkipped, onSubmit, busy }: Props) {
  const [approved, setApproved] = useState<Record<string, boolean>>({});

  if (discoverySkipped || candidates.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <p className="text-sm text-muted">
          No additional sources found — proceeding with your original URLs.
        </p>
        <button
          onClick={() => onSubmit([], "skip")}
          disabled={busy}
          className="w-fit rounded-full bg-accent px-5 py-2 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {busy ? "Starting…" : "Start research"}
        </button>
      </div>
    );
  }

  const toggle = (id: string) => setApproved((p) => ({ ...p, [id]: !p[id] }));
  const decisions = candidates.map((c) => ({ candidate_id: c.id, approved: !!approved[c.id] }));

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">Suggested additional sources</h2>
        <p className="text-sm text-muted">
          The system found these. Approve the ones you trust — nothing is added without your say-so.
        </p>
      </div>

      <div className="flex gap-3 text-sm">
        <button
          onClick={() => setApproved(Object.fromEntries(candidates.map((c) => [c.id, true])))}
          className="text-accent hover:opacity-70"
        >
          Select all
        </button>
        <button onClick={() => setApproved({})} className="text-muted hover:text-foreground">
          Clear
        </button>
      </div>

      <ul className="flex flex-col gap-2">
        {candidates.map((c) => (
          <li
            key={c.id}
            className="flex items-start gap-3 rounded-xl border border-border p-3"
          >
            <input
              type="checkbox"
              checked={!!approved[c.id]}
              onChange={() => toggle(c.id)}
              className="mt-1 h-4 w-4 accent-[var(--accent)]"
            />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{c.page_title ?? c.url}</p>
              <p className="text-xs text-muted">{c.rationale}</p>
              <a
                href={c.url}
                target="_blank"
                rel="noreferrer"
                className="text-xs text-accent hover:underline"
              >
                {c.domain}
              </a>
            </div>
          </li>
        ))}
      </ul>

      <div className="flex gap-3">
        <button
          onClick={() => onSubmit(decisions, "user_action")}
          disabled={busy}
          className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {busy ? "Starting…" : "Approve & start research"}
        </button>
        <button
          onClick={() => onSubmit([], "skip")}
          disabled={busy}
          className="rounded-full border border-border px-5 py-2 text-sm font-medium hover:bg-surface disabled:opacity-50"
        >
          Skip — use my URLs only
        </button>
      </div>
    </div>
  );
}
