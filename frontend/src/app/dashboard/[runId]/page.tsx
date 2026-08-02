"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApprovalGate } from "@/components/ApprovalGate";
import { Header } from "@/components/Header";
import { ReportView } from "@/components/ReportView";
import { RunStats } from "@/components/RunStats";
import { SourcesCard } from "@/components/SourcesCard";
import { WhatsNew } from "@/components/WhatsNew";
import { api } from "@/lib/api";
import type { DiscoveryCandidate, RunChanges, RunDetail } from "@/lib/types";

const TERMINAL = new Set(["COMPLETE", "FAILED", "CANCELLED"]);

export default function RunDetailPage() {
  const { getToken } = useAuth();
  const router = useRouter();
  const params = useParams<{ runId: string }>();
  const runId = params.runId;

  const [run, setRun] = useState<RunDetail | null>(null);
  const [changes, setChanges] = useState<RunChanges | null>(null);
  const [candidates, setCandidates] = useState<DiscoveryCandidate[] | null>(null);
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [rerunning, setRerunning] = useState(false);
  const [stopping, setStopping] = useState(false);

  async function stopRun() {
    if (!run) return;
    setStopping(true);
    try {
      await api.cancelRun(run.id, await getToken());
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to stop run");
    } finally {
      setStopping(false);
    }
  }

  async function deleteThisRun() {
    if (!run || !confirm("Delete this run? This can’t be undone.")) return;
    try {
      await api.deleteRun(run.id, await getToken());
      router.push("/dashboard");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete run");
    }
  }

  // Approve/skip the discovered sources, then hand off to the /research page to
  // stream the pipeline (reuses the existing streaming flow via sessionStorage).
  async function handleApprove(
    decisions: { candidate_id: string; approved: boolean }[],
    mechanism: "user_action" | "select_all" | "reject_all" | "skip",
  ) {
    if (!run) return;
    setApproving(true);
    try {
      await api.approve(run.id, decisions, mechanism, await getToken());
      sessionStorage.setItem("startRun", JSON.stringify({ run_id: run.id }));
      router.push("/research");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start");
      setApproving(false);
    }
  }

  // Re-run: reuse inputs AND previously approved discovered sources, skipping
  // discovery. Creates a new PENDING run server-side, then jumps to /research
  // which starts streaming it immediately.
  async function rerunWithSources() {
    if (!run) return;
    setRerunning(true);
    try {
      const token = await getToken();
      const res = await api.rerun(run.id, token);
      sessionStorage.setItem("startRun", JSON.stringify({ run_id: res.run_id }));
      router.push("/research");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to re-run");
      setRerunning(false);
    }
  }

  // Edit: prefill the research form with this run's inputs (re-runs discovery
  // on submit so the user can change what's searched).
  function editInputs() {
    if (!run) return;
    sessionStorage.setItem(
      "prefillRun",
      JSON.stringify({
        competitors: run.competitors,
        topics: run.topics,
        urls: run.input_urls,
        context: run.context ?? "",
        auto: false,
      }),
    );
    router.push("/research");
  }

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      setRun(await api.getRun(runId, token));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load run");
    }
  }, [getToken, runId]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while the run is still processing (e.g. opened directly mid-run).
  // AWAITING_APPROVAL is user-gated, not processing — don't poll it.
  useEffect(() => {
    if (run && !TERMINAL.has(run.status) && run.status !== "AWAITING_APPROVAL") {
      const t = setTimeout(load, 3000);
      return () => clearTimeout(t);
    }
  }, [run, load]);

  // Fetch "what's new" once the run is complete.
  useEffect(() => {
    if (run?.status === "COMPLETE" && changes === null) {
      (async () => {
        try {
          setChanges(await api.getChanges(runId, await getToken()));
        } catch {
          /* non-blocking — no changes panel on failure */
        }
      })();
    }
  }, [run?.status, changes, getToken, runId]);

  // A run opened while awaiting approval (e.g. from History, or after navigating
  // away from /research mid-discovery) needs its discovered sources here so the
  // user can approve them — the approval gate isn't only a /research concern.
  useEffect(() => {
    if (run?.status === "AWAITING_APPROVAL" && candidates === null) {
      (async () => {
        try {
          setCandidates(await api.getCandidates(runId, await getToken()));
        } catch {
          setCandidates([]); // treat as "no sources found" → gate offers to proceed
        }
      })();
    }
  }, [run?.status, candidates, getToken, runId]);

  return (
    <>
      <Header />
      <main className="mx-auto flex w-11/12 flex-col gap-6 py-10 lg:w-3/4">
        <Link href="/dashboard" className="text-sm text-accent hover:opacity-70">
          ‹ Back to runs
        </Link>

        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
        {!run && !error && <p className="text-sm text-muted">Loading…</p>}

        {run && (
          <>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h1 className="text-2xl font-semibold tracking-tight">
                  {run.topics.join(", ") || "Research run"}
                </h1>
                <p className="text-sm text-muted">
                  {run.competitors.join(", ")}
                  {run.competitors.length > 0 ? " · " : ""}
                  {run.status}
                </p>
              </div>
              {TERMINAL.has(run.status) && (
                <button
                  onClick={deleteThisRun}
                  className="shrink-0 rounded-full border border-border px-4 py-1.5 text-sm font-medium text-muted transition hover:border-red-300 hover:text-red-600"
                >
                  Delete run
                </button>
              )}
            </div>

            {run.status === "FAILED" && (
              <p className="text-sm text-red-600 dark:text-red-400">
                {run.error_detail ?? "This run failed."}
              </p>
            )}

            {/* Top row: Inputs (left) | Sources (right) */}
            <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
            <section className="rounded-2xl border border-border bg-surface p-5 shadow-sm">
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
                Inputs
              </h2>
              <dl className="flex flex-col gap-2 text-sm">
                {run.competitors.length > 0 && (
                  <InputRow label="Competitors" value={run.competitors.join(", ")} />
                )}
                <InputRow label="Topics" value={run.topics.join(", ")} />
                <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
                  <dt className="w-28 shrink-0 text-muted">Source URLs</dt>
                  <dd className="flex min-w-0 flex-col gap-0.5">
                    {run.input_urls.map((u) => (
                      <a
                        key={u}
                        href={u}
                        target="_blank"
                        rel="noreferrer"
                        className="truncate text-accent hover:underline"
                      >
                        {u}
                      </a>
                    ))}
                  </dd>
                </div>
                {run.context && <InputRow label="Context" value={run.context} />}
              </dl>
              <div className="mt-4 flex gap-3">
                <button
                  onClick={rerunWithSources}
                  disabled={rerunning}
                  className="rounded-full bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
                >
                  {rerunning ? "Starting…" : "Re-run (reuse discovered sources)"}
                </button>
                <button
                  onClick={editInputs}
                  className="rounded-full border border-border px-4 py-2 text-sm font-medium hover:bg-surface"
                >
                  Edit inputs
                </button>
              </div>
            </section>

              <SourcesCard sources={run.sources} />
            </div>

            {run.status === "AWAITING_APPROVAL" ? (
              <section className="rounded-2xl border border-border bg-surface p-5 shadow-sm">
                <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
                  Discovered sources — awaiting your approval
                </h2>
                {candidates === null ? (
                  <div className="flex items-center gap-3 text-sm text-muted">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
                    Loading discovered sources…
                  </div>
                ) : (
                  <ApprovalGate
                    candidates={candidates}
                    discoverySkipped={candidates.length === 0}
                    onSubmit={handleApprove}
                    busy={approving}
                  />
                )}
              </section>
            ) : (
              !TERMINAL.has(run.status) && (
                <div className="flex items-center gap-4">
                  <div className="flex items-center gap-3 text-sm text-muted">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
                    {run.status.toLowerCase()}…
                  </div>
                  <button
                    onClick={stopRun}
                    disabled={stopping}
                    className="rounded-full border border-red-300 px-4 py-1.5 text-sm font-medium text-red-600 transition hover:bg-red-50 disabled:opacity-50 dark:hover:bg-red-950/30"
                  >
                    {stopping ? "Stopping…" : "Stop"}
                  </button>
                </div>
              )
            )}

            {run.metrics && <RunStats metrics={run.metrics} />}

            {changes && <WhatsNew changes={changes} />}

            {run.report ? (
              <ReportView report={run.report} />
            ) : (
              run.status === "COMPLETE" && (
                <p className="text-sm text-muted">No grounded claims were produced for this run.</p>
              )
            )}
          </>
        )}
      </main>
    </>
  );
}

function InputRow({ label, value, pre }: { label: string; value: string; pre?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
      <dt className="w-28 shrink-0 text-muted">{label}</dt>
      <dd className={pre ? "whitespace-pre-line break-all" : ""}>{value}</dd>
    </div>
  );
}
