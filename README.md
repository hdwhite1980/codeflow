# Code Flow

Multi-AI app builder with an auditable ledger, dependency graph, and a
local Code Guardian that knows your project well enough to tell you what
breaks when you change something.

## What's in this repo

The whole product lives in one repo, deployed across three places:

```
codeflow/
├── README.md                          # this file
├── .gitignore
│
├── ledger.py                          # Postgres ledger store (production)
├── ledger_memory.py                   # In-memory store (tests/dev)
├── runtime_sync.py                    # GitHub / Supabase / Railway connectors
├── symbol_extractor.py                # Parse code into symbols + column refs
├── guardian.py                        # Local-LLM-powered code companion
├── kinds_v2.py                        # Artifact and edge kind enums (v2 set)
│
├── jobqueue.py                        # Redis / in-memory job queue abstraction
├── job_handlers.py                    # Worker handlers keyed by job kind
├── anthropic_client.py                # Async httpx wrapper for Messages API
├── build_pipeline.py                  # Spec + file generation via Anthropic
│
├── app.py                             # FastAPI orchestrator
├── worker.py                          # Background queue + reconcile worker
├── hetzner_client.py                  # Clients for Ollama and the sandbox
│
├── test_runtime_sync.py               # End-to-end runtime sync tests
├── test_guardian.py                   # Guardian tests with fake LLM
├── test_queue_and_handlers.py         # Queue + dispatcher tests
├── test_build_pipeline.py             # Build pipeline tests with fake Anthropic
│
├── Dockerfile                         # Image for Railway (web + worker)
├── docker-entrypoint.sh               # Branches on CODEFLOW_ROLE
├── requirements.txt                   # Python deps for Railway image
├── railway.toml                       # Railway service config
│
├── deploy/
│   ├── supabase_migration.sql         # Source of truth for the DB schema.
│   │                                  # Run in Supabase SQL editor at setup.
│   └── hetzner_sandbox.md             # Runbook for the Hetzner box
│
└── sandbox/                           # Code that runs on Hetzner
    ├── runner.py                      # FastAPI sandbox runner
    ├── requirements.txt               # Minimal deps for the sandbox
    └── codeflow-sandbox.service       # Systemd unit
```

## Where each piece deploys

```
┌─────────────────────────────────────────────────────────────────────┐
│ GitHub:                                                             │
│   Hosts the source. CI/CD-style auto-deploys to Railway on push.   │
│   This is what you push to from your laptop.                       │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                  push to main    │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Railway (two services from one image):                              │
│                                                                     │
│   web service     ── app.py via uvicorn                            │
│                      Public HTTPS, FastAPI orchestrator             │
│                      Reads/writes Supabase                          │
│                      Calls Hetzner for sandbox + guardian           │
│                                                                     │
│   worker service  ── worker.py                                     │
│                      Internal only, no public network               │
│                      Build queue consumer + reconcile crons         │
│                                                                     │
│   Both built from: Dockerfile + requirements.txt                    │
│   Config:          railway.toml                                     │
└─────────────────────────────────────────────────────────────────────┘
                  │                                    │
                  │ ledger reads/writes               │ sandbox + LLM calls
                  ▼                                    ▼
┌──────────────────────────────────┐  ┌──────────────────────────────────┐
│ Supabase:                        │  │ Hetzner (your AX server):        │
│                                  │  │                                  │
│   - ledger Postgres              │  │   - Caddy (TLS + bearer auth)   │
│   - auth                         │  │   - Ollama (Tier 2 LLM, 7B)     │
│   - realtime to frontend         │  │   - Sandbox runner (port 8080) │
│                                  │  │                                  │
│   Migration:                     │  │   Files from this repo:          │
│     deploy/supabase_migration.sql│  │     sandbox/runner.py            │
│                                  │  │     sandbox/requirements.txt     │
│                                  │  │     sandbox/codeflow-sandbox.    │
│                                  │  │         service                  │
│                                  │  │                                  │
│                                  │  │   Setup runbook:                 │
│                                  │  │     deploy/hetzner_sandbox.md    │
└──────────────────────────────────┘  └──────────────────────────────────┘
```

## Deployment order (do this once)

1. **GitHub** — Create a new private repo. Push this entire codebase to
   `main`. That's the source of truth from now on.

2. **Supabase** — Create a project. Open the SQL Editor and paste the
   contents of `deploy/supabase_migration.sql`. Run it. Grab the
   `DATABASE_URL` from project settings (use the connection string with
   the service role, not the anon key — the orchestrator needs to bypass
   RLS).

3. **Hetzner** — Follow `deploy/hetzner_sandbox.md`. You'll install
   Docker, Ollama, Caddy, then clone this repo to `/opt/codeflow` and
   install the systemd service from `sandbox/codeflow-sandbox.service`.

4. **Railway** — Create a new project. Connect it to the GitHub repo.
   Railway will see `railway.toml` and configure two services (web and
   worker). Set the env vars listed in the comments at the top of
   `railway.toml` — `DATABASE_URL` from Supabase, `HETZNER_OLLAMA_URL`
   and `HETZNER_SANDBOX_URL` pointing at your Hetzner box, and the API
   keys for the frontier LLM providers.

## Files NOT to commit

The `.gitignore` covers the usual suspects, but worth being explicit:

- `**/.env` — local env files
- `**/__pycache__/` — Python bytecode
- `**/.venv/` — virtual environments
- `*.pem`, `*.key` — certificates and private keys
- `.DS_Store`, `Thumbs.db` — OS junk

Never commit:
- `.env` files of any kind
- API keys for Anthropic, OpenAI, or any provider
- Database connection strings
- Caddy's auto-generated certificates or Hetzner bearer tokens

If you do accidentally commit a secret, rotate it immediately at the
provider; git history is forever.

## Running the tests

From the repo root, with Python 3.12 and `pip install -r requirements.txt`:

```bash
python ledger_memory.py        # 8 ledger tests
python test_runtime_sync.py    # 6 connector + impact tests
python test_guardian.py        # 10 guardian tests
```

All 24 should pass. They use in-memory stores and fake clients — no
network or database needed.

## What's done and what's not

**Done (tested):**
- Ledger schema, supersession, content-addressable artifact storage
- Dependency graph with typed nodes and edges
- Impact analysis with severity escalation across symbol→file→commit→deploy
- Symbol extraction (Python + TypeScript) with SQL column reference mining
- Connectors for Supabase / GitHub / Railway with reconcile + webhook paths
- Code Guardian: summarize, assess change risk, detect anomalies
- FastAPI app with 9 routes including impact endpoints
- Sandbox runner with two-phase docker exec (network ON, network OFF)
- Dockerfile, Railway config, Supabase migration with RLS + realtime

**Stubbed (need wiring before production):**
- Worker queue consumer (Redis list pop + dispatch)
- Per-project connector config table (no UI yet for customer onboarding)
- Anthropic / OpenAI clients for Tier 3 of the build pipeline
- The actual multi-AI vote-and-synthesize loop
- The React Flow frontend

**Not yet built:**
- Stripe billing
- Customer-facing project setup flow
- Audit gate AIs (Gates 1-4)
- Operational dashboards
