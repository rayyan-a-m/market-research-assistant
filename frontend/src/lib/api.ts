// Single typed entry point to the backend. Every network call goes through
// `apiFetch`, which centralizes the base URL, the Clerk bearer token, and
// error normalization into `ApiError`. Components never build fetch calls or
// read `process.env` directly — same reasoning as the backend provider
// factory: one seam to change auth or base URL.

import type {
  ApproveResponse,
  DiscoverResponse,
  DiscoveryCandidate,
  RunChanges,
  RunDetail,
  RunRequest,
  RunSummary,
} from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface FetchOptions extends RequestInit {
  token?: string | null;
}

async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const { token, headers, ...rest } = options;
  const res = await fetch(`${API_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
  });

  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = await res.text();
    }
    const message =
      typeof detail === "object" && detail !== null && "detail" in detail
        ? String((detail as { detail: unknown }).detail)
        : `Request failed with ${res.status}`;
    throw new ApiError(message, res.status, detail);
  }

  return res.json() as Promise<T>;
}

export const api = {
  listRuns: (token?: string | null) => apiFetch<RunSummary[]>("/api/runs", { token }),

  getRun: (id: string, token?: string | null) =>
    apiFetch<RunDetail>(`/api/runs/${id}`, { token }),

  getChanges: (id: string, token?: string | null) =>
    apiFetch<RunChanges>(`/api/runs/${id}/changes`, { token }),

  getCandidates: (id: string, token?: string | null) =>
    apiFetch<DiscoveryCandidate[]>(`/api/runs/${id}/candidates`, { token }),

  cancelRun: (id: string, token?: string | null) =>
    apiFetch<{ run_id: string; status: string }>(`/api/runs/${id}/cancel`, {
      method: "POST",
      token,
    }),

  deleteRun: async (id: string, token?: string | null) => {
    const res = await fetch(`${API_URL}/api/runs/${id}`, {
      method: "DELETE",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new ApiError(detail?.detail ?? `Delete failed (${res.status})`, res.status, detail);
    }
  },

  // Phase A: kick off discovery. Returns run + candidate sources for the gate.
  startDiscovery: (body: RunRequest, token?: string | null) =>
    apiFetch<DiscoverResponse>("/api/runs/discover", {
      method: "POST",
      body: JSON.stringify(body),
      token,
    }),

  approve: (
    runId: string,
    decisions: { candidate_id: string; approved: boolean }[],
    mechanism: "user_action" | "select_all" | "reject_all" | "skip",
    token?: string | null,
  ) =>
    apiFetch<ApproveResponse>(`/api/runs/${runId}/approve`, {
      method: "POST",
      body: JSON.stringify({ decisions, mechanism }),
      token,
    }),

  // Re-run reusing the run's inputs + previously approved discovered sources,
  // skipping discovery. Returns the new run id (left PENDING, ready to /start).
  rerun: (runId: string, token?: string | null) =>
    apiFetch<{ run_id: string; status: string; reused_sources: number }>(
      `/api/runs/${runId}/rerun`,
      { method: "POST", token },
    ),

  pasteSource: (
    runId: string,
    body: { source_id: string; url: string; content: string },
    token?: string | null,
  ) =>
    apiFetch<{ source_id: string; status: string; char_count: number }>(
      `/api/runs/${runId}/sources/paste`,
      { method: "POST", body: JSON.stringify(body), token },
    ),

  // Drop a blocked source and let the run continue without it.
  skipSource: (runId: string, sourceId: string, token?: string | null) =>
    apiFetch<{ source_id: string; status: string; resolved: boolean }>(
      `/api/runs/${runId}/sources/skip`,
      { method: "POST", body: JSON.stringify({ source_id: sourceId }), token },
    ),

  uploadSource: async (runId: string, sourceId: string, file: File, token?: string | null) => {
    const form = new FormData();
    form.append("source_id", sourceId);
    form.append("file", file);
    const res = await fetch(`${API_URL}/api/runs/${runId}/sources/upload`, {
      method: "POST",
      body: form,
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new ApiError(detail?.detail ?? `Upload failed with ${res.status}`, res.status, detail);
    }
    return res.json() as Promise<{ source_id: string; status: string; char_count: number }>;
  },
};

export type SseHandler = (eventName: string, data: Record<string, unknown>) => void;

// Start the pipeline and consume its SSE stream. We use fetch + ReadableStream
// (not EventSource) so the Clerk JWT travels in the Authorization header, never
// in the URL. Resolves when the stream ends.
export async function streamRun(
  runId: string,
  token: string | null,
  onEvent: SseHandler,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_URL}/api/runs/${runId}/start`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    signal,
  });
  if (!res.ok || !res.body) {
    throw new ApiError(`Failed to start run (${res.status})`, res.status);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (frame.startsWith(":")) continue; // heartbeat comment

      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) {
        try {
          onEvent(eventName, JSON.parse(dataLines.join("\n")));
        } catch {
          /* ignore malformed frame */
        }
      }
    }
  }
}
