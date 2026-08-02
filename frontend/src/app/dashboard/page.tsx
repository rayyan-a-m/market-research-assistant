"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useEffect, useState } from "react";

import { Header } from "@/components/Header";
import { api } from "@/lib/api";
import type { RunSummary } from "@/lib/types";

const STATUS_STYLE: Record<string, string> = {
  COMPLETE: "text-green-700 dark:text-green-400",
  FAILED: "text-red-600 dark:text-red-400",
  CANCELLED: "text-muted",
  PROCESSING: "text-accent",
};

export default function DashboardPage() {
  const { getToken } = useAuth();
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const token = await getToken();
        setRuns(await api.listRuns(token));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load runs");
      }
    })();
  }, [getToken]);

  async function handleDelete(id: string) {
    if (!confirm("Delete this run? This can’t be undone.")) return;
    setRuns((rs) => rs?.filter((r) => r.id !== id) ?? null); // optimistic
    try {
      await api.deleteRun(id, await getToken());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete run");
    }
  }

  return (
    <>
      <Header />
      <main className="mx-auto flex w-11/12 flex-col gap-6 py-10 lg:w-3/4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Your research runs</h1>
          <Link
            href="/research"
            className="rounded-full bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-hover"
          >
            New run
          </Link>
        </div>

        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
        {runs === null && !error && <p className="text-sm text-muted">Loading…</p>}
        {runs?.length === 0 && (
          <p className="text-sm text-muted">No runs yet — start your first one.</p>
        )}

        <ul className="flex flex-col gap-3">
          {runs?.map((run) => (
            <li
              key={run.id}
              className="flex items-center gap-3 rounded-2xl border border-border bg-surface p-4 shadow-sm transition hover:border-accent/50"
            >
              <Link
                href={`/dashboard/${run.id}`}
                className="flex min-w-0 flex-1 items-center justify-between gap-4 hover:opacity-70"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {run.topics.join(", ") || "Untitled run"}
                  </p>
                  <p className="truncate text-xs text-muted">
                    {run.competitors.length > 0 ? run.competitors.join(", ") + " · " : ""}
                    {new Date(run.created_at).toLocaleString()}
                  </p>
                </div>
                <span className={`shrink-0 text-xs font-medium ${STATUS_STYLE[run.status] ?? "text-muted"}`}>
                  {run.status}
                </span>
              </Link>
              <button
                onClick={() => handleDelete(run.id)}
                aria-label="Delete run"
                title="Delete run"
                className="shrink-0 rounded-lg px-2 py-1 text-muted transition hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/30"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      </main>
    </>
  );
}
