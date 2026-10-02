# Engineering Note: Autonomous Browser Agent & ERP Reconciliation System

---

## 1. Architecture Decisions & Rationale

### Separation of Control UI + Mock ERP in Next.js
- **Rationale**: Combining the mock ERP application pages (`/invoices`, `/purchase-orders`, `/vendors`, `/invoices/new`) with the Control UI (`/agent`) in a single Next.js 16 App Router application provides a closed-loop testing environment. The agent operates real HTTP-rendered DOM views in a browser, exactly mirroring enterprise SaaS operations where automation agents interact with third-party web apps without privileged API access.
- **Trade-off**: Requires maintaining both ERP domain routes and agent execution telemetry routes in the same codebase. Mitigated by strict separation of concerns between UI route components and backend agent orchestrators.

### Stateless FastAPI Backend
- **Rationale**: Deploying FastAPI on AWS Lambda via the AWS Lambda Web Adapter ensures infinite horizontal scaling and zero idle infrastructure cost. Because serverless containers are ephemeral and recycled unpredictably, storing execution state in-memory is prohibited.
- **State Management**: All execution history, step audit trails, and screenshots are persisted durably to **Neon Serverless PostgreSQL**. Active session flags, pause states, and approval nonces are held in **Upstash Redis** for sub-millisecond atomic checks.

### Remote CDP over Local Browser Binary
- **Rationale**: Running headless Chromium inside an AWS Lambda container requires packaging a ~500MB browser binary, shared libraries (`libnss3`, `libatk`, etc.), and massive memory footprints (2GB+ RAM), causing severe cold starts (8–15 seconds). By utilizing **Remote CDP (Browserless / Steel.dev)**, the FastAPI container remains a slim ~80MB Python image. Browser execution happens in dedicated containerized instances, connecting instantly over WebSocket.

### Upstash Redis for Pause/Resume and Approval Gates
- **Rationale**: Human-in-the-approval loops require asynchronous pause and resume capabilities. When an invoice exceeds the approval threshold (e.g., >₹50,000), the agent halts execution, writes an `awaiting_approval` state to Upstash Redis, and emits a Server-Sent Event (SSE). When the operator clicks "Approve" in the Control UI, a FastAPI endpoint updates Redis and releases the execution lock, allowing the ReAct loop to resume instantly without losing context.

---

## 2. Remote CDP vs. Local Browser Trade-offs

| Dimension | Remote CDP (Browserless / Steel.dev) | Local Browser Binary (Puppeteer/Playwright bundled) |
| :--- | :--- | :--- |
| **Container Image Size** | Slim (~80MB Python FastAPI container) | Bloated (800MB–1.5GB with Chromium and OS dependencies) |
| **Cold Start Latency** | Sub-second Lambda cold start | 8–15 seconds cold start due to binary extraction and sandbox setup |
| **Memory Footprint** | Minimal (~120MB RAM on FastAPI container) | High (Requires 1.5GB–2GB RAM allocation per Lambda instance) |
| **Network Latency** | WebSocket round-trip to remote browser provider (~20–50ms) | Local loopback (`127.0.0.1`), near-zero latency |
| **Multi-Tenant Isolation** | Complete tenant browser isolation (ephemeral browser containers) | Shared container filesystem / process space risks |
| **Operational Dependency** | Relies on third-party SaaS availability (Browserless/Steel.dev) | Self-contained, but susceptible to missing shared libraries in Linux distros |

---

## 3. Where the Agent Struggles & Observed Failure Modes

### Form Locator Ambiguity on Dynamic DOMs
- **Challenge**: Modern React/Tailwind applications render dynamic DOM structures where element IDs or class names can shift or share semantic labels (e.g., multiple submit buttons or placeholder text).
- **Mitigation**: The agent relies on accessibility tree snapshots (`read_page()`) rather than raw CSS selectors, querying elements by accessible roles and ARIA labels. However, ambiguous forms occasionally cause the LLM to select incorrect input fields.

### Rate-Limiting on LLM Function Calls
- **Challenge**: Complex multi-step ReAct loops can fire 15–20 sequential Groq API requests within a short timeframe, hitting API rate limits (`429 Too Many Requests`).
- **Mitigation**: Implemented exponential backoff with jitter on LLM tool dispatch and prompt batching to minimize round trips.

### Handling Unexpected Modal Popups
- **Challenge**: Unhandled cookie banners, validation error tooltips, or session timeout alerts interrupt the expected accessibility tree hierarchy.
- **Mitigation**: Prompt engineering instructs the agent to inspect error messages in the DOM and take corrective action (e.g., clicking dismiss or re-filling invalid fields), though unexpected overlays can occasionally stall the run.

### Recovery Checkpointing
- **Challenge**: When a network timeout or ERP 500 error occurs mid-run, restarting from step 1 would duplicate submitted invoices.
- **Mitigation**: Enforced strict idempotency (`check_exists` before create) combined with Neon audit logging. The recovery routine queries the last successful step from Neon and resumes from `step_index + 1`.

---

## 4. Production Readiness Gaps

To transition from an engineering assignment to a production-grade enterprise deployment, the following gaps must be addressed:

1. **Enterprise Auth & RBAC**: Integrate OIDC / SAML (Auth0, Okta) with role-based access control (RBAC) across both the Control UI and FastAPI endpoints.
2. **Strict Tenant Data Isolation**: Enforce PostgreSQL Row-Level Security (RLS) with session-scoped tenant claims (`SET LOCAL app.current_tenant_id`) across all queries to prevent cross-tenant data leaks.
3. **Encrypted Credential Vaults**: Store ERP login credentials and API keys in AWS Secrets Manager or HashiCorp Vault rather than environment variables.
4. **Distributed Task Queues**: Replace synchronous FastAPI loop blocking with a distributed worker queue (Celery + Redis or Temporal) for long-running browser sessions exceeding serverless timeout limits.
5. **Dead-Letter Queues (DLQ)**: Route persistently failing agent runs or unrecoverable ERP exceptions to a DLQ for manual site-reliability engineering (SRE) review and alerting.

---

## 5. AI Assistance Attribution

This project was built under strict engineering supervision with a clear demarcation of responsibilities:

- **Architectural Leadership & System Design**: 100% human-directed. Defining the Next.js + FastAPI decoupled architecture, serverless CDP strategy, Upstash Redis approval gate state machine, and the rigorous zero-downtime recovery protocol.
- **Code Generation & Implementation**: Assisted by AI agents for boilerplate generation, TypeScript/Python type annotations, Playwright script scaffolding, and unit test structuring, followed by rigorous staff-engineer manual review and security hardening.
