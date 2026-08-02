"use client";

import { useState } from "react";

interface Props {
  url: string;
  reason?: string;
  onPaste: (content: string) => Promise<void>;
  onSkip: () => Promise<void>;
}

// Rendered when the fetcher pauses on a source it couldn't use (unreachable,
// or a login/authwall page). The run is blocked here until the user chooses:
// paste the content, or continue without this source. Both resume the run.
export function SourceFallback({ url, reason, onPaste, onSkip }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<null | "paste" | "skip">(null);
  const [done, setDone] = useState<null | "recovered" | "skipped">(null);
  const [error, setError] = useState<string | null>(null);

  const host = safeHost(url);

  if (done) {
    return (
      <div className="rounded-xl border border-border bg-surface p-3 text-sm">
        <span className="text-muted">
          {done === "recovered" ? "Added your content for " : "Continuing without "}
        </span>
        <span className="font-medium">{host}</span>
      </div>
    );
  }

  async function run(kind: "paste" | "skip", fn: () => Promise<void>) {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      setDone(kind === "paste" ? "recovered" : "skipped");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-amber-300 bg-amber-50 p-4 dark:border-amber-900/50 dark:bg-amber-950/20">
      <div className="text-sm">
        <span className="font-medium">Couldn’t use {host}.</span>{" "}
        <span className="text-muted">
          {reason ? `${reason}. ` : ""}Paste the content to include it, or continue without this
          source.
        </span>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Select the article body in your browser and paste it here…"
        rows={5}
        className="w-full rounded-lg border border-border bg-background p-2 text-sm"
      />

      <div className="flex flex-wrap gap-3">
        <button
          onClick={() => run("paste", () => onPaste(text))}
          disabled={busy !== null || !text.trim()}
          className="rounded-full bg-accent px-4 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {busy === "paste" ? "Adding…" : "Add this content"}
        </button>
        <button
          onClick={() => run("skip", onSkip)}
          disabled={busy !== null}
          className="rounded-full border border-border px-4 py-1.5 text-sm font-medium hover:bg-surface disabled:opacity-50"
        >
          {busy === "skip" ? "Continuing…" : "Continue without this source"}
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}

function safeHost(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}
