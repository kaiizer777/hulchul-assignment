# WORK.md — Hulchul AI Engineering Assignment
## Invoice Processing Browser Agent

> **Stack:** FastAPI (Python) + Next.js (TypeScript) + Playwright (accessibility tree) + Groq (`openai/gpt-oss-120b`) + Neon (Postgres) + Upstash Redis + AWS Lambda (Web Adapter) + Cloudflare Workers
> **Workflow:** Agent receives a plain-English goal → operates the mock ERP in a real browser → produces verified evidence of completion.

---

## Architecture Overview

```
Goal (plain English)
    ↓
FastAPI Agent (ReAct loop — Observe → Think → Act)
    ↓
Playwright over CDP → Browserless/Steel.dev (remote browser)
    ↓
Mock Next.js ERP (invoices, POs, vendors)
    ↓
Neon Postgres (durable step records + base64 screenshots) + Upstash Redis (active session state)
    ↓
Verification Report (pass/fail table + evidence)
    ↓
Control UI (SSE live log, Pause, Approval modal)
```

---

## Phase 1 — Mock ERP (Target App) `Difficulty: Low` `Complexity: Medium`

- [x] **1.1** Scaffold Next.js app in `/frontend` with App Router, TypeScript, Tailwind
- [x] **1.2** Create `/invoices` page — table listing all invoices with status badges
- [x] **1.3** Create `/invoices/new` page — form with fields: vendor, amount, date, PO number, status
- [x] **1.4** Create `/purchase-orders` page — PO table with PO number, vendor, approved amount
- [x] **1.5** Create `/vendors` page — vendor list with IDs and names
- [x] **1.6** Set up Neon Postgres schema:
  - `invoices` table (id, vendor, amount, date, po_number, status, created_at)
  - `purchase_orders` table (po_number, vendor, approved_amount, status)
  - `agent_runs` table (run_id, goal, status, created_at)
  - `agent_steps` table (step_id, run_id, action, result, screenshot_b64, timestamp)
- [x] **1.7** Seed synthetic data — 10 invoices across these cases: 4 valid (PO exists, amount matches), 2 mismatched (amount differs from PO approved amount by >10%), 2 missing PO (po_number not found in purchase_orders), 2 over threshold (amount >₹50,000 requiring approval). Seed 8 POs covering the valid and mismatched invoices only — leave the missing-PO invoices uncovered intentionally. All data is synthetic (no real vendor names or bank details).
- [x] **1.8** Expose Next.js API routes for CRUD on invoices and POs (agent will use browser, not these directly — but needed for verification)

### Review 1
- [x] All pages render correctly with seed data
- [x] Forms submit and persist to Neon correctly
- [x] No broken links or missing fields

### Tests 1
- [x] Unit test: each API route returns correct shape
- [x] UI smoke test: `/invoices`, `/invoices/new`, `/purchase-orders` load without errors

---

## Phase 2 — Agent Core (ReAct Loop) `Difficulty: High` `Complexity: High`

- [x] **2.1** Set up FastAPI in `/backend` with `uvicorn`, `python-dotenv`, `asyncpg`
- [x] **2.2** Connect to **Browserless/Steel.dev** over CDP — do NOT run Playwright locally on Lambda. Use `playwright.connect_over_cdp(BROWSER_WS_ENDPOINT)` where `BROWSER_WS_ENDPOINT` is the WebSocket URL from your Browserless/Steel.dev account. This keeps Lambda's container small (no browser binary) and offloads browser lifecycle management to the remote service. The CDP connection must be re-established on each Lambda invocation since Lambda is stateless.
- [x] **2.3** Build Playwright tool set (each is a function the LLM can call):
  - `navigate(url)` — go to a URL
  - `read_page()` — dump accessibility tree snapshot (~2–5 KB)
  - `click(selector)` — click an element by accessibility label
  - `fill(selector, value)` — fill a form field
  - `select(selector, value)` — select a dropdown option
  - `take_screenshot()` — capture buffer → store as base64 in Neon `agent_steps` (only on failure or decision point)
  - `check_exists(entity_type, identifier)` — idempotency check before any create

- [x] **2.4** Build ReAct loop:
  - Build a system prompt that defines the agent's role, the available tools, the approval threshold (extracted from the goal), and the rule that it must call `check_exists` before any create action
  - On each iteration: call `read_page()` to get the current accessibility tree snapshot, then send `[system prompt + goal + full conversation history + current snapshot]` to Groq (`openai/gpt-oss-120b`) with tool definitions
  - Parse the tool call from the response — if no tool call is returned and the model says `done`, exit the loop
  - Execute the chosen tool, capture the result, append `{ tool_call, tool_result }` to conversation history
  - On `needs_approval`: emit SSE event, write approval-pending state to Upstash Redis, pause the loop — do not exit
  - Hard cap at 30 iterations per run to prevent infinite loops; mark run as `stalled` if cap is hit
