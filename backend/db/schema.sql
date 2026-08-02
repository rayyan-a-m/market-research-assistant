-- Market Research Intelligence Assistant — database schema
--
-- Run this once in the Supabase SQL editor (see deployment.md §2.5).
-- Four tables: runs, discovery_candidates, run_sources, claims.
--
-- Not included here (they belong to the scale-out roadmap in
-- FUTURE_ENHANCEMENTS.md, not the current build): a `chunks` pgvector
-- table (retrieval is in-memory per run), `url_cache`, and `llm_audit_log`.
-- There's also no separate `users` table — the Clerk user id is carried
-- inline on `runs.user_id`.
--
-- On RLS and how ownership is ACTUALLY enforced here — read this, it's a
-- real distinction:
--   The backend talks to Supabase with the SERVICE ROLE key, which bypasses
--   RLS by design (it's a trusted server). So the operative ownership check
--   is APPLICATION-LAYER: every router filters/writes by the authenticated
--   Clerk user id (runs.user_id). The RLS policies below are defense-in-depth
--   that only fire if a client ever queries these tables through a
--   Supabase-authenticated role (e.g. if the frontend later talked to
--   Supabase directly using a Clerk->Supabase JWT template). They are NOT
--   the thing protecting data today. Documented honestly rather than
--   claimed as the active control. See DESIGN_DECISIONS.md.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- Research runs -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           text        NOT NULL,          -- Clerk user id
  status            text        NOT NULL DEFAULT 'DISCOVERING',
  competitors       text[]      NOT NULL DEFAULT '{}',
  topics            text[]      NOT NULL,
  input_urls        text[]      NOT NULL,
  context           text,                            -- optional analyst guidance
  discovery_skipped boolean     NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz,
  error_detail      text,
  metrics           jsonb,                            -- run stats: duration, tokens, counts
  CONSTRAINT runs_status_check CHECK (
    status IN ('DISCOVERING','AWAITING_APPROVAL','PENDING',
               'PROCESSING','COMPLETE','FAILED','CANCELLED')
  )
);
CREATE INDEX IF NOT EXISTS idx_runs_user_id ON runs (user_id, created_at DESC);

-- Discovery candidates (proposed sources, pre-approval) ---------------------
CREATE TABLE IF NOT EXISTS discovery_candidates (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid        NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  url          text        NOT NULL,
  domain       text        NOT NULL,
  page_title   text,
  rationale    text        NOT NULL,               -- rule-generated, no LLM call
  competitor   text        NOT NULL,
  ssrf_safe    boolean     NOT NULL DEFAULT false,
  decision     text,                                -- NULL | 'APPROVED' | 'REJECTED'
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT discovery_decision_check CHECK (
    decision IS NULL OR decision IN ('APPROVED','REJECTED')
  )
);
CREATE INDEX IF NOT EXISTS idx_candidates_run_id ON discovery_candidates (run_id);

-- Fetched / provided source documents ---------------------------------------
CREATE TABLE IF NOT EXISTS run_sources (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id           uuid        NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  url              text        NOT NULL,
  status           text        NOT NULL,
  error            text,
  content_markdown text,                            -- source of truth for the judge
  source_origin    text        NOT NULL DEFAULT 'USER_SUPPLIED',
  CONSTRAINT run_sources_status_check CHECK (
    status IN ('OK','ERROR','OK_PASTED','OK_PDF_UPLOAD','SKIPPED_BY_USER')
  ),
  CONSTRAINT run_sources_origin_check CHECK (
    source_origin IN ('USER_SUPPLIED','USER_APPROVED_DISCOVERED',
                      'USER_PASTED_TEXT','USER_UPLOADED_PDF')
  )
);
CREATE INDEX IF NOT EXISTS idx_run_sources_run_id ON run_sources (run_id);

-- Verified claims (the report is assembled from these rows) ------------------
CREATE TABLE IF NOT EXISTS claims (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id         uuid        NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  theme          text        NOT NULL,
  theme_summary  text        NOT NULL DEFAULT '',  -- theme-level prose, derived once
  text           text        NOT NULL,
  source_url     text        NOT NULL,
  source_passage text,
  verified       boolean     NOT NULL,
  confidence     double precision NOT NULL,
  verdict        text        NOT NULL,
  judge_reason   text,
  CONSTRAINT claims_verdict_check CHECK (
    verdict IN ('supported','partial','unsupported','low_confidence')
  )
);
CREATE INDEX IF NOT EXISTS idx_claims_run_id ON claims (run_id, theme);

-- Defense-in-depth RLS (see the header note — NOT the active control today) --
ALTER TABLE runs                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovery_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_sources          ENABLE ROW LEVEL SECURITY;
ALTER TABLE claims               ENABLE ROW LEVEL SECURITY;

-- These policies compare against a Clerk id carried in the JWT `sub` claim.
-- They only take effect for requests made with a Supabase-authenticated
-- role; the service-role backend bypasses them.
CREATE POLICY runs_owner ON runs FOR ALL
  USING (user_id = auth.jwt() ->> 'sub');

CREATE POLICY candidates_owner ON discovery_candidates FOR ALL
  USING (run_id IN (SELECT id FROM runs WHERE user_id = auth.jwt() ->> 'sub'));

CREATE POLICY sources_owner ON run_sources FOR ALL
  USING (run_id IN (SELECT id FROM runs WHERE user_id = auth.jwt() ->> 'sub'));

CREATE POLICY claims_owner ON claims FOR ALL
  USING (run_id IN (SELECT id FROM runs WHERE user_id = auth.jwt() ->> 'sub'));
