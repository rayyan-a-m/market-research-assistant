# Deployment Guide

End-to-end instructions to deploy the Market Research Intelligence
Assistant: every account you need, how to generate every API key, exactly
where each key gets set, and the commands to get the backend live on
**Azure Container Apps** and the frontend on **Vercel**.

Read this top-to-bottom the first time. The order matters — some steps
produce values (URLs, keys) that later steps consume.

---

## 0. Architecture recap (so the deploy steps make sense)

Two deployable units, deployed to two platforms:

| Unit | What it is | Where it goes |
|---|---|---|
| `backend/` | FastAPI app (single container, single replica) | Azure Container Apps |
| `frontend/` | Next.js 15 app | Vercel |

Plus three managed services it talks to:

| Service | Purpose | Provider |
|---|---|---|
| PostgreSQL | Persistence (runs, sources, claims, discovery candidates) | Supabase |
| Auth | Google OAuth + passwordless email-code sign-in + JWT verification | Clerk |
| Summarizer / embeddings / discovery | Gemini (free tier / GCP credits) | Google AI Studio |
| Hallucination judge | Grades claims against their source, free tier | Google AI Studio (same key) |
| Discovery web search | Competitor source discovery | Serper |

> **Cost:** this stack is designed to run at ≈ $0 — Gemini has a free tier,
> and Azure Container Apps + Supabase + Clerk + Vercel all have free grants.
> The free LLM tiers are rate-limited, so the practical limit is
> requests-per-minute, not spend. Every run reports its own token usage,
> search-API hits, and an estimated cost so you can see the headroom.

> **One hard constraint, called out early because it drives an Azure
> setting below:** the backend keeps one piece of state in process memory —
> the `asyncio.Event` map (`pipeline/unreachable.py`) used to pause a
> running fetch while a user pastes content for (or skips) a blocked source. That only
> works with **exactly one replica and one worker process**. The Container
> App is therefore pinned to `min-replicas=1 max-replicas=1`. This is
> intentional for a lightweight deployment (see
> [`FUTURE_ENHANCEMENTS.md`](FUTURE_ENHANCEMENTS.md) #1); scaling out later
> means moving that pause state to Redis. (Discovery approval needs no
> in-memory state — it's plain request/response around DB rows.)

---

## 1. Accounts you need (all have free tiers)

Create these first. Links are to the signup/console pages.

1. **Azure** — https://azure.microsoft.com/free (hosts the backend). Needs a
   credit card for identity even on the free tier; Container Apps has a
   generous monthly free grant.
2. **Vercel** — https://vercel.com/signup (hosts the frontend). Sign in
   with GitHub for the smoothest repo import.
3. **Supabase** — https://supabase.com/dashboard (PostgreSQL).
4. **Clerk** — https://dashboard.clerk.com (auth).
5. **Google AI Studio** — https://aistudio.google.com/apikey (Gemini API
   key; free tier, or uses your free GCP credits).
6. **GitHub** — https://github.com (source repo for Vercel to deploy from).
   No separate LLM account is needed: the judge uses the same
   `GEMINI_API_KEY` as the summarizer.
7. **Serper** — https://serper.dev (web search for source discovery; free
   tier ~2,500 queries).

Local tools:

- **Azure CLI** — https://learn.microsoft.com/cli/azure/install-azure-cli
  (`brew install azure-cli` on macOS)
- **Docker** — to build the backend image
- **Node 20+** and **Python 3.12+** — for local runs

---

## 2. Generate every API key

Do all of these now and paste them into the root `.env` scratch file
(already created with placeholders). You'll distribute them to Azure and
Vercel in steps 5–6. **Never commit them** — `.gitignore` already excludes
`.env` and `.env.local`.

### 2.1 Google Gemini — `GEMINI_API_KEY`

Powers the summarizer, discovery ranking, and embeddings.

1. Go to https://aistudio.google.com/apikey (sign in with your Google
   account — the same one that has your free GCP credits).
2. **Create API key** → pick a Google Cloud project (or let it create one)
   → copy the `AIza...` value.
3. The free tier is rate-limited but has no dollar cost. If you'd rather
   bill against your GCP credits (higher limits), enable the **Generative
   Language API** on the project; the same key then draws on the project's
   quota/credits. No code change either way — it's the same `GEMINI_API_KEY`.

### 2.2 The hallucination judge — no extra key needed

**Nothing to do in this step.** The judge runs on `gemini-flash-latest`
using the same `GEMINI_API_KEY` from §2.1, so there is no second provider
account to create.

A genuinely *cross-family* judge (a non-Gemini model grading Gemini's output)
is the ideal, because same-family judging shares blind spots. For the scope of
this application the judge is a cheaper Gemini model grading a stronger one —
a cross-*model* check within the same family, using the same `GEMINI_API_KEY`.
It still grades each claim against the original source; the trade-off is
documented in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) #10.

