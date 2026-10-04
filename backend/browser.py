import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, Coroutine, Optional, Dict, Any, TypeVar
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

_T = TypeVar("_T")


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


MAX_CDP_REATTACH_ATTEMPTS: int = 2


def is_browser_session_alive(session: Optional[BrowserSession]) -> bool:
    """Synchronous liveness probe for a CDP session (no I/O)."""
    if session is None:
        return False
    try:
        page = getattr(session, "page", None)
        if page is not None and callable(getattr(page, "is_closed", None)):
            try:
                if page.is_closed():
                    return False
            except Exception:
                return False
        browser = getattr(session, "browser", None)
        if browser is not None and callable(getattr(browser, "is_connected", None)):
            try:
                if not browser.is_connected():
                    return False
            except Exception:
                return False
        return True
    except Exception:
        return False


async def check_browser_session_health(session: Optional[BrowserSession], timeout_s: float = 5.0) -> bool:
    """Mid-run health check used by the ReAct loop; True means reusable."""
    if not is_browser_session_alive(session):
        return False
    try:
        await asyncio.wait_for(session.page.evaluate("() => document.readyState"), timeout=timeout_s)
        return True
    except Exception as e:
        from backend.tools import is_session_lost_error

        if is_session_lost_error(e):
            return False
        return True


async def run_cancellation_safe(coro: Coroutine[Any, Any, _T]) -> _T:
    """
    Run a teardown coroutine to completion even while the caller is being cancelled.

    Teardown runs in `finally` blocks that a cancelled task unwinds through, and
    an agent run is cancelled rather than awaited, so awaiting the teardown
    directly leaves it interruptible: a second cancellation (a repeated cancel,
    or an enclosing wait_for timeout) aborts it mid-flight and the resource it
    was releasing is leaked. CancelledError is a BaseException, so an
    `except Exception` arm around it never sees the interruption at all.

    The work therefore runs as its own task and is awaited through shield()
    until it settles. A cancellation observed on the way is re-raised
    afterwards, so it is delayed and never swallowed: the caller still ends up
    cancelled, but only after the release has happened.
    """
    work = asyncio.ensure_future(coro)
    pending_cancel: Optional[asyncio.CancelledError] = None
    while True:
        try:
            await asyncio.shield(work)
            break
        except asyncio.CancelledError as cancel_err:
            pending_cancel = cancel_err
            if work.done():
                break
        except Exception:
            # The teardown itself failed. Surfaced below so the caller decides
            # whether that is fatal; the exception is retrieved either way, so
            # asyncio does not also report it as never-retrieved.
            break
    work_err = work.exception()
    if pending_cancel is not None:
        raise pending_cancel
    if work_err is not None:
        raise work_err
    return work.result()


async def _post_steel_session_release(api_key: str, session_id: str) -> None:
    """Issue the Steel.dev session release request. Raises on transport failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.steel.dev/v1/sessions/{session_id}/release",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 200:
        logger.info(f"Released Steel.dev session {session_id}")
    else:
        logger.warning(f"Steel.dev session release returned status {resp.status_code}: {resp.text}")


async def _release_steel_session(api_key: str, session_id: str) -> None:
    """Explicitly release a Steel.dev session via REST API.

    Guarded by run_cancellation_safe: this runs from the teardown of a task that
    is usually being cancelled, and an interrupted release leaks a remote
    browser session that then holds Steel concurrency until its own timeout.
    """
    try:
        await run_cancellation_safe(_post_steel_session_release(api_key, session_id))
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
    Establish a browser session. If a remote WebSocket endpoint (CDP) is provided or configured in
    BROWSER_WS_ENDPOINT, connects over CDP (e.g. Steel.dev or Browserless).
    Otherwise (or if endpoint is 'local'), launches a local Playwright Chromium instance for local development.
    """
    raw_endpoint = endpoint if endpoint is not None else settings.BROWSER_WS_ENDPOINT
    ws_endpoint = raw_endpoint.strip() if raw_endpoint else ""
    use_local = not ws_endpoint or ws_endpoint.lower() in ("local", "none", "unset")
    timeout = timeout_ms if timeout_ms is not None else settings.BROWSER_CONNECT_TIMEOUT_MS

    p: Optional[Playwright] = None
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    steel_api_key: Optional[str] = None
    steel_session_id: Optional[str] = None

    context_opts: Dict[str, Any] = {}
    if viewport:
        context_opts["viewport"] = viewport
    else:
        context_opts["viewport"] = {"width": 1280, "height": 800}

    if use_local:
        logger.info("Starting local Playwright Chromium browser...")
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(**context_opts)
            page = await context.new_page()

            session = BrowserSession(
                playwright=p,
                browser=browser,
                context=context,
                page=page,
                session_id=None,
            )
            yield session
        except PlaywrightError as pe:
            logger.error(f"Playwright error launching local browser: {pe}")
            raise BrowserConnectionError(f"Failed to launch local browser: {pe}") from pe
        except Exception as e:
            logger.error(f"Unexpected error in local browser session: {e}")
            raise BrowserConnectionError(f"Unexpected error in local browser session: {e}") from e
        finally:
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
                    logger.warning(f"Error closing browser: {e}")
            if p:
                try:
                    await p.stop()
                except Exception as e:
                    logger.warning(f"Error stopping Playwright runtime: {e}")
        return

    # Remote CDP path
    actual_ws_endpoint = ws_endpoint
    if ws_endpoint and ("localhost:9222" in ws_endpoint or "127.0.0.1:9222" in ws_endpoint):
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("http://localhost:9222/json/version")
                if resp.status_code == 200:
                    data = resp.json()
                    actual_ws_endpoint = data.get("webSocketDebuggerUrl", ws_endpoint)
        except Exception as e:
            logger.warning(f"Could not fetch chrome debugger version URL: {e}")

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
                        logger.warning(f"Steel.dev concurrency limit hit on attempt {attempt + 1}. Retrying with backoff...")
                        if attempt < 2:
                            await asyncio.sleep(2.0 * (attempt + 1))
                    else:
                        logger.warning(f"Steel session creation returned status {resp.status_code}: {resp.text}")
                        break
            except Exception as e:
                logger.warning(f"Error creating Steel session on attempt {attempt + 1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1.5)

    logger.info("Connecting to remote browser over CDP via %s...", actual_ws_endpoint[:25] + "...")
    try:
        p = await async_playwright().start()
        browser = await p.chromium.connect_over_cdp(actual_ws_endpoint, timeout=timeout)
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
    Connect to the remote browser over CDP or local browser, perform a probe navigation,
    and return diagnostic connectivity metrics. Includes retry for transient connection drops.
    """
    start_time = time.perf_counter()
    last_error: Optional[Exception] = None
    configured_endpoint = endpoint if endpoint is not None else settings.BROWSER_WS_ENDPOINT

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
                    "endpoint_configured": bool(configured_endpoint),
                }
        except Exception as e:
            last_error = e
            if attempt < retries:
                logger.warning(f"Browser probe attempt {attempt + 1} failed: {e}. Retrying after 2s...")
                await asyncio.sleep(2.0)

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    logger.error(f"Browser verification probe failed: {last_error}")
    return {
        "connected": False,
        "error": str(last_error),
        "latency_ms": elapsed_ms,
        "endpoint_configured": bool(configured_endpoint),
    }