- [x] **2.5** Implement idempotency — before every `fill + submit`, call `check_exists` first; abort if already present
- [x] **2.6** Persist every step to `agent_steps` table in Neon (action, result, screenshot_b64, timestamp)
- [x] **2.7** Implement approval gate — when the agent detects an invoice amount exceeding the threshold (extracted from the goal, default ₹50,000), it must NOT submit the form. Instead: emit a `needs_approval` SSE event containing `{ invoice_id, vendor, amount, po_number }`, write `{ run_id, status: "awaiting_approval", invoice_id }` to Upstash Redis, and pause the loop by polling Redis every 2 seconds for an `approved` or `rejected` flag. On `approved`: proceed to submit. On `rejected`: mark the invoice as `skipped` in Neon and move to the next invoice.
- [x] **2.8** Implement recovery — wrap every tool execution in a try/except. On any failure (network error, ERP 500, CDP session drop, timeout): log the error to `agent_steps` with `result: "failed"`, take a screenshot, emit a `step_failed` SSE event, then attempt resume. Resume logic: query Neon for the last `agent_steps` row with `result: "success"` for this `run_id`, extract the step index, and restart the loop from `step_index + 1`. Never re-navigate to or re-submit an invoice that already has a `completed` or `skipped` status in the ERP — the `check_exists` call guards this.
- [x] **2.9** Store active session state (current step index, run_id, pause flag) in Upstash Redis

### Review 2
- [ ] ReAct loop correctly selects tools given accessibility tree input
- [x] Idempotency check fires before every create action
- [x] Approval gate pauses execution and waits for human
- [x] Recovery correctly resumes from last persisted step, not from the beginning
- [x] All steps persisted to Neon with correct timestamps

### Tests 2
- [x] Unit test: each tool function (navigate, fill, click, check_exists) returns correct shape
- [x] Unit test: ReAct loop parses Groq tool call response correctly
- [x] Unit test: idempotency check returns `exists: true` for duplicate, `exists: false` for new
- [x] Integration test: agent completes a 3-step mock task end to end


---

## Phase 3 — SSE Streaming + Control UI `Difficulty: Medium` `Complexity: High`

- [x] **3.1** Add SSE endpoint in FastAPI using `sse_starlette.EventSourceResponse`
  - Stream step events: `{ type: "step", action, result, timestamp }`
  - Stream approval request: `{ type: "needs_approval", invoice_id, amount }`
  - Stream completion: `{ type: "done", summary }`
  - Pad each event with SSE comment block on Lambda to force buffer flush
- [ ] **3.2** Deploy FastAPI via **AWS Lambda Web Adapter** (NOT Mangum — no streaming path)
- [x] **3.3** Build Control UI in Next.js (`/agent` page):
  - Goal input textarea
  - Start button
  - Live step log (consume SSE via EventSource)
  - Pause / Resume button (writes pause flag to Upstash Redis)
  - Approval modal — shows invoice details, Approve / Reject buttons
- [x] **3.4** Wire Approve/Reject to a FastAPI endpoint that updates Upstash Redis and resumes the agent loop

### Review 3
- [ ] SSE events arrive in real time in the browser (no bulk flush at end)
- [ ] Pause button actually stops agent between steps (not mid-action)
- [ ] Approval modal shows correct invoice details
- [ ] Approve resumes agent, Reject marks invoice as skipped in Neon

### Tests 3
- [ ] SSE endpoint streams events correctly under simulated slow network
- [ ] Pause flag in Upstash correctly halts the loop
- [ ] Approval flow end to end: modal appears → user approves → agent continues

---

## Phase 4 — Verification + Evidence `Difficulty: Low` `Complexity: Low`

- [x] **4.1** After agent completes, query Neon for final invoice states
- [x] **4.2** Compare actual state vs expected state (from seed data definition)
- [x] **4.3** Generate pass/fail verification table:
  - Invoice ID | Expected Status | Actual Status | Match (✅/❌)
- [x] **4.4** List any incomplete items clearly with reason (flagged, awaiting approval, failed)
- [x] **4.5** Render verification report on `/agent` page below the step log
- [x] **4.6** Screenshots (base64 from Neon) rendered inline in each failed step row in the report

### Review 4
- [x] Verification catches both successes and failures correctly
- [x] Incomplete items are visible, not hidden
- [x] Screenshots render correctly inline from Neon base64 data

### Tests 4
- [x] Unit test: verification function correctly diffs expected vs actual state
- [x] Test with a deliberately wrong invoice — verify it shows ❌ not ✅

---

## Phase 5 — Adaptability (No Code Change) `Difficulty: Low` `Complexity: Low`

