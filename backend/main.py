import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, Set
from fastapi import FastAPI, Depends, status, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
import asyncpg
from sse_starlette.sse import EventSourceResponse

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, check_db_health, get_db_connection, get_db_pool
from backend.browser import verify_cdp_connection
from backend.verification import VerificationReport, generate_verification_report


class RunEventHub:
    """
    Manages active per-run asynchronous event queues for real-time Server-Sent Events (SSE) streaming.
    Provides publish, subscribe, and unsubscribe operations for agent runs.
    """

    def __init__(self) -> None:
        """Initialize the run event hub subscriber registry dictionary."""
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """Subscribe and return a new asynchronous event queue for the specified run ID."""
        q = asyncio.Queue()
        if run_id not in self._subscribers:
            self._subscribers[run_id] = set()
        self._subscribers[run_id].add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        """Unsubscribe and discard an asynchronous event queue from the specified run ID."""
        if run_id in self._subscribers:
            self._subscribers[run_id].discard(q)
            if not self._subscribers[run_id]:
                del self._subscribers[run_id]

    async def publish(self, run_id: str, event: Dict[str, Any]) -> None:
        """Publish an event dictionary to all active subscriber queues for the specified run ID."""
        if run_id in self._subscribers:
            for q in list(self._subscribers[run_id]):
                try:
                    await q.put(event)
                except Exception:
                    pass


