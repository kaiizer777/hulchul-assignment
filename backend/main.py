import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional, Set, Tuple
from fastapi import FastAPI, Depends, Request, Response, status, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
import asyncpg
from sse_starlette.sse import EventSourceResponse

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, check_db_health, get_db_connection, get_db_pool
from backend.browser import verify_cdp_connection
from backend.run_lease import affected_rows, reconcile_orphaned_agent_runs
from backend.verification import VerificationReport, generate_verification_report
from backend.auth import (
    AUTH_UNAVAILABLE_DETAIL,
    INVALID_CREDENTIALS_DETAIL,
    Session,
    clear_login_failures,
    clear_session_cookie,
    client_ip,
    create_session,
    destroy_session,
    enforce_login_rate_limit,
    is_https_request,
    read_session_token,
    require_session,
    set_session_cookie,
    verify_password,
)


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


# In-memory registry of background agent executions keyed by run_id.
# asyncio.create_task is used (not Starlette BackgroundTasks) so POST /agent/run
# can return 202 immediately: BackgroundTasks are awaited before the ASGI app
# returns, which would keep the client waiting.
#
# Accepted tradeoff: this task has no durable owner. Lambda freeze/thaw can
# suspend a warm-container task, the 15-min Lambda ceiling applies, and
# container replacement loses the run with no startup path reclaiming it, so an
# accepted run can stay 'running' without progressing. Durable dispatch needs a
# worker plus queue, IAM and deploy plumbing and is tracked in #56; cross-instance
# event-hub loss is tracked in #33. In-PR mitigations only narrow the window: a
# cancelled task records a terminal status in the database and Redis
# (_mark_agent_run_terminal), the SSE stream subscribes before reading
# history so no live event is lost across the handoff, and an open stream also
# reconciles against the durable store on an interval so a lost in-process
# subscription cannot strand it on stale state.
_active_agent_tasks: Dict[str, asyncio.Task] = {}
# Synchronous reservation guard closing the check-then-act race between the
# existing-task check in run_agent_endpoint and asyncio.create_task: the first
# await (get_db_pool) would otherwise let a second POST with the same run_id
# pass the check and start a duplicate side-effecting loop. Reserved
# synchronously before the first await; released on setup failure or when the
# background task finishes.
_reserved_agent_runs: Set[str] = set()


# Ceiling for each database write that reports a terminal run status. The
# cancellation handler shields this coroutine so a second cancellation cannot
# abort it, and the asyncpg pool is configured without a command or acquire
# timeout (backend/db.py), so a database that accepts the connection and then
# never answers would hold that shielded await -- and anything draining it --
# with no ceiling. Bounding the individual writes rather than the coroutine
# keeps the shield honest: no orphaned follow-up task is created, the terminal
# status is still recorded whenever the database responds, and the wait always
# terminates. Redis needs no equivalent bound; its client is already built with
# httpx.AsyncClient(timeout=10.0).
TERMINAL_WRITE_IO_TIMEOUT_SECONDS: float = 5.0

# Ceiling for the startup orphan sweep (issue #56). The asyncpg pool is built
# without a command_timeout and acquire() is called without one (backend/db.py),
# so a connection that is accepted and then never answers parks the sweep
# indefinitely -- and `except` cannot catch a hang. The sweep runs before
# `yield`, so that would stop the app serving requests at all, which is strictly
# worse than an incomplete sweep. Bounding it lets the cold start proceed and
# leaves the remainder to the next one.
#
# Exceeding it abandons the sweep mid-flight, which is safe: every candidate is
# reclaimed by its own conditional UPDATE, so the runs already processed stay
# reclaimed and only the rest are deferred.
STARTUP_RECONCILE_TIMEOUT_SECONDS: float = 15.0