- [x] **5.1** Agent reads goal from plain-English input — no hardcoded vendor/amount/filter
- [x] **5.2** Test variation 1: `"Process only invoices from Vendor Acme"` — agent filters correctly
- [x] **5.3** Test variation 2: `"Hold anything over ₹25,000 for approval"` — threshold changes from goal, not config
- [x] **5.4** Test variation 3: Different invoice CSV/seed — agent handles new data without changes

### Review 5
- [x] All three variations work with zero code changes
- [x] LLM correctly extracts vendor filter and threshold from plain-English goal

---

## Phase 6 — Failure & Recovery Demo `Difficulty: High` `Complexity: Medium`

- [x] **6.1** Inject a simulated 500 error from the ERP after the 3rd invoice is processed — add a `?fail_after=3` query param or a `SIMULATE_FAILURE_AFTER` env var that the Next.js ERP API route reads and returns a 500 on the Nth invoice submission. This keeps failure injection in the test environment without touching agent code.
- [x] **6.2** Verify agent logs the failure, takes a screenshot, persists the failed step to Neon
- [x] **6.3** Verify agent does NOT retry completed steps (idempotency)
- [x] **6.4** Resume agent — verify it picks up from step 4, not step 1
- [x] **6.5** Inject a session drop (kill browser connection mid-run) — verify agent reconnects via CDP and resumes
- [x] **6.6** Verify no duplicate invoice entries in ERP after recovery

### Review 6
- [x] Recovery resumes from correct step every time
- [x] No duplicates in ERP after any failure scenario
- [x] Failure is clearly visible in step log and verification report

### Tests 6
- [x] Test: inject 500 on step 3 → resume → verify steps 1-3 not re-executed
- [x] Test: verify ERP invoice count is correct after recovery (no duplicates)


---

## Phase 7 — Deploy

- [x] **7.1** Deploy Next.js (ERP + Control UI) to **Cloudflare Workers** via `@opennextjs/cloudflare`
- [x] **7.2** Deploy FastAPI to **AWS Lambda** via **AWS Lambda Web Adapter** (container image — NOT ZIP layer)
- [x] **7.3** Use a slim Python base image (`python:3.12-slim`, always latest stable 3.x) — no browser binary needed since Playwright connects to Browserless/Steel.dev over CDP
- [x] **7.4** Set all secrets in `.env` (never committed) — provide `.env.example` with all keys listed
- [x] **7.5** Configure CORS on FastAPI for Cloudflare Workers domain
- [ ] **7.6** Verify SSE streaming works end to end on deployed infra (not just local)
- [ ] **7.7** Test all three variations and failure case on deployed URLs

### Review 7
- [x] Live URL works end to end
- [x] SSE streams correctly on Lambda (no bulk flush)
- [x] `.env.example` covers every required key
- [x] No credentials anywhere in the repo

---

## Phase 8 — Polish & Submission

- [ ] **8.1** Write `README.md` with: project overview, setup steps, how to run locally, env vars, deployed URLs
- [ ] **8.2** Write `ENGINEERING_NOTE.md`:
  - Why this workflow
  - Personal contribution vs AI assistance (be specific)
  - Key technical decisions and tradeoffs
  - Known limitations
  - What I'd build next
- [ ] **8.3** Record demo video (max 5 min):
  - 0:00–1:30 — Normal run (full invoice processing)
  - 1:30–2:30 — Variation (different goal, no code change)
  - 2:30–4:00 — Failure injection + recovery
  - 4:00–5:00 — Verification report + evidence
- [ ] **8.4** Upload video to YouTube (unlisted) or Loom
- [ ] **8.5** Push final code to GitHub (public repo)
- [ ] **8.6** Reply on Internshala with: repo link + video link + start date confirmation + 30hr/week confirmation

### Review 8
- [ ] Video is under 5 minutes and covers all 4 required moments
- [ ] README lets someone run the project from scratch
- [ ] Engineering note is honest about AI assistance
- [ ] Repo is clean, no secrets, has `.env.example`

---

## Required Env Vars (`.env.example`)

```env
# Groq
GROQ_API_KEY=

# Neon Postgres
DATABASE_URL=

# Upstash Redis
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=

# Remote Browser (Browserless or Steel.dev)
BROWSER_WS_ENDPOINT=

# Next.js
NEXT_PUBLIC_API_URL=

# Failure Injection (set to a number to simulate ERP 500 after N invoice submissions; leave empty in production)
SIMULATE_FAILURE_AFTER=
```

---

## Model Requirements

- Groq account with access to `openai/gpt-oss-120b`
- Browserless or Steel.dev account (free tier sufficient for demo)
- AWS account (Lambda only)
- Neon account (free tier: 512 MB)
- Upstash account (free tier sufficient)
- Cloudflare account (free tier sufficient)