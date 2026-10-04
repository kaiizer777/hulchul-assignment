# Autonomous Browser Agent for ERP Operations & Invoice Processing

Production-grade, resilient autonomous AI agent executing browser-based ERP workflows, invoice reconciliation, and automated exception handling with human-in-the-loop approval gates.

> 📄 **Submission Documentation**: Comprehensive architecture review, 5-pillar alignment, recovery protocols, and enterprise roadmap are documented in the [**Engineering Submission Note**](ENGINEERING_NOTE.md).

---

## Tech Stack

- **Frontend & Mock ERP**: Next.js 16 App Router (Turbopack, TypeScript, Tailwind CSS v4), deployed on **Cloudflare Workers** (`@opennextjs/cloudflare`).
- **Backend Orchestrator**: FastAPI (Python 3.12, Uvicorn, Pydantic v2), deployed on **AWS Lambda** via AWS Lambda Web Adapter.
- **Database**: Neon Serverless PostgreSQL (`asyncpg`), storing invoices, purchase orders, agent runs, and step audit trails with base64 screenshots.
- **State & Coordination**: Upstash Redis (Serverless REST API) for active session state, pause/resume flags, and nonces for human-in-the-loop approval gates.
- **AI / LLM**: Groq API running `openai/gpt-oss-120b` for ReAct (Reason + Act) loop orchestration.
- **Browser Automation**: Playwright connecting over Remote CDP (Browserless / Steel.dev) — zero local browser binaries in serverless containers.

---

## Architecture & Flow

```mermaid
sequenceDiagram
    participant User
    participant ControlUI as Next.js Control UI (/agent)
    participant FastAPI as FastAPI Backend (ReAct Loop)
    participant Redis as Upstash Redis (Session/Approval)
    participant Groq as Groq LLM (GPT-OSS-120b)
    participant CDP as Remote Playwright CDP (Browserless)
    participant ERP as Mock Next.js ERP (/invoices)
    participant DB as Neon PostgreSQL

    User->>ControlUI: Enter plain-English goal & threshold
    ControlUI->>FastAPI: POST /api/agent/run (Goal)
    FastAPI->>DB: Create agent_run record
    loop ReAct Orchestration Loop
        FastAPI->>CDP: read_page() (Accessibility tree snapshot)
        CDP-->>FastAPI: DOM Accessibility Tree (~2-5KB)
        FastAPI->>Groq: Send Context (Goal + History + Snapshot)
        Groq-->>FastAPI: Tool Call (navigate, fill, click, check_exists)
        alt Approval Required (>₹50k or custom threshold)
            FastAPI->>Redis: Set state awaiting_approval
            FastAPI->>ControlUI: SSE Event: needs_approval
            ControlUI->>User: Display Approval Modal
            User->>ControlUI: Click Approve / Reject
            ControlUI->>FastAPI: POST /api/agent/approve
            FastAPI->>Redis: Set state approved
        end
        FastAPI->>CDP: Execute Tool Action
        CDP->>ERP: Interact with ERP UI
        ERP-->>CDP: Response / DOM state
        FastAPI->>DB: Persist step result & base64 screenshot
        FastAPI->>ControlUI: SSE Event: step result
    end
    FastAPI->>DB: Final state verification & verification report
    FastAPI->>ControlUI: SSE Event: done (Summary & Verification Table)
```

---

## Local Setup & Running

### Prerequisites
- Node.js v18+ and `npm`
- Python 3.12+ and `pip`
- Accounts for Neon Postgres, Upstash Redis, Groq, and Browserless/Steel.dev (or local Playwright CDP instance).

### 1. Clone Repository & Install Dependencies

```bash
git clone https://github.com/kaiizer777/hulchul-assignment.git
cd hulchul-assignment

# Install Frontend dependencies
cd frontend
npm install
cd ..

# Install Backend dependencies
cd backend
python -m venv .venv
# Activate virtual environment:
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` in the root (or configure individual environment variables):

