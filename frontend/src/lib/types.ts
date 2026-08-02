// Frontend mirror of the backend Pydantic schemas (app/models/schemas.py).
// Kept deliberately hand-written and small rather than code-generated: the
// API surface is a handful of shapes, and a reviewer can read this file
// next to the Python one and confirm they match. If the contract grew,
// generating these from the FastAPI OpenAPI schema would be the next step.

export type Verdict = "supported" | "partial" | "unsupported" | "low_confidence";

export type RunStatus =
  | "DISCOVERING"
  | "AWAITING_APPROVAL"
  | "PENDING"
  | "PROCESSING"
  | "COMPLETE"
  | "FAILED"
  | "CANCELLED";

export type SourceStatus =
  | "OK"
  | "ERROR"
  | "OK_PASTED"
  | "OK_PDF_UPLOAD"
  | "SKIPPED_BY_USER";

export type SourceOrigin =
  | "USER_SUPPLIED"
  | "USER_APPROVED_DISCOVERED"
  | "USER_PASTED_TEXT"
  | "USER_UPLOADED_PDF";

export interface RunRequest {
  competitors: string[];
  topics: string[];
  urls: string[];
  context?: string | null;
}

export interface DiscoveryCandidate {
  id: string;
  url: string;
  domain: string;
  page_title?: string | null;
  rationale: string;
  competitor: string;
  ssrf_safe: boolean;
  decision?: "APPROVED" | "REJECTED" | null;
}

export interface Claim {
  theme: string;
  text: string;
  source_url: string;
  source_passage?: string | null;
  verified: boolean;
  confidence: number;
  verdict: Verdict;
  judge_reason?: string | null;
}

export interface Theme {
  title: string;
  summary: string;
  claims: Claim[];
}

export interface ResearchReport {
  themes: Theme[];
}

export interface RunSource {
  url: string;
  status: SourceStatus;
  error?: string | null;
  source_origin: SourceOrigin;
}

export interface RunSummary {
  id: string;
  status: RunStatus;
  competitors: string[];
  topics: string[];
  created_at: string;
  completed_at?: string | null;
}

export interface ClaimChange {
  claim: Claim;
  previous_verdict: Verdict;
}

export interface RunChanges {
  has_previous: boolean;
  previous_run_id?: string | null;
  new_claims: Claim[];
  changed: ClaimChange[];
}

export interface RunMetrics {
  duration_ms: number;
  sources_fetched: number;
  claims_total: number;
  claims_verified: number;
  themes: number;
  summarizer_model: string;
  judge_model: string;
  input_tokens: number;
  output_tokens: number;
  /** Provider calls that actually reached a model (cache hits and claims
   *  resolved by the cheaper guardrail rungs don't count). */
  llm_calls: number;
  /** Web-search API queries issued during discovery (0 for a re-run). */
  search_calls: number;
  /** Rough USD cost at standard paid rates — the app runs on free tiers, so
   *  the real charge is $0. Embeddings excluded. */
  estimated_cost_usd: number;
  /** Claims the summarizer attributed to a URL that was never fetched, and
   *  which the structural attribution check dropped before verification. */
  claims_dropped_unattributed: number;
}

export interface RunDetail extends RunSummary {
  input_urls: string[];
  context?: string | null;
  sources: RunSource[];
  report?: ResearchReport | null;
  error_detail?: string | null;
  metrics?: RunMetrics | null;
}

export interface DiscoverResponse {
  run_id: string;
  status: RunStatus;
  discovery_skipped: boolean;
  candidates: DiscoveryCandidate[];
}

export interface ApproveResponse {
  run_id: string;
  status: RunStatus;
  approved_count: number;
  total_sources: number;
}

// SSE progress events emitted by the research pipeline. `stage` is the
// coarse phase; the rest is stage-specific and intentionally loose here.
export interface ProgressEvent {
  stage: string;
  url?: string;
  status?: string;
  error?: string;
  reason?: string;
  source_id?: string;
  [key: string]: unknown;
}