run_event_hub = RunEventHub()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("hulchul.backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle (e.g. database pools)."""
    logger.info("FastAPI starting up: initializing asyncpg database pool...")
    try:
        await init_db_pool()
    except Exception as e:
        logger.error(f"Failed to initialize database pool on startup: {e}")
        # Allow app to start even if DB is momentarily unreachable, health checks will report degraded
    yield
    logger.info("FastAPI shutting down: closing asyncpg database pool...")
    await close_db_pool()


app = FastAPI(
    title="Hulchul Agent Core Backend",
    version="0.2.0",
    description="FastAPI ReAct Agent Backend with Remote CDP Browser Automation",
    lifespan=lifespan,
)

# CORS configuration for Next.js frontend
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
if settings.NEXT_PUBLIC_API_URL and settings.NEXT_PUBLIC_API_URL not in allowed_origins:
    allowed_origins.append(settings.NEXT_PUBLIC_API_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Response Schemas (Strict Pydantic v2 Models)
class HealthResponse(BaseModel):
    """Response model for system health checks."""
    model_config = ConfigDict(extra="forbid")
    status: str
    database: str
    version: str


class BrowserHealthResponse(BaseModel):
    """Response model for remote CDP browser diagnostic checks."""
    model_config = ConfigDict(extra="forbid")
    connected: bool
    browser_type: Optional[str] = None
    blank_page_title: Optional[str] = None
    latency_ms: float
    endpoint_configured: bool
    error: Optional[str] = None


class RootResponse(BaseModel):
    """Response model for API root endpoint."""
    model_config = ConfigDict(extra="forbid")
    message: str
    version: str
    phase: str


class CheckExistsRequest(BaseModel):
    """Request model for entity presence / idempotency checks."""
    model_config = ConfigDict(extra="forbid")
    entity_type: str
    identifier: str


class CheckExistsResponse(BaseModel):
    """Response model for entity presence / idempotency checks."""
    model_config = ConfigDict(extra="forbid")
    exists: bool
    entity_type: str
    identifier: str
    record: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ToolsResponse(BaseModel):
    """Response model listing available agent tools."""
    model_config = ConfigDict(extra="forbid")
    count: int
    tools: list[Dict[str, Any]]


@app.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    """Root API endpoint returning basic service status."""
    return RootResponse(
        message="Hulchul Backend API is running",
        version="0.2.0",
        phase="Phase 2.1 - 2.6 Complete",
    )


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """System health check endpoint verifying database connectivity."""
    db_ok = await check_db_health()
    db_status = "connected" if db_ok else "unreachable"
    overall_status = "healthy" if db_ok else "degraded"
    return HealthResponse(
        status=overall_status,
        database=db_status,
        version="0.2.0",
    )


@app.get("/health/browser", response_model=BrowserHealthResponse)
async def browser_health_check() -> BrowserHealthResponse:
    """Diagnostic probe verifying CDP connection to remote Browserless/Steel.dev."""
    result = await verify_cdp_connection()
    return BrowserHealthResponse(
        connected=result.get("connected", False),
        browser_type=result.get("browser_type"),
        blank_page_title=result.get("blank_page_title"),
        latency_ms=result.get("latency_ms", 0.0),
        endpoint_configured=result.get("endpoint_configured", False),
        error=result.get("error"),
    )


@app.get("/health/redis")
async def redis_health_check() -> Dict[str, Any]:
    """Diagnostic probe verifying connectivity to Upstash Redis."""
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    is_ok = await redis.ping()
    return {
        "connected": is_ok,
        "configured": redis.is_configured,
        "status": "healthy" if is_ok else "degraded",
    }


@app.get("/tools", response_model=ToolsResponse)
async def list_tools() -> ToolsResponse:
    """List available LLM agent tool definitions with parameters and schemas."""
    from backend.tools import TOOL_DEFINITIONS
    return ToolsResponse(
        count=len(TOOL_DEFINITIONS),
        tools=TOOL_DEFINITIONS,
    )


@app.post("/tools/check-exists", response_model=CheckExistsResponse)
async def check_exists_endpoint(payload: CheckExistsRequest) -> CheckExistsResponse:
    """Idempotency check endpoint verifying entity presence in Neon database."""
    from backend.tools import check_exists
    res = await check_exists(entity_type=payload.entity_type, identifier=payload.identifier)
    return CheckExistsResponse(
        exists=res.get("exists", False),
        entity_type=res.get("entity_type", payload.entity_type),
        identifier=res.get("identifier", payload.identifier),
        record=res.get("record"),
        error=res.get("error"),
    )


# ---------------------------------------------------------------------------
# Phase 2.4 Agent Execution & Orchestration Schemas & Endpoints
# ---------------------------------------------------------------------------

class AgentRunRequest(BaseModel):
    """Request model to initiate an agent run with goal and optional run ID."""
    model_config = ConfigDict(extra="forbid")
    goal: str
    run_id: Optional[uuid.UUID] = None


class AgentRunResponse(BaseModel):
    """Response model detailing agent execution results."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: str
    iterations: int
    goal: str
    threshold: float
    summary: Optional[str] = None


class PauseResumeResponse(BaseModel):
    """Response model for agent pause and resume actions."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    paused: bool
    message: str


class ApprovalDecisionRequest(BaseModel):
    """Request model for human approval decision and nonce verification."""
    model_config = ConfigDict(extra="forbid")
    decision: str
    nonce: Optional[str] = None


class ApprovalDecisionResponse(BaseModel):
    """Response model confirming recorded approval decision."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    decision: str
    recorded: bool


class ApprovalPendingResponse(BaseModel):
    """Response model indicating whether an agent run is awaiting approval."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    pending: bool
    approval_data: Optional[Dict[str, Any]] = None


class AgentRunDetailResponse(BaseModel):
    """Response model containing full details and history of an agent run."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    goal: str
    status: str
    created_at: str
    steps: list[Dict[str, Any]]


class AgentStepDetailResponse(BaseModel):
    """Response model detailing a single agent execution step."""
    model_config = ConfigDict(extra="forbid")
    step_id: str
    run_id: str
    action: str
    result: Optional[str] = None
    has_screenshot: bool
    screenshot_b64: Optional[str] = None
    timestamp: str


class AgentStepsListResponse(BaseModel):
    """Response model listing all execution steps for an agent run."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    count: int
    steps: list[AgentStepDetailResponse]


class AgentSessionStateResponse(BaseModel):
    """Response model containing active agent session state from Redis."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    state: Optional[Dict[str, Any]] = None


@app.post("/agent/run", response_model=AgentRunResponse)
async def run_agent_endpoint(payload: AgentRunRequest) -> AgentRunResponse:
    """Execute the ReAct agent loop for a given goal with real-time event broadcasting."""
    from backend.browser import get_browser_session
    from backend.tools import PlaywrightTools
    from backend.agent import ReActAgent

    run_id_str = str(payload.run_id) if payload.run_id else None
    async with get_browser_session() as session:
        tools = PlaywrightTools(page=session.page, run_id=run_id_str)

        async def handle_agent_event(event: Dict[str, Any]) -> None:
            """Forward agent events to the global run event hub for SSE broadcasting."""
            await run_event_hub.publish(agent.run_id, event)

        agent = ReActAgent(run_id=run_id_str, tools=tools, on_event=handle_agent_event)
        result = await agent.run(goal=payload.goal)

    return AgentRunResponse(
        run_id=result["run_id"],
        status=result["status"],
        iterations=result["iterations"],
        goal=result["goal"],
        threshold=result["threshold"],
        summary=result["summary"],
    )


@app.get("/agent/runs/{run_id}", response_model=AgentRunDetailResponse)
async def get_agent_run(run_id: uuid.UUID) -> AgentRunDetailResponse:
    """Fetch run details and step history from Neon database."""
    from fastapi import HTTPException
    from backend.db import get_db_pool
    pool = await get_db_pool()
    run_uuid = run_id

    async with pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT run_id, goal, status, created_at FROM agent_runs WHERE run_id = $1;",
            run_uuid,
        )
        if not run_row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        step_rows = await conn.fetch(
            """
            SELECT step_id, action, result, screenshot_b64, timestamp
            FROM agent_steps
            WHERE run_id = $1
            ORDER BY timestamp ASC;
            """,
            run_uuid,
        )

        steps = [
            {
                "step_id": str(r["step_id"]),
                "action": r["action"],
                "result": r["result"],
                "has_screenshot": bool(r["screenshot_b64"]),
                "timestamp": r["timestamp"].isoformat(),
            }
            for r in step_rows
        ]

        return AgentRunDetailResponse(
            run_id=str(run_row["run_id"]),
            goal=run_row["goal"],
            status=run_row["status"],
            created_at=run_row["created_at"].isoformat(),
            steps=steps,
        )


@app.get("/agent/runs/{run_id}/steps", response_model=AgentStepsListResponse)
async def list_agent_run_steps(
    run_id: uuid.UUID,
    include_screenshots: bool = Query(default=False, description="Include base64 screenshot buffers in response"),
) -> AgentStepsListResponse:
    """Fetch all persisted execution steps for an agent run ordered chronologically (Phase 2.6)."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        run_exists = await conn.fetchval(
            "SELECT 1 FROM agent_runs WHERE run_id = $1;",
            run_id,
        )
        if not run_exists:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        query = """
            SELECT step_id, run_id, action, result, screenshot_b64, timestamp
            FROM agent_steps
            WHERE run_id = $1
            ORDER BY timestamp ASC;
        """
        rows = await conn.fetch(query, run_id)
        steps_list = [
            AgentStepDetailResponse(
                step_id=str(r["step_id"]),
                run_id=str(r["run_id"]),
                action=r["action"],
                result=r["result"],
                has_screenshot=bool(r["screenshot_b64"]),
                screenshot_b64=r["screenshot_b64"] if include_screenshots else None,
                timestamp=r["timestamp"].isoformat(),
            )
            for r in rows
        ]
        return AgentStepsListResponse(
            run_id=str(run_id),
            count=len(steps_list),
            steps=steps_list,
        )


@app.get("/agent/steps/{step_id}", response_model=AgentStepDetailResponse)
async def get_agent_step(step_id: uuid.UUID) -> AgentStepDetailResponse:
    """Fetch single step details including base64 screenshot buffer from Neon DB (Phase 2.6)."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT step_id, run_id, action, result, screenshot_b64, timestamp
            FROM agent_steps
            WHERE step_id = $1;
            """,
            step_id,
        )
        if not r:
            raise HTTPException(status_code=404, detail=f"Step '{step_id}' not found")

        return AgentStepDetailResponse(
            step_id=str(r["step_id"]),
            run_id=str(r["run_id"]),
            action=r["action"],
            result=r["result"],
            has_screenshot=bool(r["screenshot_b64"]),
            screenshot_b64=r["screenshot_b64"],
            timestamp=r["timestamp"].isoformat(),
        )