async def _mark_agent_run_terminal(
    run_id_str: str,
    status: str,
    owner_id: Optional[str] = None,
) -> None:
    """Record a terminal run status in the database, Redis, and the event hub.

    Each target is written in its own try/except so a failure in one cannot
    prevent the others from recording the terminal status.

    ``owner_id`` fences the database write on the run lease (issue #56). This helper is
    the fallback terminal write for an execution that never produced a result of its
    own -- a cancellation, or an exception raised out of ``ReActAgent.run`` -- and an
    expired lease does not stop its former holder. Without the fence a superseded
    execution stamps ``failed`` over the run its replacement is executing, which is
    the same overwrite the owner-scoped write in ``update_run_status`` exists to
    prevent, and that fence is only load-bearing while every other terminal write
    carries one too.

    A NULL owner stays writable, so a cancellation landing before ``ensure_run_record``
    claimed anything still records ``failed`` rather than stranding the run as
    ``running`` until the next cold start. A *different* owner is not writable, and
    neither is a live lease held by someone else: passing ``owner_id=None`` means no
    agent was ever built for this run, so this request never claimed a lease and must
    not touch a run somebody else holds.

    A landed write also hands the lease back, in the same statement so the write stays
    a single round trip. Leaving ``owner_id`` set on a ``failed`` row keeps the run
    unclaimable for up to ``RUN_LEASE_SECONDS``, refusing a legitimate retry for the
    whole window.

    The hub publication is gated on the database write succeeding. The stream
    reconciles `agent_runs.status` on every poll, so announcing a status the
    durable store never accepted would make the next poll re-emit the older one
    as a fresh transition and walk the client backwards from `failed` to
    `running`. That gate now covers a fenced-out write as well: zero affected rows
    means this execution does not own the run, so it has no status to announce.
    Redis is a separate read model and is still written best-effort.
    """
    db_updated = False
    # Distinct from db_updated: the database could not be reached or answered, which
    # says nothing about who owns the run, as opposed to a fence that refused the write.
    db_errored = False
    try:
        pool = await asyncio.wait_for(get_db_pool(), TERMINAL_WRITE_IO_TIMEOUT_SECONDS)
        async with pool.acquire(timeout=TERMINAL_WRITE_IO_TIMEOUT_SECONDS) as conn:
            if owner_id is None:
                statement = (
                    "UPDATE agent_runs SET status = $1 WHERE run_id = $2 "
                    "AND (agent_runs.owner_id IS NULL "
                    "OR agent_runs.lease_expires_at <= now());"
                )
                params: Tuple[Any, ...] = (status, uuid.UUID(run_id_str))
            else:
                statement = (
                    "UPDATE agent_runs "
                    "SET status = $1, owner_id = NULL, lease_expires_at = NULL "
                    "WHERE run_id = $2 "
                    "AND (agent_runs.owner_id IS NULL OR agent_runs.owner_id = $3);"
                )
                params = (status, uuid.UUID(run_id_str), owner_id)
            command_tag = await asyncio.wait_for(
                conn.execute(statement, *params),
                TERMINAL_WRITE_IO_TIMEOUT_SECONDS,
            )
        db_updated = affected_rows(command_tag) > 0
        if not db_updated:
            logger.warning(
                f"Did not mark run {run_id_str} as {status}: this execution no longer "
                "owns the run lease, so the newer owner's state stands."
            )
    except Exception as db_err:
        db_errored = True
        logger.warning(f"Could not mark run {run_id_str} as {status} in the database: {db_err}")

    # Only a fenced-out write is skipped. A database error still falls through to
    # Redis: nothing is known about the durable row in that case, so the read model
    # gets the best information available rather than none, and
    # test_issue29_nonblocking pins that contract for a stalled database. A fenced-out
    # write is the opposite case -- the newer owner's state is known to be correct and
    # this execution has no claim on the run -- so writing `failed` into the shared
    # snapshot would hand clients exactly the status the database and the hub just
    # refused to accept. set_session_state replaces the snapshot's status outright, so
    # the clobber is not limited to the field.
    if db_updated or db_errored:
        try:
            from backend.redis_client import get_redis_client
            redis = get_redis_client()
            state = await redis.get_session_state(run_id_str)
            if state is not None:
                state["status"] = status
                await redis.set_session_state(run_id_str, state)
        except Exception as redis_err:
            logger.warning(f"Could not mark run {run_id_str} as {status} in Redis: {redis_err}")

    if not db_updated:
        return

    try:
        await run_event_hub.publish(
            run_id_str,
            {
                "type": "status_change",
                "run_id": run_id_str,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as hub_err:
        logger.warning(f"Could not publish terminal status for run {run_id_str}: {hub_err}")


def _log_terminal_write_outcome(terminal_write: "asyncio.Future[None]") -> None:
    """Surface failures from a terminal-status write that outlived its handler.

    Synchronous by necessity: asyncio invokes a done callback from the event
    loop and never awaits its return value, so an `async def` here would build a
    coroutine that nobody awaits -- the log would never fire and every cancelled
    run would emit a "coroutine was never awaited" RuntimeWarning.
    """
    if terminal_write.cancelled():
        return
    write_err = terminal_write.exception()
    if write_err is not None:
        logger.warning(f"Terminal status write did not complete cleanly: {write_err}")


async def _execute_agent_run_background(run_id_str: str, goal: str) -> None:
    """Run the full ReAct loop off-request; publish live events via run_event_hub."""
    from backend.browser import get_browser_session, run_cancellation_safe, MAX_CDP_REATTACH_ATTEMPTS
    from backend.tools import PlaywrightTools
    from backend.agent import ReActAgent

    session_stack: list = []
    # ReActAgent.run records the run's own terminal status (and the matching
    # Redis state) before it returns. A cancellation that lands during the
    # teardown below therefore arrives *after* the run already committed
    # 'completed', and writing 'failed' over it would contradict the durable
    # record the SSE poll reads on every pass.
    agent_finished = False

    # Retained past the block that builds it so the two terminal-status arms below can
    # fence on the run lease (issue #56). Without a reference to the agent those arms
    # have only _mark_agent_run_terminal to fall back on, which is why it now needs an
    # owner id. None means no agent was ever constructed -- the CDP session or the
    # ReActAgent constructor failed first -- so this request never claimed a lease.
    agent: Optional["ReActAgent"] = None

    async def _enter_cdp_session():
        """Enter a browser session context manager and track it for later release."""
        cm = get_browser_session()
        sess = await cm.__aenter__()
        session_stack.append(cm)
        return sess

    try:
        try:
            session = await _enter_cdp_session()
            tools = PlaywrightTools(page=session.page, run_id=run_id_str)

            async def _reattach_page():
                """Release the current browser session and return a freshly acquired page."""
                if session_stack:
                    old_cm = session_stack.pop()
                    try:
                        await old_cm.__aexit__(None, None, None)
                    except Exception as release_err:
                        logger.warning(f"Failed to release previous browser session during reattach: {release_err}")
                new_session = await _enter_cdp_session()
                return new_session.page

            # No on_event forwarding here: ReActAgent.emit_event already publishes
            # every event to run_event_hub itself, so a hub-publishing callback
            # delivered each event to SSE subscribers twice.
            agent = ReActAgent(
                run_id=run_id_str,
                tools=tools,
                reconnect=_reattach_page,
                max_reattaches=MAX_CDP_REATTACH_ATTEMPTS,
            )
            await agent.run(goal=goal)
            agent_finished = True
        finally:
            # Teardown is reached either by a finished run or by a cancellation
            # landing in the run above, and the second cancellation that follows
            # it must not abort a close midway: a session left open holds a
            # remote browser for its whole timeout. Each close is therefore run
            # cancellation-safe, and a cancellation seen here is re-raised only
            # after every remaining session has been released.
            pending_cancel: Optional[asyncio.CancelledError] = None
            for cm in reversed(session_stack):
                try:
                    await run_cancellation_safe(cm.__aexit__(None, None, None))
                except asyncio.CancelledError as cancel_err:
                    pending_cancel = cancel_err
                except Exception:
                    pass
            if pending_cancel is not None:
                raise pending_cancel
    except asyncio.CancelledError:
        # CancelledError is a BaseException since 3.8, so the `except Exception`
        # arm below never sees a cancelled run. Without this branch the task
        # unwinds without recording a terminal status and the run stays
        # 'running' in the database and Redis forever.
        if agent_finished:
            # The run already recorded its own terminal status. Overwriting it
            # here would report a completed run as failed purely because its
            # browser sessions were slow to close.
            logger.warning(
                f"Background agent run {run_id_str} was cancelled during teardown "
                "after the agent finished; keeping the terminal status it recorded"
            )
            raise
        logger.warning(f"Background agent run {run_id_str} was cancelled")
        # Awaiting the terminal writes directly leaves them interruptible: a
        # second cancellation (a repeated cancel, or an enclosing wait_for
        # timeout) would abort them mid-flight and strand the run as 'running'.
        # shield() lets the writes run to completion across further
        # cancellations of this handler, and every write inside is individually
        # bounded (TERMINAL_WRITE_IO_TIMEOUT_SECONDS), so the shield is never
        # waiting on an unbounded database await. Lifespan never awaits these
        # tasks, so there is no drain to stall either way.
        terminal_write = asyncio.ensure_future(
            _mark_agent_run_terminal(
                run_id_str, "failed", agent.run_owner_id if agent is not None else None
            )
        )
        # Registered before the shield, not only on the cancellation path below,
        # so the fallback log also covers a cancellation landing between creating
        # the task and awaiting it.
        terminal_write.add_done_callback(_log_terminal_write_outcome)
        # A cancellation raised out of this await propagates: the handler is
        # already unwinding from a CancelledError, which is never swallowed.
        await asyncio.shield(terminal_write)
        raise
    except Exception as e:
        logger.exception(f"Background agent run {run_id_str} failed: {e}")
        # Fenced on the lease owner for the same reason as the cancellation arm: a
        # run() that raised because it lost the lease must not stamp 'failed' over
        # the run its replacement is executing.
        await _mark_agent_run_terminal(
            run_id_str, "failed", agent.run_owner_id if agent is not None else None
        )
    finally:
        current = asyncio.current_task()
        if current is not None and _active_agent_tasks.get(run_id_str) is current:
            _active_agent_tasks.pop(run_id_str, None)
        _reserved_agent_runs.discard(run_id_str)


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

    # Reclaim runs abandoned by a previous execution environment (issue #56). Separate
    # try/except from pool init: a failed sweep must never be able to stop the app from
    # starting, and adding DB round trips to every cold start is the one thing a
    # serverless invocation cannot afford.
    try:
        pool = await get_db_pool()
        reclaimed = await asyncio.wait_for(
            reconcile_orphaned_agent_runs(pool),
            timeout=STARTUP_RECONCILE_TIMEOUT_SECONDS,
        )
        if reclaimed:
            logger.error(
                f"Reconciled {len(reclaimed)} orphaned agent run(s) on startup: {reclaimed}"
            )
    except asyncio.TimeoutError:
        logger.error(
            f"Startup orphan sweep exceeded {STARTUP_RECONCILE_TIMEOUT_SECONDS}s and was "
            "abandoned; any runs it did not reach stay 'running' and are retried on the "
            "next cold start."
        )
    except Exception as e:
        logger.error(f"Failed to reconcile orphaned agent runs on startup: {e}")
    yield
    logger.info("FastAPI shutting down: closing asyncpg database pool...")
    await close_db_pool()


app = FastAPI(
    title="Hulchul Agent Core Backend",
    version="0.2.0",
    description="FastAPI ReAct Agent Backend with Remote CDP Browser Automation",
    lifespan=lifespan,
    # The interactive docs and the OpenAPI schema are unauthenticated and are not
    # covered by the session guard, so leaving them on publishes every route --
    # including /auth/login -- to anyone who can reach the Lambda URL. Off unless
    # explicitly requested; the contract is deny-by-default.
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)

# CORS configuration for Next.js frontend and Cloudflare Workers via dynamic settings
def get_allowed_origins() -> list[str]:
    """
    Build the CORS origin allowlist as exact origins only.

    Wildcards are dropped rather than expanded: `*` is invalid together with
    allow_credentials, and Starlette has never supported host globs, so both used
    to be dead config that left `*` doing all the work.
    """
    candidates = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    candidates.append(settings.ERP_BASE_URL)
    candidates.append(settings.FRONTEND_URL)
    candidates.append(settings.NEXT_PUBLIC_API_URL)

    origins: list[str] = []
    for origin in candidates:
        if not origin or "*" in origin:
            if origin:
                logger.warning(f"Ignoring non-exact CORS origin '{origin}'")
            continue
        if origin not in origins:
            origins.append(origin)
    return origins


allowed_origins = get_allowed_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    # Credentials stay on because the session rides in a cookie. Per the CORS spec
    # `*` is not a legal value alongside credentials, so removing `*` above also
    # fixes that latent misconfiguration.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Cookie"],
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


# ---------------------------------------------------------------------------
# Authentication (single operator, cookie session)
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    """Login credential payload. JSON only: python-multipart is intentionally absent."""
    model_config = ConfigDict(extra="forbid")
    password: str


@app.post("/auth/login", response_model=Session)
async def login(payload: LoginRequest, request: Request, response: Response) -> Session:
    """Exchange the operator password for a session cookie."""
    ip = client_ip(request)
    # Atomic pre-hash gate: enforce_login_rate_limit issues a single
    # server-atomic INCR and 429s past budget before argon2 runs. That INCR
    # is the failure record, so no second increment happens on a 401.
    await enforce_login_rate_limit(ip)

    if not await verify_password(payload.password):
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS_DETAIL)

    await clear_login_failures(ip)
    issued = await create_session()
    if issued is None:
        raise HTTPException(status_code=503, detail=AUTH_UNAVAILABLE_DETAIL)

    set_session_cookie(response, issued.token, is_https_request(request))
    return issued.session


@app.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    """Revoke the current session and clear the cookie. 204 is idempotent; 503 (cookie kept) when revocation cannot be confirmed."""
    revoked = await destroy_session(read_session_token(request))
    if not revoked:
        raise HTTPException(status_code=503, detail=AUTH_UNAVAILABLE_DETAIL)
    clear_session_cookie(response, is_https_request(request))


@app.get("/auth/session", response_model=Session)
async def auth_session(session: Session = Depends(require_session)) -> Session:
    """Return the active session, or 401 when there is no valid one."""
    return session


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
async def list_tools(_session: Session = Depends(require_session)) -> ToolsResponse:
    """List available LLM agent tool definitions with parameters and schemas."""
    from backend.tools import TOOL_DEFINITIONS
    return ToolsResponse(
        count=len(TOOL_DEFINITIONS),
        tools=TOOL_DEFINITIONS,
    )


@app.post("/tools/check-exists", response_model=CheckExistsResponse)
async def check_exists_endpoint(
    payload: CheckExistsRequest,
    _session: Session = Depends(require_session),
) -> CheckExistsResponse:
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


@app.post("/agent/run", response_model=AgentRunResponse, status_code=202)
async def run_agent_endpoint(
    payload: AgentRunRequest,
    _session: Session = Depends(require_session),
) -> AgentRunResponse:
    """Accept an agent goal, persist the run record, and continue the ReAct loop in background."""
    from backend.agent import extract_approval_threshold

    run_id_str = str(payload.run_id) if payload.run_id else str(uuid.uuid4())
    goal = payload.goal
    threshold = extract_approval_threshold(goal, default=settings.DEFAULT_APPROVAL_THRESHOLD)

    existing = _active_agent_tasks.get(run_id_str)
    if existing is not None and not existing.done():
        return AgentRunResponse(
            run_id=run_id_str,
            status="running",
            iterations=0,
            goal=goal,
            threshold=threshold,
            summary=None,
        )
    if run_id_str in _reserved_agent_runs:
        return AgentRunResponse(
            run_id=run_id_str,
            status="running",
            iterations=0,
            goal=goal,
            threshold=threshold,
            summary=None,
        )
    _reserved_agent_runs.add(run_id_str)
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # DO NOTHING, not DO UPDATE SET status = 'running' (issue #56). This runs
            # before the background task claims the run lease, so it cannot know whether
            # this request will be accepted. An unconditional update therefore wrote
            # 'running' for a run whose claim was about to be refused, leaving a row the
            # client was told was running with no executor behind it, and reset a live
            # owner's 'paused' / 'awaiting_approval' back to 'running' on every duplicate
            # request -- which that owner never re-reads, so nothing noticed.
            #
            # Creating the row is still this statement's job: the SSE stream replays
            # agent_runs on connect, so the run has to exist before the client opens it.
            # Status and goal transitions on an existing row belong to
            # ReActAgent.ensure_run_record, which is the lease-aware statement and can
            # make them only once it actually holds the lease.
            #
            # RETURNING is not needed to tell the two outcomes apart: with DO NOTHING the
            # command tag already carries it -- "INSERT 0 1" when this request created
            # the row, "INSERT 0 0" when it already existed. execute() rather than
            # fetchval() keeps this a single round trip and leaves the statement's shape
            # unchanged for anything asserting on it.
            insert_tag = await conn.execute(
                """
                INSERT INTO agent_runs (run_id, goal, status, created_at)
                VALUES ($1, $2, 'running', now())
                ON CONFLICT (run_id) DO NOTHING;
                """,
                uuid.UUID(run_id_str),
                goal,
            )

        # Only for a row this request actually created. set_session_state is a plain
        # SET, so it replaces the whole snapshot: seeding it unconditionally would
        # rewrite a live run's 'paused' / 'awaiting_approval' state and its step index
        # back to running/0 on every duplicate request, from any process, while the
        # Postgres row -- which the agent and the SSE poll both read -- correctly kept
        # its status. run() seeds the snapshot itself once it holds the lease.
        if affected_rows(insert_tag) > 0:
            try:
                from backend.redis_client import get_redis_client
                redis = get_redis_client()
                await redis.set_session_state(
                    run_id_str,
                    {
                        "run_id": run_id_str,
                        "goal": goal,
                        "threshold": threshold,
                        "current_step": 0,
                        "status": "running",
                    },
                )
            except Exception as redis_err:
                logger.warning(f"Best-effort session state init failed for run {run_id_str}: {redis_err}")

        task = asyncio.create_task(_execute_agent_run_background(run_id_str, goal))
        _active_agent_tasks[run_id_str] = task
    except BaseException:
        _reserved_agent_runs.discard(run_id_str)
        raise
    return AgentRunResponse(
        run_id=run_id_str,
        status="running",
        iterations=0,
        goal=goal,
        threshold=threshold,
        summary=None,
    )


@app.get("/agent/runs/{run_id}", response_model=AgentRunDetailResponse)
async def get_agent_run(
    run_id: uuid.UUID,
    _session: Session = Depends(require_session),
) -> AgentRunDetailResponse:
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
    _session: Session = Depends(require_session),
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
async def get_agent_step(
    step_id: uuid.UUID,
    _session: Session = Depends(require_session),
) -> AgentStepDetailResponse:
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
async def get_agent_run_verification(
    run_id: uuid.UUID,
    _session: Session = Depends(require_session),
) -> VerificationReport:
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
async def pause_agent_run(
    run_id: str,
    _session: Session = Depends(require_session),
) -> PauseResumeResponse:
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
async def resume_agent_run(
    run_id: str,
    _session: Session = Depends(require_session),
) -> PauseResumeResponse:
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
async def get_pending_approval(
    run_id: str,
    _session: Session = Depends(require_session),
) -> ApprovalPendingResponse:
    """Fetch pending approval request details from Redis if awaiting approval (Phase 2.7 / Phase 3)."""
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    pending = await redis.get_approval_pending(run_id)
    if pending and pending.get("status") == "awaiting_approval":
        return ApprovalPendingResponse(run_id=run_id, pending=True, approval_data=pending)
    return ApprovalPendingResponse(run_id=run_id, pending=False, approval_data=None)


@app.post("/agent/runs/{run_id}/approval", response_model=ApprovalDecisionResponse)
async def submit_approval_decision(
    run_id: str,
    payload: ApprovalDecisionRequest,
    _session: Session = Depends(require_session),
) -> ApprovalDecisionResponse:
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

    # Validate per-request nonce (fail closed: pending must carry a nonce and it must match)
    pending_nonce = pending.get("nonce")
    if not pending_nonce or payload.nonce is None or payload.nonce != pending_nonce:
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
async def get_agent_state(
    run_id: str,
    _session: Session = Depends(require_session),
) -> AgentSessionStateResponse:
    """Fetch active session state from Redis."""
    from backend.redis_client import get_redis_client
    redis = get_redis_client()
    state = await redis.get_session_state(run_id)
    return AgentSessionStateResponse(
        run_id=run_id,
        state=state,
    )


# Cadence for reconciling an open SSE stream against the durable store. Short
# enough that a step the in-process hub never saw still lands promptly, long
# enough that one indexed SELECT per run per interval is not a cost worth tuning
# further in-process.
SSE_DURABLE_POLL_INTERVAL_SECONDS: float = 3.0
# Keep-alive cadence so proxies and Lambda do not reap an idle stream. Longer
# than the poll interval, so a live stream still reconciles several times per
# ping.
SSE_PING_INTERVAL_SECONDS: float = 15.0
# Ceiling on one durable reconcile read. The cursor poll's queries are bounded
# work -- an indexed SELECT over the rows committed since the last read, or the
# run's whole step list only on the first poll of a stream that has replayed
# nothing -- so anything slower than this is a stalled connection rather than a
# large result. Left unbounded, such a read blocks the generator and stops the
# ping above, which is the silent stall this poll design exists to prevent.
# Deliberately above the poll interval so a single slow-but-successful read is
# not killed and rescheduled every 3s, and below the ping interval so one
# timeout delays keep-alive by at most 5s instead of forever. It is 1.7% of the
# 300s Lambda function timeout, so a stream absorbs many consecutive timeouts
# within one invocation.
SSE_RECONCILE_TIMEOUT_SECONDS: float = 5.0

# Whole step list for a run, ordered by the same (timestamp, step_id) key the
# incremental poll uses so the two orderings cannot disagree.
_SSE_STEPS_ALL_SQL = """
    SELECT step_id, action, result, screenshot_b64, timestamp
    FROM agent_steps
    WHERE run_id = $1
    ORDER BY timestamp ASC, step_id ASC;
"""

# Steps committed after the last one this stream delivered. The row comparison
# advances the cursor past every row it has already seen, so a batch of steps
# sharing one timestamp is neither skipped nor replayed.
# idx_agent_steps_run_timestamp (run_id, timestamp, step_id) covers the predicate.
# Both comparison columns have to be in the index: Postgres can only use a row
# comparison as an index qual when the index covers every column it spans.
_SSE_STEPS_AFTER_CURSOR_SQL = """
    SELECT step_id, action, result, screenshot_b64, timestamp
    FROM agent_steps
    WHERE run_id = $1 AND (timestamp, step_id) > ($2, $3)
    ORDER BY timestamp ASC, step_id ASC;
"""


# The event types this stream treats as a persisted agent_steps row: the same
# classifications _step_row_to_frame derives, so a live event and a replayed row
# for one step are the same kind of frame. step_start is deliberately excluded --
# it carries the same step_id as the terminal event that follows it, so
# registering it here would suppress that step's own completion.
_SSE_PERSISTED_STEP_EVENT_TYPES = frozenset(
    {"step_complete", "step_failed", "step_unknown", "session_lost"}
)


def _status_change_frame(run_id_str: str, status: str, timestamp: str) -> Dict[str, Any]:
    """Build the SSE frame carrying a run status transition."""
    return {
        "event": "status_change",
        "data": json.dumps({
            "type": "status_change",
            "run_id": run_id_str,
            "status": status,
            "timestamp": timestamp,
        }),
    }


def _done_frame(run_id_str: str, status: str = "completed") -> Dict[str, Any]:
    """Build the SSE terminal 'done' frame."""
    return {
        "event": "done",
        "data": json.dumps({
            "type": "done",
            "run_id": run_id_str,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }),
    }


def _step_row_to_frame(run_id_str: str, row: Any, step_index: int) -> Dict[str, Any]:
    """Map a persisted agent_steps row to its SSE frame.

    One mapping for both the connect-time replay and the durable poll so the
    persisted-step classification cannot drift between them: terminal
    session_lost only for a session_lost action, an `outcome unknown` result as
    step_unknown, and any other failed/aborted result as step_failed.
    """
    res_str = row["result"] or ""
    lowered = res_str.lower()
    persisted_action = row["action"] or ""
    is_terminal_session_lost = persisted_action == "session_lost"
    is_unknown = lowered.startswith("outcome unknown")
    is_fail = ("failed" in lowered or "aborted" in lowered) and not is_unknown
    if is_terminal_session_lost:
        event_type = "session_lost"
    elif is_unknown:
        event_type = "step_unknown"
    else:
        event_type = "step_failed" if is_fail else "step_complete"
    event_data = {
        "type": event_type,
        "run_id": run_id_str,
        "step_id": str(row["step_id"]),
        "step_index": step_index,
        "action": row["action"],
        "result": res_str,
        "timestamp": row["timestamp"].isoformat(),
        "has_screenshot": bool(row["screenshot_b64"]),
    }
    if is_terminal_session_lost:
        event_data["terminal"] = True
        event_data["reattached"] = False
    if is_fail or is_unknown:
        event_data["error"] = res_str
    return {
        "event": event_type,
        "data": json.dumps(event_data),
    }


@app.get("/agent/runs/{run_id}/stream", response_class=EventSourceResponse)
async def stream_agent_run_endpoint(
    run_id: uuid.UUID,
    _session: Session = Depends(require_session),
) -> EventSourceResponse:
    """
    Server-Sent Events (SSE) streaming endpoint: GET /agent/runs/{run_id}/stream.
    Streams structured real-time agent execution events and history playback from Neon DB.
    """
    run_id_str = str(run_id)

    async def event_generator():
        """Yield history playback, live hub events, and durable-state reconciliation."""
        # Subscribe before reading history. An event published between the history
        # SELECT and the subscription would land in neither delivery path and be
        # lost from a still-open stream, so the queue buffers anything that arrives
        # during the replay below.
        queue = run_event_hub.subscribe(run_id_str)
        # step_ids already delivered on this stream. The agent persists a step
        # before publishing its live event, so the live path and the durable poll
        # can both offer the same step, in either order; both paths check this
        # set before yielding, which keeps the second delivery out instead of
        # depending on the frontend's dedupe. Bounded by the run's own step
        # count, which the agent's iteration cap bounds.
        emitted_step_ids: Set[str] = set()
        emitted_done: bool = False
        # (timestamp, step_id) of the newest step delivered so far. Seeded by the
        # replay so the poll resumes strictly after it; the row comparison in
        # _SSE_STEPS_AFTER_CURSOR_SQL keeps rows sharing a timestamp from being
        # skipped or replayed.
        step_cursor: Optional[Tuple[Any, Any]] = None
        # Last status delivered, whatever path delivered it. status_change has no
        # idempotency key the way step_id is, so it is emitted on change only.
        last_status: Optional[str] = None
        next_step_index = 1
        try:
            # 1. History playback from Neon database
            try:
                pool = await get_db_pool()
                async with pool.acquire() as conn:
                    run_row = await conn.fetchrow(
                        "SELECT run_id, goal, status, created_at FROM agent_runs WHERE run_id = $1;",
                        run_id,
                    )
                    if run_row:
                        last_status = run_row["status"]
                        yield _status_change_frame(
                            run_id_str, last_status, run_row["created_at"].isoformat()
                        )

                    step_rows = await conn.fetch(_SSE_STEPS_ALL_SQL, run_id)
                    for r in step_rows:
                        emitted_step_ids.add(str(r["step_id"]))
                        step_cursor = (r["timestamp"], r["step_id"])
                        yield _step_row_to_frame(run_id_str, r, next_step_index)
                        next_step_index += 1

                    if run_row and run_row["status"] in ("completed", "done"):
                        # Drain any events buffered during history fetch before emitting done
                        while not queue.empty():
                            try:
                                buffered = queue.get_nowait()
                                b_type = buffered.get("type", "message")
                                b_step_id = buffered.get("step_id")
                                if b_step_id and b_type in _SSE_PERSISTED_STEP_EVENT_TYPES:
                                    b_step_id_str = str(b_step_id)
                                    if b_step_id_str not in emitted_step_ids:
                                        emitted_step_ids.add(b_step_id_str)
                                        buffered = {**buffered, "step_index": next_step_index}
                                        next_step_index += 1
                                        yield {"event": b_type, "data": json.dumps(buffered)}
                                elif b_type == "status_change" and buffered.get("status") in ("completed", "done"):
                                    last_status = buffered["status"]
                            except asyncio.QueueEmpty:
                                break
                        yield _done_frame(run_id_str, last_status or "completed")
                        emitted_done = True
            except Exception as db_err:
                # Falling through to the live loop keeps buffered events flowing
                # instead of leaving them stranded in the queue.
                logger.warning(f"Error fetching historical steps for SSE stream {run_id_str}: {db_err}")

            async def _reconcile_durable_state() -> List[Dict[str, Any]]:
                """Return frames for durable steps and status newer than what was delivered.

                Reads through the same cursor the replay and the live path advance,
                so neither of them can produce a duplicate delivery.
                """
                nonlocal step_cursor, last_status, next_step_index, emitted_done
                frames: List[Dict[str, Any]] = []
                pool = await get_db_pool()
                async with pool.acquire() as conn:
                    try:
                        status = await conn.fetchval(
                            "SELECT status FROM agent_runs WHERE run_id = $1;",
                            run_id,
                        )
                        status_changed = status is not None and status != last_status
                        if step_cursor is None:
                            # Nothing has been replayed (no steps yet, or the replay
                            # above failed), so the whole list is still unread.
                            rows = await conn.fetch(_SSE_STEPS_ALL_SQL, run_id)
                        else:
                            rows = await conn.fetch(
                                _SSE_STEPS_AFTER_CURSOR_SQL,
                                run_id,
                                step_cursor[0],
                                step_cursor[1],
                            )
                    except asyncio.CancelledError:
                        # Terminate rather than return a connection that was
                        # cancelled mid-query. Pool release shields itself from
                        # cancellation and then blocks on asyncpg's
                        # cancellation wait and connection reset, which have no
                        # bound here because the pool sets no command_timeout --
                        # so asyncio.wait_for would wait on that cleanup instead
                        # of returning, and the bound above would not bound
                        # anything. A terminated connection is released
                        # immediately and the pool opens a replacement. Nothing is
                        # lost: these are autocommit SELECTs with no open
                        # transaction.
                        conn.terminate()
                        raise
                # Every await this reconcile depends on has returned. Only now
                # advance the status and the cursor: doing either inside the
                # block above would consume state whose frame is thrown away
                # with the local list if the read times out or fails, silently
                # dropping a status_change or skipping the rows behind the cursor.
                if status_changed:
                    last_status = status
                    frames.append(
                        _status_change_frame(
                            run_id_str, status, datetime.now(timezone.utc).isoformat()
                        )
                    )
                for r in rows:
                    step_cursor = (r["timestamp"], r["step_id"])
                    step_id = str(r["step_id"])
                    if step_id in emitted_step_ids:
                        continue
                    emitted_step_ids.add(step_id)
                    frames.append(_step_row_to_frame(run_id_str, r, next_step_index))
                    next_step_index += 1
                if status in ("completed", "done") and not emitted_done:
                    frames.append(_done_frame(run_id_str, status))
                    emitted_done = True
                return frames

            # 2. Live hub delivery plus durable-state reconciliation. The hub is
            # process-local, so a stream opened in a different container from the
            # one executing the run has an empty subscriber set: without the poll
            # it would receive nothing but pings, never error, never reconnect,
            # and sit on the run's last state indefinitely. The wait below ends
            # at whichever deadline comes first so the queue and the poll each
            # get their turn.
            poll_deadline = time.monotonic() + SSE_DURABLE_POLL_INTERVAL_SECONDS
            ping_deadline = time.monotonic() + SSE_PING_INTERVAL_SECONDS
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        # 0.001 rather than 0: a non-positive timeout would abandon
                        # an already-queued event instead of delivering it.
                        timeout=max(min(poll_deadline, ping_deadline) - time.monotonic(), 0.001),
                    )
                    event_type = event.get("type", "message")
                    live_step_id = event.get("step_id")
                    if live_step_id and event_type in _SSE_PERSISTED_STEP_EVENT_TYPES:
                        live_step_id = str(live_step_id)
                        if live_step_id in emitted_step_ids:
                            # The agent persists a step before it publishes the
                            # live event, so the cursor poll can read and emit
                            # that row first and the hub copy lands afterwards.
                            # The step is already on this stream, so this copy is
                            # dropped here instead of being left to the
                            # frontend's dedupe.
                            continue
                        emitted_step_ids.add(live_step_id)
                        # Live events carry the agent's own loop counter while
                        # the replay and the poll number the rows they deliver
                        # from this stream's counter. Reassign so a single
                        # stream never mixes the two schemes and the displayed
                        # ordinals stay monotonic.
                        event = {**event, "step_index": next_step_index}
                        next_step_index += 1
                    if event_type == "status_change" and event.get("status"):
                        if emitted_done and event.get("status") not in ("completed", "done"):
                            # Discard stale non-terminal status event buffered before terminal completion
                            continue
                        last_status = event["status"]
                    if event_type == "done":
                        if emitted_done:
                            continue
                        emitted_done = True
                    yield {
                        "event": event_type,
                        "data": json.dumps(event)
                    }
                    continue
                except asyncio.TimeoutError:
                    pass

                if time.monotonic() >= poll_deadline:
                    try:
                        # Bounded like the queue wait above. A stalled reconcile
                        # read must not hold the generator open: it would stop the
                        # ping below and turn a database hiccup into a silently
                        # frozen stream.
                        reconcile = await asyncio.wait_for(
                            _reconcile_durable_state(),
                            timeout=SSE_RECONCILE_TIMEOUT_SECONDS,
                        )
                        for frame in reconcile:
                            yield frame
                    except asyncio.TimeoutError:
                        # Logged apart from a read failure because the two mean
                        # different things operationally: a timeout is the bound
                        # above firing on a stalled connection, not a query that
                        # returned an error. The cursor is untouched either way, so
                        # the next deadline re-reads the gap.
                        logger.warning(
                            f"Timed out reconciling durable state for SSE stream {run_id_str} "
                            f"after {SSE_RECONCILE_TIMEOUT_SECONDS}s"
                        )
                    except Exception as db_err:
                        # A transient read must not close the stream. The cursor is
                        # left untouched, so the next deadline re-reads the gap.
                        logger.warning(
                            f"Error reconciling durable state for SSE stream {run_id_str}: {db_err}"
                        )
                    poll_deadline = time.monotonic() + SSE_DURABLE_POLL_INTERVAL_SECONDS
                if time.monotonic() >= ping_deadline:
                    # Keep-alive ping event / comment
                    yield {
                        "event": "ping",
                        "data": json.dumps({
                            "type": "ping",
                            "run_id": run_id_str,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                    }
                    ping_deadline = time.monotonic() + SSE_PING_INTERVAL_SECONDS
        finally:
            run_event_hub.unsubscribe(run_id_str, queue)

    return EventSourceResponse(event_generator())