```env
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
UPSTASH_REDIS_REST_URL=https://your-redis.upstash.io
UPSTASH_REDIS_REST_TOKEN=your_redis_token
BROWSER_WS_ENDPOINT=wss://chrome.browserless.io?token=your_token
NEXT_PUBLIC_API_URL=http://localhost:3051
SIMULATE_FAILURE_AFTER=
```

### 3. Database Schema Setup & Seeding

```bash
cd backend
python init_db.py
python seed_db.py
cd ..
```

### 4. Running Locally

- **Start Mock ERP & Control UI (Frontend)**:
  ```bash
  cd frontend
  npm run dev
  ```
  Runs on `http://localhost:3051`.

- **Start FastAPI Orchestrator (Backend)**:
  ```bash
  cd backend
  uvicorn main:app --reload --port 8051
  ```
  Runs on `http://localhost:8051` (API docs at `http://localhost:8051/docs`).

---

## Environment Variables Reference

| Variable | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `GROQ_API_KEY` | Groq API key for LLM ReAct loop execution (`openai/gpt-oss-120b`) | Yes | None |
| `DATABASE_URL` | Neon Serverless PostgreSQL connection string | Yes | None |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST URL for session and approval gates | Yes | None |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST token | Yes | None |
| `BROWSER_WS_ENDPOINT` | WebSocket CDP endpoint for remote browser (Browserless/Steel.dev) | Yes | None |
| `NEXT_PUBLIC_API_URL` | ERP frontend base URL used for backend navigation | Yes | `http://localhost:3051` |
| `NEXT_PUBLIC_BACKEND_URL` | Backend base URL inlined into the **client bundle at build time**. Must be set when building for Cloudflare. A `[vars]` entry of the same name in `wrangler.toml` is a runtime Worker binding and is **not** visible to the browser | Yes (for Cloudflare builds) | Production Lambda URL via `frontend/.env.production` |
| `SIMULATE_FAILURE_AFTER` | Simulates ERP 500 error after N invoice submissions for recovery testing | No | Empty (Disabled) |

---

## Deployment

### Frontend (Cloudflare Worker)

```bash
cd frontend
npm run deploy:cloudflare
```

This builds the OpenNext bundle, asserts that `NEXT_PUBLIC_BACKEND_URL` was actually
inlined into a client chunk (`npm run verify:cloudflare`), and only then deploys with
Wrangler. Run the steps separately while iterating:

```bash
npm run build:cloudflare
npm run verify:cloudflare
npm run verify:secrets
npx wrangler deploy
```

Both `DATABASE_URL` (Neon) and `UPSTASH_REDIS_REST_TOKEN` (Upstash Redis) must be
provisioned on the Worker **before** deploying, because both are read from
`process.env` at request time and a missing one is not a build error — it only shows
up as `/api/*` routes failing once the deploy is live. `npm run deploy:cloudflare`
now runs `npm run verify:secrets` between the bundle check and the upload and refuses
to deploy if either secret is absent or unverifiable. Provision them out of band:

```bash
cd frontend
npx wrangler secret put DATABASE_URL
npx wrangler secret put UPSTASH_REDIS_REST_TOKEN
```

`DATABASE_URL` must be a Worker **secret**, not a `[vars]` entry in `wrangler.toml`:
it embeds the Neon account password, and `wrangler.toml` is committed to git. The
check reads secret *names* only and never values.

### Backend (AWS Lambda via ECR)

```powershell
cd infra/aws
.\deploy.ps1
```

The Docker build uses the **repository root** as its context so that the `backend`
package layout is preserved inside the image.

#### Manual gate: container image import check

CI cannot run Docker, so the image layout is asserted statically in
`backend/tests/test_phase7_deploy.py`. After changing `backend/Dockerfile` or the build
context, run the behavioural check by hand before deploying:

```bash
docker build -t hulchul-backend:check -f backend/Dockerfile .
docker run --rm --entrypoint python hulchul-backend:check -c "import backend.main"
```

A failure here means `from backend.config import settings` cannot resolve inside the
image, and every Lambda endpoint will return `Extension.Crash`.

---

## Test Execution

### Frontend Tests
```bash
cd frontend
npm test
```

### Backend Tests
```bash
cd backend
python -m pytest backend/tests/
```
