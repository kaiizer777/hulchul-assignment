import logging
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
from fastapi import FastAPI, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
import asyncpg

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, check_db_health, get_db_connection
from backend.browser import verify_cdp_connection

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
    model_config = ConfigDict(extra="forbid")
    status: str
    database: str
    version: str


class BrowserHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connected: bool
    browser_type: Optional[str] = None
    blank_page_title: Optional[str] = None
    latency_ms: float
    endpoint_configured: bool
    error: Optional[str] = None


class RootResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    version: str
    phase: str


class CheckExistsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str
    identifier: str


class CheckExistsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exists: bool
    entity_type: str
    identifier: str
    record: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ToolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    tools: list[Dict[str, Any]]


@app.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    return RootResponse(
        message="Hulchul Backend API is running",
        version="0.2.0",
        phase="Phase 2.1, 2.2, 2.3 & 2.4 Complete",
    )


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
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
    model_config = ConfigDict(extra="forbid")
    goal: str
    run_id: Optional[uuid.UUID] = None


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: str
    iterations: int
    goal: str
    threshold: float
    summary: Optional[str] = None


class PauseResumeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    paused: bool
    message: str


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    nonce: Optional[str] = None


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    decision: str
    recorded: bool


class AgentRunDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    goal: str
    status: str
    created_at: str
    steps: list[Dict[str, Any]]


class AgentSessionStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    state: Optional[Dict[str, Any]] = None


@app.post("/agent/run", response_model=AgentRunResponse)
async def run_agent_endpoint(payload: AgentRunRequest) -> AgentRunResponse:
    """Execute the ReAct agent loop for a given goal."""
    from backend.browser import get_browser_session
    from backend.tools import PlaywrightTools
    from backend.agent import ReActAgent

    run_id_str = str(payload.run_id) if payload.run_id else None
    async with get_browser_session() as session:
        tools = PlaywrightTools(page=session.page, run_id=run_id_str)
        agent = ReActAgent(run_id=run_id_str, tools=tools)
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
    if pending_nonce and payload.nonce != pending_nonce:
        raise HTTPException(
            status_code=400,
            detail="Invalid approval nonce for the current pending request",
        )

    ok = await redis.set_approval_decision(run_id, payload.decision, nonce=pending_nonce)
    return ApprovalDecisionResponse(
        run_id=run_id,
        decision=payload.decision,
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