If the judge call fails at runtime, the stage marks claims *unverified* and the
run completes — verification degrades, it doesn't crash the run.

### 2.3 Serper — `SERPER_API_KEY`

The web-search provider for source discovery.

1. Go to https://serper.dev → sign up → **API Key** in the dashboard.
2. Copy the key. Serper's free tier (~2,500 queries) is plenty for a demo.

### 2.4 Supabase — `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`

1. https://supabase.com/dashboard → **New project**. Name it
   `market-intel`, pick a region near your Azure region, set a strong DB
   password (save it).
2. Wait for provisioning (~2 min), then:
   - **Project Settings → API**:
     - `SUPABASE_URL` = the **Project URL** (`https://<ref>.supabase.co`)
     - `SUPABASE_SERVICE_ROLE_KEY` = the **service_role** secret (NOT the
       anon key — the backend needs to bypass RLS as a trusted server;
       this key must never reach the browser)
   - **Project Settings → Database → Connection string → URI**:
     - `DATABASE_URL` = the `postgresql://postgres:[PASSWORD]@...` URI,
       with `[PASSWORD]` replaced by the DB password from step 1. Use the
       **Session pooler** (port 5432) connection string.
3. Create the schema: open **SQL Editor → New query**, paste the DDL from
   `backend/db/schema.sql` (the 4 tables: `runs`, `discovery_candidates`,
   `run_sources`, `claims`, plus their RLS policies), and **Run**.

### 2.5 Clerk — publishable + secret keys

1. https://dashboard.clerk.com → **Create application**. Name it
   `Market Intel`.