@app.get("/agent/runs/{run_id}/verification", response_model=VerificationReport)
async def get_agent_run_verification(run_id: uuid.UUID) -> VerificationReport:
    """
    Fetch comprehensive verification report for an agent run, comparing actual invoice states
    in Neon against expected seed rules, including incomplete items and failed step screenshots (Phase 4).
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        run_exists = await conn.fetchval("SELECT 1 FROM agent_runs WHERE run_id = $1;", run_id)
        if not run_exists:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    return await generate_verification_report(pool, str(run_id))


@app.post("/agent/runs/{run_id}/pause", response_model=PauseResumeResponse)
async def pause_agent_run(run_id: str) -> PauseResumeResponse:
    """Set pause flag in Redis for an active agent run."""
    from fastapi import HTTPException
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    ok = await redis.set_pause_flag(run_id, paused=True)
    if not ok:
        raise HTTPException(status_code=503, detail="Redis is not configured")
    return PauseResumeResponse(
        run_id=run_id,
        paused=True,
        message="Agent run pause flag set to paused",
    )


@app.post("/agent/runs/{run_id}/resume", response_model=PauseResumeResponse)
async def resume_agent_run(run_id: str) -> PauseResumeResponse:
    """Clear pause flag in Redis for an active agent run."""
    from fastapi import HTTPException
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    ok = await redis.set_pause_flag(run_id, paused=False)
    if not ok:
        raise HTTPException(status_code=503, detail="Redis is not configured")
    return PauseResumeResponse(
        run_id=run_id,
        paused=False,
        message="Agent run resumed",
    )


@app.get("/agent/runs/{run_id}/approval", response_model=ApprovalPendingResponse)
async def get_pending_approval(run_id: str) -> ApprovalPendingResponse:
    """Fetch pending approval request details from Redis if awaiting approval (Phase 2.7 / Phase 3)."""
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    pending = await redis.get_approval_pending(run_id)
    if pending and pending.get("status") == "awaiting_approval":
        return ApprovalPendingResponse(run_id=run_id, pending=True, approval_data=pending)
    return ApprovalPendingResponse(run_id=run_id, pending=False, approval_data=None)


@app.post("/agent/runs/{run_id}/approval", response_model=ApprovalDecisionResponse)
async def submit_approval_decision(run_id: str, payload: ApprovalDecisionRequest) -> ApprovalDecisionResponse:
    """Record human approval decision ('approved' or 'rejected') in Redis with active request and nonce check."""
    from fastapi import HTTPException
    from backend.redis_client import get_redis_client
    redis = get_redis_client()

    # Require an active pending approval request
    pending = await redis.get_approval_pending(run_id)
    if not pending or pending.get("status") != "awaiting_approval":
        raise HTTPException(
            status_code=400,
            detail=f"No active pending approval request for run '{run_id}'",
        )

    # Validate per-request nonce if provided or expected
    pending_nonce = pending.get("nonce")
    if pending_nonce and (payload.nonce is None or payload.nonce != pending_nonce):
        raise HTTPException(
            status_code=400,
            detail="Invalid or missing approval nonce for the current pending request",
        )

    norm_decision = payload.decision.strip().lower()
    if norm_decision not in ("approved", "rejected"):
        raise HTTPException(
            status_code=400,
            detail=f"Decision must be 'approved' or 'rejected', got '{payload.decision}'",
        )

    ok = await redis.set_approval_decision(run_id, norm_decision, nonce=pending_nonce)
    return ApprovalDecisionResponse(
        run_id=run_id,
        decision=norm_decision,
        recorded=ok,
    )


@app.get("/agent/runs/{run_id}/state", response_model=AgentSessionStateResponse)
async def get_agent_state(run_id: str) -> AgentSessionStateResponse:
    """Fetch active session state from Redis."""
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    state = await redis.get_session_state(run_id)
    return AgentSessionStateResponse(
        run_id=run_id,
        state=state,
    )


@app.get("/agent/runs/{run_id}/stream", response_class=EventSourceResponse)
async def stream_agent_run_endpoint(run_id: uuid.UUID) -> EventSourceResponse:
    """
    Server-Sent Events (SSE) streaming endpoint: GET /agent/runs/{run_id}/stream.
    Streams structured real-time agent execution events and history playback from Neon DB.
    """
    run_id_str = str(run_id)

    async def event_generator():
        """Asynchronous generator yielding historical and live SSE events."""
        # 1. History playback from Neon database
        try:
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                run_row = await conn.fetchrow(
                    "SELECT run_id, goal, status, created_at FROM agent_runs WHERE run_id = $1;",
                    run_id,
                )
                if run_row:
                    yield {
                        "event": "status_change",
                        "data": json.dumps({
                            "type": "status_change",
                            "run_id": run_id_str,
                            "status": run_row["status"],
                            "timestamp": run_row["created_at"].isoformat(),
                        })
                    }

                step_rows = await conn.fetch(
                    """
                    SELECT step_id, action, result, screenshot_b64, timestamp
                    FROM agent_steps
                    WHERE run_id = $1
                    ORDER BY timestamp ASC;
                    """,
                    run_id,
                )
                for idx, r in enumerate(step_rows, start=1):
                    res_str = r["result"] or ""
                    is_fail = "failed" in res_str.lower() or "aborted" in res_str.lower()
                    event_type = "step_failed" if is_fail else "step_complete"
                    event_data = {
                        "type": event_type,
                        "run_id": run_id_str,
                        "step_id": str(r["step_id"]),
                        "step_index": idx,
                        "action": r["action"],
                        "result": res_str,
                        "timestamp": r["timestamp"].isoformat(),
                        "has_screenshot": bool(r["screenshot_b64"]),
                    }
                    if is_fail:
                        event_data["error"] = res_str
                    yield {
                        "event": event_type,
                        "data": json.dumps(event_data)
                    }
        except Exception as db_err:
            logger.warning(f"Error fetching historical steps for SSE stream {run_id_str}: {db_err}")

        # 2. Live event queue subscription
        queue = run_event_hub.subscribe(run_id_str)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_type = event.get("type", "message")
                    yield {
                        "event": event_type,
                        "data": json.dumps(event)
                    }
                except asyncio.TimeoutError:
                    # Keep-alive ping event / comment
                    yield {
                        "event": "ping",
                        "data": json.dumps({
                            "type": "ping",
                            "run_id": run_id_str,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                    }
        finally:
            run_event_hub.unsubscribe(run_id_str, queue)

    return EventSourceResponse(event_generator())



