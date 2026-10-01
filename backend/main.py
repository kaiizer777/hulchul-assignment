import logging
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
        phase="Phase 2.1, 2.2 & 2.3 Complete",
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