2. Enable two sign-in methods under **User & Authentication** so any reviewer
   can get in (see `DESIGN_DECISIONS.md` #14):
   - **SSO connections → Google** (for a demo, Clerk's shared Google
     credentials work; for a "real" deployment, plug in your own Google OAuth
     client under the Google connection settings).
   - **Email, phone, username → Email**: turn on **sign-up with email** and
     **sign-in with email**, both using **Email verification code**. Leave
     **Password OFF** — it's passwordless by design.
3. **API Keys** page gives you:
   - `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` = the `pk_...` value (frontend)
   - `CLERK_SECRET_KEY` = the `sk_...` value (both frontend server + backend)
4. Leave this tab open — you'll add the deployed frontend URL to Clerk's
   allowed origins in step 7.

---

## 3. Env var reference — what goes where

| Variable | Value from | Backend (Azure) | Frontend (Vercel) |
|---|---|---|---|
| `ENV` | literal `production` | ✅ | — |
| `GEMINI_API_KEY` | §2.1 | ✅ (secret) | — |
| `SERPER_API_KEY` | §2.3 | ✅ (secret) | — |
| `DATABASE_URL` | §2.4 | ✅ (secret) | — |
| `SUPABASE_URL` | §2.4 | ✅ | — |
| `SUPABASE_SERVICE_ROLE_KEY` | §2.4 | ✅ (secret) | — |
| `CLERK_SECRET_KEY` | §2.5 | ✅ (secret) | ✅ (secret) |
| `FRONTEND_ORIGIN` | Vercel URL (step 6) | ✅ | — |
| `JSON_LOGS` | literal `true` in production | ✅ | — |
| `LOG_LEVEL` | `INFO` (default) | ➖ (optional) | — |
| `RUN_TOKEN_BUDGET` | `250000` (default) | ➖ (optional) | — |
| `RATE_LIMIT_RUNS_PER_WINDOW` | `5` (default) | ➖ (optional) | — |
| `RATE_LIMIT_DISCOVERY_PER_WINDOW` | `10` (default) | ➖ (optional) | — |
| `NEXT_PUBLIC_API_URL` | Azure URL (step 5) | — | ✅ |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | §2.5 | — | ✅ |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL` | literal `/sign-in` | — | ✅ |
| `NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL` | literal `/dashboard` | — | ✅ |
| `NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL` | literal `/dashboard` | — | ✅ |

"secret" = store as an Azure Container Apps secret / Vercel encrypted env
var, not as a plain value. The chicken-and-egg between `FRONTEND_ORIGIN`
and `NEXT_PUBLIC_API_URL` is resolved in step 7 (deploy both, then set
each other's URL).

> The three `NEXT_PUBLIC_CLERK_*` redirect vars look optional and are not.
> Without `NEXT_PUBLIC_CLERK_SIGN_IN_URL`, Clerk's middleware sends a
> signed-out visitor to its own hosted page on `<slug>.accounts.dev` instead
> of this app's `/sign-in` route, so the app's own sign-in page never renders
> in production. All `NEXT_PUBLIC_*` values are baked in at build time —
> adding them later means redeploying the frontend, not just saving them.

> **Local dev shortcut:** the backend reads the repo-root `.env` directly
> (`config.py` loads `../.env`), so for local runs you only fill that one
> file — no need to copy the backend block into `backend/.env`. The frontend
> still needs its block in `frontend/.env.local` (Next.js only reads env
> files from the `frontend/` folder). Set `AUTH_DISABLED=true` in the root
> `.env` to run the backend before Clerk is wired up.

---

## 4. Deploy a hello-world first (do this on day one)

Before wiring real keys, prove the pipe works end to end. This catches
Azure quota/provisioning problems early instead of on submission night.

```bash
az login
az account set --subscription "<your-subscription-id>"

# Names used throughout — change the suffix to something globally unique
RG=market-intel-rg
LOC=eastus
ACR=marketintelacr$RANDOM     # ACR names are global; must be unique + lowercase
ENVNAME=market-intel-env
APP=market-intel-api

az group create --name $RG --location $LOC

# Container registry to hold the backend image
az acr create --resource-group $RG --name $ACR --sku Basic --admin-enabled true

# Container Apps environment
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az containerapp env create --name $ENVNAME --resource-group $RG --location $LOC
```

Record `$ACR` and `$RG` — the real deploy in step 5 reuses them.

---

## 5. Deploy the backend to Azure Container Apps

### 5.1 Build and push the image

From the repo root:

```bash
# Build in ACR (no local Docker needed) — uses backend/Dockerfile
az acr build --registry $ACR --image market-intel-api:latest ./backend
```

> The Dockerfile installs Playwright/Chromium for crawl4ai, so the image
> is large (~1.5GB) and the first build takes a few minutes. That's
> expected.

### 5.2 Create the Container App with all env vars

Run **§5.1 (`az acr build`) first** — the image must exist in the registry
before this step, or you'll get `MANIFEST_UNKNOWN: manifest tagged by "latest"
is not found`.

Set secrets first, then reference them. The backend reads exactly three
secrets (`GEMINI_API_KEY`, `SERPER_API_KEY`, `DATABASE_URL`) plus three plain
env vars (`ENV`, `JSON_LOGS`, `FRONTEND_ORIGIN`). `SUPABASE_*` and
`CLERK_SECRET_KEY` are **not** read by the backend (it uses `DATABASE_URL` for
Postgres and Clerk's public JWKS for auth), so they're omitted here — set
`CLERK_SECRET_KEY` on Vercel instead. Fill in the values from §2, and use the
**Session Pooler** `DATABASE_URL` from §2.4 (the direct `db.<ref>.supabase.co`
host is IPv6-only and unreachable from Azure):

```bash
az containerapp create \
  --name $APP \
  --resource-group $RG \
  --environment $ENVNAME \
  --image $ACR.azurecr.io/market-intel-api:latest \
  --registry-server $ACR.azurecr.io \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 1.0 --memory 2.0Gi \
  --secrets \
      gemini-key="AIza..." \
      serper-key="your-serper-key" \
      database-url="postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres" \
  --env-vars \
      ENV=production \
      GEMINI_API_KEY=secretref:gemini-key \
      SERPER_API_KEY=secretref:serper-key \
      DATABASE_URL=secretref:database-url \
      JSON_LOGS=true \
      FRONTEND_ORIGIN="https://PLACEHOLDER.vercel.app"
```

> `--min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi`:
> single replica because of the in-memory state constraint (§0); 2GiB
> because headless Chromium needs the headroom. Do not enable
> autoscaling on this app.

`FRONTEND_ORIGIN` is a placeholder for now — you'll correct it in step 7
once Vercel gives you the real URL.

### 5.3 Get the backend URL and smoke-test it

```bash
az containerapp show --name $APP --resource-group $RG \
  --query properties.configuration.ingress.fqdn -o tsv
# -> market-intel-api.<hash>.<region>.azurecontainerapps.io

curl https://market-intel-api.greendune-83286828.eastus.azurecontainerapps.io/healthz
# -> {"status":"ok"}
```

That full `https://<fqdn>` is your **`NEXT_PUBLIC_API_URL`** for Vercel.

### 5.4 Redeploying after code changes

```bash
az acr build --registry $ACR --image market-intel-api:latest ./backend
az containerapp update --name $APP --resource-group $RG \
  --image $ACR.azurecr.io/market-intel-api:latest
```

---

## 6. Deploy the frontend to Vercel

1. Push the repo to GitHub (if you haven't) — see §9.
2. https://vercel.com/new → **Import** the GitHub repo.
3. **Root Directory**: set to `frontend` (the Next.js app is not at the
   repo root). Vercel auto-detects Next.js 15; leave build/output defaults.
4. **Environment Variables** — add all six:
   - `NEXT_PUBLIC_API_URL` = the Azure `https://<fqdn>` from step 5.3
   - `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` = `pk_...` from §2.5
   - `CLERK_SECRET_KEY` = `sk_...` from §2.5 (mark as sensitive)
   - `NEXT_PUBLIC_CLERK_SIGN_IN_URL` = `/sign-in`
   - `NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL` = `/dashboard`
   - `NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL` = `/dashboard`

   Skipping the last three is the easy mistake here: the build succeeds and
   sign-in still works, but visitors land on Clerk's hosted `accounts.dev`
   page rather than this app's own `/sign-in` route.
5. **Deploy**. Vercel gives you a URL like
   `https://market-intel-<hash>.vercel.app`. That is your
   **`FRONTEND_ORIGIN`**.

---

## 7. Wire the two together (the cross-references)

Now that both URLs exist, close the loop:

1. **Backend CORS** — point it at the real frontend origin:
   ```bash
   az containerapp update --name $APP --resource-group $RG \
     --set-env-vars FRONTEND_ORIGIN="https://market-intel-<hash>.vercel.app"
   ```
2. **Clerk allowed origins** — in the Clerk dashboard → your app →
   **Domains** (or **Paths/Allowed origins**), add the Vercel URL so
   Clerk will issue sessions to it. If you attach a custom domain later,
   add that too.
3. **Redeploy the frontend** only if you changed any `NEXT_PUBLIC_*` var
   after the first deploy (those are baked in at build time).

---

## 8. Verify the full flow

Do this signed **out** — in a private window. Half the things that break at
this stage only break for a visitor who has no session yet.

1. Open the Vercel URL, click **Start a research run** → you should land on
   the app's own `/sign-in` page (branded, not `accounts.dev`; if it's the
   latter, the redirect vars from §3 are missing).
2. Sign in with **Google**, or with an **email one-time code** → land on
   `/dashboard`.
3. Start a research run with 2–3 real URLs (e.g. a couple of competitor
   blog posts).
4. Confirm: discovery candidates appear → approve/skip → live SSE progress
   → themed report with per-claim verdicts and source links.
5. If something fails, check backend logs:
   ```bash
   az containerapp logs show --name $APP --resource-group $RG --follow
   ```

### Cost note (per run)

This stack is designed to run at **≈ $0**. A typical 3–5 URL run is: one
`gemini-pro-latest` summarization call (the largest single unit of work,
driven by retrieved-context size) on the free tier / GCP credits; N judge
calls on `gemini-flash-latest` (one per claim, gated by a ladder of cheaper
checks — a claim citing an unfetched URL is dropped for free, and one with no
closely-matching source content is resolved by an embedding comparison, both
before any LLM call); and a
batch of `gemini-embedding-001` calls (retrieval + discovery ranking + judge
similarity — discovery ranks with embeddings, not a chat model). The real
constraint is the free tiers' **rate limits** (requests-per-minute), not
dollars — hard input bounds (`MAX_URLS=10`, `MAX_TOPICS=5`) keep a single run
inside them. The Pro-tier summarizer model has the tightest free-tier rate limit; if you hit
it, set `SUMMARIZER_MODEL=gemini-2.5-flash` (a quota trade, not a code change).

---

## 9. Push to GitHub

From the repo root (`progressAssesment/` locally — rename as you like):

```bash
git init
git add .
git commit -m "Market Research Intelligence Assistant"
git branch -M main
git remote add origin https://github.com/<you>/market-research-assistant.git
git push -u origin main
```

`.gitignore` already excludes `.env*`, `node_modules/`, `.venv/`,
`.next/`, `.claude/`, and the local `_reference/` folder. **Before pushing,
confirm no secrets are staged** (this greps the working tree for key-shaped
strings):

```bash
git grep -nE 'AIza[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|BSA[A-Za-z0-9]|service_role|postgresql://' -- ':!*.example' ':!deployment.md' || echo "clean"
```

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Frontend loads but API calls fail with CORS error | `FRONTEND_ORIGIN` on backend ≠ actual Vercel URL | Re-run step 7.1 with the exact origin (no trailing slash) |
| Sign-in redirect loops | Vercel URL not in Clerk allowed origins | Add it in Clerk dashboard (step 7.2) |
| `/healthz` works but runs hang at discovery | Missing/invalid `SERPER_API_KEY` | Discovery is non-blocking by design; check logs, re-set the secret |
| Backend 500s on any LLM stage | Missing/invalid provider key, or no billing on the provider | `az containerapp logs show`; the error is normalized and names the provider |
| Container keeps restarting | 2GiB not enough / Chromium OOM | Confirm `--memory 2.0Gi`; lower `FETCH_CONCURRENCY` env var |
| Run "stuck" after a redeploy | In-memory discovery/pause state lost on restart (§0) | Expected trade-off; start a new run. Documented limitation |
| `az acr build` fails | Not logged in / wrong subscription | `az login`, `az account set --subscription ...` |
