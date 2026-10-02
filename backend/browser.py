import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, Optional, Dict, Any
from urllib.parse import urlparse, parse_qs
import httpx
from playwright.async_api import (
    async_playwright,
    Playwright,
    Browser,
    BrowserContext,
    Page,
    Error as PlaywrightError,
)
from backend.config import settings

logger = logging.getLogger(__name__)


class BrowserConnectionError(Exception):
    """Raised when connecting to remote browser over CDP fails."""
    pass


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    session_id: Optional[str] = None


async def _release_steel_session(api_key: str, session_id: str) -> None:
    """Explicitly release a Steel.dev session via REST API."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.steel.dev/v1/sessions/{session_id}/release",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                logger.info(f"Released Steel.dev session {session_id}")
            else:
                logger.warning(f"Steel.dev session release returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"Failed to release Steel.dev session {session_id}: {e}")


async def _cleanup_stale_steel_sessions(api_key: str) -> None:
    """Clean up any abandoned live sessions on Steel.dev to ensure available concurrency."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.steel.dev/v1/sessions",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                for s in data.get("sessions", []):
                    if s.get("status") == "live":
                        sid = s.get("id")
                        if sid:
                            logger.info(f"Cleaning up stale Steel session {sid}")
                            await client.post(
                                f"https://api.steel.dev/v1/sessions/{sid}/release",
                                headers={"Authorization": f"Bearer {api_key}"},
                            )
    except Exception as e:
        logger.warning(f"Failed checking stale Steel sessions: {e}")


@asynccontextmanager
async def get_browser_session(
    endpoint: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    viewport: Optional[Dict[str, int]] = None,
) -> AsyncGenerator[BrowserSession, None]:
    """
    Establish a connection to Browserless / Steel.dev over Chrome DevTools Protocol (CDP).
    
    Lambda is stateless and cannot bundle full browser binaries; this context manager
    connects to the remote WebSocket endpoint on demand and guarantees clean teardown
    of pages, contexts, browser connections, and the Playwright driver.
    """
    ws_endpoint = endpoint if endpoint is not None else settings.BROWSER_WS_ENDPOINT
    if not ws_endpoint:
        raise BrowserConnectionError("BROWSER_WS_ENDPOINT is not configured in environment or .env")

    timeout = timeout_ms if timeout_ms is not None else settings.BROWSER_CONNECT_TIMEOUT_MS

    p: Optional[Playwright] = None
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    steel_api_key: Optional[str] = None
    steel_session_id: Optional[str] = None
    actual_ws_endpoint = ws_endpoint

    # Detect Steel.dev endpoint to handle isolated session lifecycle and release
    parsed = urlparse(ws_endpoint)
    is_steel = "steel.dev" in (parsed.netloc or "")
    if is_steel:
        qs = parse_qs(parsed.query)
        api_key_list = qs.get("apiKey")
        if api_key_list:
            steel_api_key = api_key_list[0]

    if is_steel and steel_api_key:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.steel.dev/v1/sessions",
                        headers={"Authorization": f"Bearer {steel_api_key}", "Content-Type": "application/json"},
                        json={"timeout": 120000},
                    )
                    if resp.status_code in (200, 201):
                        session_data = resp.json()
                        steel_session_id = session_data.get("id")
                        actual_ws_endpoint = session_data.get("websocketUrl", ws_endpoint)
                        logger.info(f"Created dedicated Steel.dev session {steel_session_id}")
                        break
                    elif resp.status_code == 429:
                        logger.warning(f"Steel.dev concurrency limit hit on attempt {attempt + 1}. Cleaning up stale sessions...")
                        await _cleanup_stale_steel_sessions(steel_api_key)
                        await asyncio.sleep(2.0 * (attempt + 1))
                    else:
                        logger.warning(f"Steel session creation returned status {resp.status_code}: {resp.text}")
                        break
            except Exception as e:
                logger.warning(f"Error creating Steel session on attempt {attempt + 1}: {e}")
                await asyncio.sleep(1.5)

    logger.info("Connecting to remote browser over CDP via %s...", actual_ws_endpoint[:25] + "...")
    try:
        p = await async_playwright().start()
        browser = await p.chromium.connect_over_cdp(actual_ws_endpoint, timeout=timeout)
        
        # Create an isolated browser context per session
        context_opts: Dict[str, Any] = {}
        if viewport:
            context_opts["viewport"] = viewport
        else:
            context_opts["viewport"] = {"width": 1280, "height": 800}

        context = await browser.new_context(**context_opts)
        page = await context.new_page()

        session = BrowserSession(
            playwright=p,
            browser=browser,
            context=context,
            page=page,
            session_id=steel_session_id,
        )
        yield session

    except PlaywrightError as pe:
        logger.error(f"Playwright error during CDP connection: {pe}")
        raise BrowserConnectionError(f"Failed to connect to remote browser over CDP: {pe}") from pe
    except Exception as e:
        logger.error(f"Unexpected error during CDP browser session: {e}")
        raise BrowserConnectionError(f"Unexpected error in CDP browser session: {e}") from e
    finally:
        # Guarantee teardown of all allocated resources
        if page:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")

        if context:
            try:
                await context.close()
            except Exception as e:
                logger.warning(f"Error closing context: {e}")

        if browser:
            try:
                if browser.is_connected():
                    await browser.close()
            except Exception as e:
                logger.warning(f"Error closing CDP browser connection: {e}")

        if p:
            try:
                await p.stop()
            except Exception as e:
                logger.warning(f"Error stopping Playwright runtime: {e}")

        # If a Steel.dev session was created, guarantee release so concurrency is never blocked
        if steel_api_key and steel_session_id:
            await _release_steel_session(steel_api_key, steel_session_id)


async def verify_cdp_connection(
    endpoint: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    retries: int = 1,
) -> Dict[str, Any]:
    """
    Connect to the remote browser over CDP, perform a probe navigation,
    and return diagnostic connectivity metrics. Includes retry for transient connection drops.
    """
    start_time = time.perf_counter()
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            async with get_browser_session(endpoint=endpoint, timeout_ms=timeout_ms) as session:
                browser_name = session.browser.browser_type.name
                await session.page.goto("about:blank")
                title = await session.page.title()
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                
                return {
                    "connected": True,
                    "browser_type": browser_name,
                    "blank_page_title": title,
                    "latency_ms": elapsed_ms,
                    "endpoint_configured": bool(endpoint or settings.BROWSER_WS_ENDPOINT),
                }
        except Exception as e:
            last_error = e
            if attempt < retries:
                logger.warning(f"CDP probe attempt {attempt + 1} failed: {e}. Retrying after 2s...")
                await asyncio.sleep(2.0)

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    logger.error(f"CDP verification probe failed: {last_error}")
    return {
        "connected": False,
        "error": str(last_error),
        "latency_ms": elapsed_ms,
        "endpoint_configured": bool(endpoint or settings.BROWSER_WS_ENDPOINT),
    }

