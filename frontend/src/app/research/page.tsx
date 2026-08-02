"use client";

import { useAuth } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApprovalGate } from "@/components/ApprovalGate";
import { Header } from "@/components/Header";
import { SourceFallback } from "@/components/SourceFallback";
import { TagInput } from "@/components/TagInput";
import { UrlList } from "@/components/UrlList";
import { api, streamRun } from "@/lib/api";
import type { DiscoverResponse, RunRequest } from "@/lib/types";

type Step = "input" | "discovering" | "approval" | "running" | "error";
type Unreachable = { source_id: string; url: string; reason?: string };

export default function ResearchPage() {
  const { getToken } = useAuth();
  const router = useRouter();

  const [step, setStep] = useState<Step>("input");
  const [competitors, setCompetitors] = useState<string[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [urls, setUrls] = useState<string[]>([]);
  const [context, setContext] = useState("");

  const [discover, setDiscover] = useState<DiscoverResponse | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [unreachable, setUnreachable] = useState<Unreachable[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function runDiscovery(payload: RunRequest) {
    setError(null);
    setStep("discovering");
    try {
      const token = await getToken();
      const res = await api.startDiscovery(payload, token);
      setDiscover(res);
      setStep("approval");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Discovery failed");
      setStep("error");
    }
  }

  function handleDiscover(e: React.FormEvent) {
    e.preventDefault();
    const cleanTopics = topics.map((t) => t.trim()).filter(Boolean);
    const cleanUrls = urls.map((u) => u.trim()).filter(Boolean);
    if (cleanTopics.length === 0) {
      setError("Add at least one topic.");
      return;
    }
    if (cleanUrls.length === 0) {
      setError("Add at least one source URL.");
      return;
    }
    setError(null);
    void runDiscovery({
      competitors: competitors.map((c) => c.trim()).filter(Boolean),
      topics: cleanTopics,
      urls: cleanUrls,
      context: context.trim() || null,
    });
  }

  // Entry points from a run detail page (stashed in sessionStorage before
  // navigating here):
  //   • "startRun"   → a re-run that reused sources; skip straight to streaming
  //   • "prefillRun" → prefill the form; `auto` also starts discovery
  useEffect(() => {
    const startRaw = sessionStorage.getItem("startRun");
    if (startRaw) {
      sessionStorage.removeItem("startRun");
      try {
        const { run_id } = JSON.parse(startRaw) as { run_id: string };
        setStep("running");
        void runStream(run_id);
        return;
      } catch {
        /* fall through */
      }
    }

    const raw = sessionStorage.getItem("prefillRun");
    if (!raw) return;
    sessionStorage.removeItem("prefillRun"); // consume once
    try {
      const p = JSON.parse(raw) as {
        competitors?: string[];
        topics?: string[];
        urls?: string[];
        context?: string;
        auto?: boolean;
      };
      setCompetitors(p.competitors ?? []);
      setTopics(p.topics ?? []);
      setUrls(p.urls ?? []);
      setContext(p.context ?? "");
      if (p.auto) {
        void runDiscovery({
          competitors: p.competitors ?? [],
          topics: p.topics ?? [],
          urls: p.urls ?? [],
          context: p.context?.trim() || null,
        });
      }
    } catch {
      /* ignore malformed prefill */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleApprove(
    decisions: { candidate_id: string; approved: boolean }[],
    mechanism: "user_action" | "select_all" | "reject_all" | "skip",
  ) {
    if (!discover) return;
    setBusy(true);
    try {
      const token = await getToken();
      await api.approve(discover.run_id, decisions, mechanism, token);
      setStep("running");
      setBusy(false);
      await runStream(discover.run_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start");
      setStep("error");
      setBusy(false);
    }
  }

  async function runStream(runId: string) {
    setActiveRunId(runId);
    const token = await getToken();
    try {
      await streamRun(runId, token, (name, data) => {
        if (name === "unreachable") {
          setUnreachable((u) => [
            ...u,
            {
              source_id: String(data.source_id),
              url: String(data.url),
              reason: data.reason ? String(data.reason) : undefined,
            },
          ]);
        } else if (name === "complete" || name === "cancelled") {
          router.push(`/dashboard/${runId}`);
        } else if (name === "error") {
          setError(String(data.detail ?? "pipeline failed"));
          setStep("error");
        } else {
          setLog((l) => [...l, describe(name, data)]);
        }
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Stream error");
      setStep("error");
    }
  }

  async function pasteFor(sourceId: string, url: string, content: string) {
    if (!activeRunId) return;
    const token = await getToken();
    await api.pasteSource(activeRunId, { source_id: sourceId, url, content }, token);
    setUnreachable((u) => u.filter((s) => s.source_id !== sourceId));
  }

  async function skipFor(sourceId: string) {
    if (!activeRunId) return;
    const token = await getToken();
    await api.skipSource(activeRunId, sourceId, token);
    setUnreachable((u) => u.filter((s) => s.source_id !== sourceId));
  }

  async function stopRun() {
    if (!activeRunId) return;
    try {
      await api.cancelRun(activeRunId, await getToken());
      // the pipeline emits a "cancelled" event → runStream navigates to detail
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop");
    }
  }

  return (
    <>
      <Header />
      <main className="mx-auto flex max-w-2xl flex-col gap-8 px-6 py-12">
        {step === "input" && (
          <form onSubmit={handleDiscover} className="flex flex-col gap-5">
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">New research run</h1>
              <p className="text-sm text-muted">
                Enter competitors, topics, and source URLs. We’ll suggest more sources before we start.
              </p>
            </div>
            <Field label="Competitors">
              <TagInput value={competitors} onChange={setCompetitors} placeholder="Type a competitor, press Enter…" />
            </Field>
            <Field label="Topics" required>
              <TagInput value={topics} onChange={setTopics} placeholder="Type a topic, press Enter…" />
            </Field>
            <Field label="Source URLs" required>
              <UrlList value={urls} onChange={setUrls} />
            </Field>
            <Field label="Context (optional)">
              <input
                value={context}
                onChange={(e) => setContext(e.target.value)}
                placeholder="Focus on EMEA launches in the last 6 months"
                className="input"
              />
            </Field>
            {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
            <button
              type="submit"
              className="w-fit rounded-full bg-accent px-6 py-2.5 text-sm font-medium text-white hover:bg-accent-hover"
            >
              Find sources
            </button>
          </form>
        )}

        {step === "discovering" && <Spinner label="Discovering additional sources…" />}

        {step === "approval" && discover && (
          <ApprovalGate
            candidates={discover.candidates}
            discoverySkipped={discover.discovery_skipped}
            onSubmit={handleApprove}
            busy={busy}
          />
        )}

        {step === "running" && (
          <div className="flex flex-col gap-5">
            <div className="flex items-center justify-between gap-4">
              <h1 className="text-xl font-semibold tracking-tight">Researching…</h1>
              <button
                onClick={stopRun}
                className="shrink-0 rounded-full border border-red-300 px-4 py-1.5 text-sm font-medium text-red-600 transition hover:bg-red-50 dark:hover:bg-red-950/30"
              >
                Stop
              </button>
            </div>
            {unreachable.map((s) => (
              <SourceFallback
                key={s.source_id}
                url={s.url}
                reason={s.reason}
                onPaste={(content) => pasteFor(s.source_id, s.url, content)}
                onSkip={() => skipFor(s.source_id)}
              />
            ))}
            <ul className="flex flex-col gap-1 text-sm text-muted">
              {log.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
            <Spinner label="Streaming progress…" />
          </div>
        )}

        {step === "error" && (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
            <button
              onClick={() => setStep("input")}
              className="w-fit rounded-full border border-border px-5 py-2 text-sm font-medium hover:bg-surface"
            >
              Start over
            </button>
          </div>
        )}
      </main>
    </>
  );
}

function Field({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">
        {label} {required && <span className="text-muted">*</span>}
      </span>
      {children}
    </label>
  );
}

function Spinner({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 text-sm text-muted">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
      {label}
    </div>
  );
}

function describe(name: string, data: Record<string, unknown>): string {
  const stage = String(data.stage ?? name);
  if (name === "stage_start") {
    if (stage === "FETCHING") return `Fetching ${data.total_urls ?? ""} sources…`;
    if (stage === "SUMMARIZING") return "Summarizing findings…";
    if (stage === "JUDGING") return `Verifying ${data.claims_total ?? ""} claims…`;
    if (stage === "PROCESSING") return "Analyzing sources…";
    return `${stage}…`;
  }
  if (name === "progress" && data.url) {
    return `${new URL(String(data.url)).hostname} — ${data.status}`;
  }
  return stage;
}
