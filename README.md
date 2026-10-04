# Autonomous Browser Agent for ERP Operations & Invoice Processing

[![Frontend](https://img.shields.io/badge/Frontend-Next.js%2016%20%7C%20Zustand-black?style=flat-square&logo=nextdotjs)](https://nextjs.org/)
[![Cloudflare Workers](https://img.shields.io/badge/Edge-Cloudflare%20Workers-F38020?style=flat-square&logo=cloudflare)](https://workers.cloudflare.com/)
[![Backend](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python%203.12-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![AWS Lambda](https://img.shields.io/badge/Backend-AWS%20Lambda%20%7C%20Terraform-FF9900?style=flat-square&logo=awslambda)](https://aws.amazon.com/lambda/)
[![Browser](https://img.shields.io/badge/Browser-Playwright%20Remote%20CDP-2EAD33?style=flat-square&logo=playwright)](https://playwright.dev/)
[![Database](https://img.shields.io/badge/Database-Neon%20Postgres%20%7C%20Upstash%20Redis-005F46?style=flat-square&labelColor=1F2937&logo=postgresql)](https://neon.tech/)
[![LLM](https://img.shields.io/badge/LLM-Groq%20(GPT--OSS--120B)-F55036?style=flat-square)](https://groq.com/)

A production-grade, resilient autonomous AI browser agent engineered for enterprise ERP workflows, invoice reconciliation, and exception handling with human-in-the-loop (HITL) approval gates.

> 📄 **Detailed Engineering Submission**: For deep-dive architectural trade-offs, state machine internals, recovery mechanics, and enterprise roadmap, see [**ENGINEERING_NOTE.md**](ENGINEERING_NOTE.md).

---

## 🎯 5 Core Assignment Pillars

| Pillar | Implementation | Highlight |
| :--- | :--- | :--- |
| **1. Working Execution** | Autonomous multi-page navigation across `/invoices`, `/purchase-orders`, `/vendors`, and `/invoices/new` | Reconciles line items, extracts PO numbers, handles form inputs, and records audit logs |
| **2. Dynamic Adaptability** | Zero-code prompt interpretation for custom vendor filters & dynamic approval thresholds | E.g. *"Hold anything over ₹25,000 for approval"* dynamically reconfigures execution boundaries |
| **3. Recovery & Idempotency** | Pre-mutation `check_exists` guards, session re-attachment, and crash resumption | Safely resumes from last successful step (`step_index + 1`) on simulated 500s or network drops |
| **4. Verified Completion** | Deterministic post-execution SQL verification engine | Compares ERP database mutations against ground truth with cryptographic pass/fail receipts |
| **5. Human-in-the-Loop** | Non-blocking pause, SSE event broadcasting, and real-time approval modals | High-value invoices trigger approval gates resolved via atomic Upstash Redis nonces |

---

## 🏗️ Architecture & Telemetry Flow

```mermaid
sequenceDiagram
    participant User as Operator (Control UI)
    participant Store as Zustand Store (Client)
    participant API as FastAPI (AWS Lambda)
    participant Redis as Upstash Redis (Lease/Gate)
    participant Groq as Groq (GPT-OSS-120B)
    participant CDP as Remote CDP (Playwright)
    participant ERP as Mock ERP (/invoices)
    participant DB as Neon PostgreSQL

    User->>Store: Select Preset / Enter Custom Goal
    Store->>API: POST /api/agent/run
    API->>DB: Initialize Run & Acquire Distributed Lease
    API-->>Store: Return run_id & Open SSE Stream
    loop ReAct Autonomous Loop
        API->>CDP: read_page() (Compact A11y Tree 2-5KB)
        CDP->>ERP: Inspect DOM Elements
        API->>Groq: Prompt (Goal + Step History + A11y Snapshot)
        Groq-->>API: Tool Action (navigate, fill, click, check_exists)
        alt Threshold Exceeded (>₹25k or custom)
            API->>Redis: Set state = awaiting_approval + nonce
            API-->>Store: SSE Event: needs_approval
            Store->>User: Render Interactive Approval Modal
            User->>Store: Click Approve / Reject
            Store->>API: POST /api/agent/runs/:id/approval
            API->>Redis: Resolve nonce & resume loop
        end
        API->>CDP: Execute Tool Action on ERP
        API->>DB: Persist Step Record & Base64 Screenshot
        API-->>Store: SSE Event: step_complete / step_failed
        Store->>Store: Deduplicate & Upsert Step (Idempotent)
    end
    API->>DB: Run Deterministic Verification Sweep
    API-->>Store: SSE Event: done + Verification Report
```

---

## ⚡ Frontend State Architecture (Zustand)

The control workstation (`/agent`) is powered by a centralized **Zustand** store ([`useAgentStore.ts`](frontend/src/app/store/useAgentStore.ts)) engineered for real-time telemetry and zero-latency UI reactivity:

- **Single Source of Truth**: Centralizes agent execution status, SSE connection states (`disconnected`, `connecting`, `connected`, `reconnecting`, `closed`), step timeline, verification reports, approval nonces, and execution metrics.
- **`persist` Middleware & URL Synchronization**: Persists active goals, preset selections (`all_pending`, `vendor_acme`, `approval_threshold`), and auto-scroll preferences in `localStorage` across page refreshes, automatically synchronizing with URL query parameters (`?preset=vendor_acme`).
- **Idempotent Step Deduplication**: `upsertStepEvent` matches incoming telemetry by unique `step_id` or `step_index`, seamlessly merging incremental results, errors, and screenshots without timeline jitter or duplicate entries.
- **Zero Stale Closures in SSE Streams**: Stream listeners and approval fallback polling access state dynamically via Zustand's `get()`/`set()` references, eliminating stale closure bugs during rapid event bursts.
- **Defensive Connection Lifecycle**: Auto-cleans active streams, aborts pending fetch signals via `AbortController`, and recovers from transient connection drops with automatic exponential backoff.

---

## 🚀 Quick Start & Local Setup

### Prerequisites
- Node.js v18+ & Python 3.12+
- Neon PostgreSQL, Upstash Redis, Groq API key, and Remote CDP endpoint (Browserless or local Chromium)

### 1. Clone & Install

```bash
git clone https://github.com/kaiizer777/hulchul-assignment.git
cd hulchul-assignment

# Frontend dependencies
cd frontend && npm install && cd ..

# Backend dependencies & virtualenv
cd backend
python -m venv .venv
# Activate: .\.venv\Scripts\Activate.ps1 (Win) or source .venv/bin/activate (Linux/Mac)
pip install -r requirements.txt
cd ..
```

### 2. Environment Configuration
Copy `.env.example` to `.env` in the root:

```env
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
UPSTASH_REDIS_REST_URL=https://your-redis.upstash.io
UPSTASH_REDIS_REST_TOKEN=your_redis_token
BROWSER_WS_ENDPOINT=wss://chrome.browserless.io?token=your_token
NEXT_PUBLIC_API_URL=http://localhost:3051
```

### 3. Initialize Database & Seed
```bash
cd backend
python init_db.py
python seed_db.py
cd ..
```

### 4. Run Locally
```bash
# Terminal 1: Next.js Frontend & Mock ERP (Port 3051)
cd frontend && npm run dev

# Terminal 2: FastAPI Orchestrator (Port 8051)
cd backend && uvicorn main:app --reload --port 8051
```

- Control UI: `http://localhost:3051/agent`
- Mock ERP: `http://localhost:3051/invoices`
- API Docs: `http://localhost:8051/docs`

---

## 🧪 Testing

```bash
# Frontend Unit & Store Tests
cd frontend && npm test

# Backend Integration & Agent Tests
cd backend && python -m pytest backend/tests/
```

---

## 📦 Deployment Overview

- **Frontend (Cloudflare Workers)**: Built via `@opennextjs/cloudflare` with static asset bundling and Edge API routes (`npm run deploy:cloudflare`).
- **Backend (AWS Lambda via ECR)**: Packaged as a lightweight container (~80MB) using the AWS Lambda Web Adapter (`infra/aws/deploy.ps1`).
