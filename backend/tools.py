import asyncio
import base64
import logging
import re
import uuid
from decimal import Decimal
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import asyncpg
from pydantic import BaseModel, Field, ConfigDict
from playwright.async_api import Page, Locator, Error as PlaywrightError

from backend.config import settings
from backend.db import get_db_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-target / session-lost classifier (Issue #31)
# ---------------------------------------------------------------------------

CLOSED_TARGET_SUBSTRINGS = (
    "target page, context or browser has been closed",
    "context or browser has been closed",
    "target closed",
    "session closed",
    "browser has been closed",
    "browser has disconnected",
    "browser disconnected",
    "connection closed",
    "websocket is not open",
    "websocket closed",
)


def is_session_lost_error(err: Any) -> bool:
    """Return True when an error string/exception indicates mid-run CDP eviction."""
    if err is None:
        return False
    try:
        msg = str(err).lower()
    except Exception:
        return False
    if not msg:
        return False
    return any(pat in msg for pat in CLOSED_TARGET_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Strict Pydantic Schemas for Tool Inputs & DTOs
# ---------------------------------------------------------------------------

class NavigateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., description="Target URL to navigate to (absolute or relative to ERP base URL)")


class ReadPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClickInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(..., description="Accessibility label, button/link name, or CSS selector")


class FillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(..., description="Form field label, placeholder, name, or CSS selector")
    value: Union[str, int, float] = Field(..., description="Value to enter into the input field")


class SelectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(..., description="Dropdown label, placeholder, or CSS selector")
    value: str = Field(..., description="Option label or value to select")


class TakeScreenshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: Optional[str] = Field(None, description="UUID of the active agent run to link step screenshot")
    step_id: Optional[str] = Field(None, description="UUID of specific agent step to update")
    action: Optional[str] = Field("take_screenshot", description="Action name associated with this screenshot")
    result: Optional[str] = Field(None, description="Result or status note for the step")


class CheckExistsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(..., description="Entity type: 'invoice', 'purchase_order', or 'vendor'")
    identifier: str = Field(..., description="Identifier to check (UUID, PO number, or vendor name)")


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# OpenAI / Groq Tool Definitions (JSON Schemas for LLM Function Calling)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a specific URL (supports absolute URLs or relative paths like '/invoices/new').",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Destination URL (e.g. '/invoices', '/invoices/new', '/purchase-orders').",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Dump the current page accessibility tree snapshot (~2-5 KB) to inspect headings, buttons, forms, and tables.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an interactive element by accessibility label (e.g. 'Create Invoice', 'Cancel'), text, or CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Accessibility name/label, button text, or CSS selector of the element to click.",
                    }
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Fill a form input field by its field label, placeholder, or CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Field label (e.g. 'Amount', 'PO Number', 'Invoice Date') or input selector.",
                    },
                    "value": {
                        "type": "string",
                        "description": "The value to fill into the input field.",
                    },
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": "Select an option in a dropdown / select element by label or value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Dropdown label (e.g. 'Vendor') or select selector.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Option display text or value to select (e.g. 'Acme Corp').",
                    },
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture page screenshot as base64 and store in Neon agent_steps (used on failure or decision points).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action or reason for capturing screenshot.",
                    },
                    "result": {
                        "type": "string",
                        "description": "Outcome or failure note to associate with the step record.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_exists",
            "description": "Idempotency check before creating any record. Verifies whether an invoice or purchase order already exists in Neon DB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": ["invoice", "purchase_order", "vendor"],
                        "description": "Type of entity to check for existence.",
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier to look up (PO number e.g. 'PO-1002', invoice UUID, or vendor name).",
                    },
                },
                "required": ["entity_type", "identifier"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Smart Locator Resolution Helper
# ---------------------------------------------------------------------------

async def resolve_locator(
    page: Page,
    selector: str,
    target_type: str = "any",
    timeout_ms: int = 5000,
) -> Locator:
    """
    Intelligently resolves an element locator from an accessibility label,
    placeholder, role name, or CSS/XPath selector without throwing on special characters.
    """
    clean = selector.strip()
    is_explicit_selector = (
        clean.startswith("#")
        or clean.startswith(".")
        or clean.startswith("//")
        or clean.startswith("xpath=")
        or clean.startswith("css=")
        or clean.startswith("text=")
        or ("[" in clean and "]" in clean)
    )

    if is_explicit_selector:
        try:
            loc = page.locator(clean)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # Clean off trailing asterisks or hints
    clean_no_star = re.sub(r"[\*]+$", "", clean).strip()

    # 1. Actionable Click Elements (buttons, links)
    if target_type in ("button", "link", "any"):
        for candidate in (clean, clean_no_star):
            btn_loc = page.get_by_role("button", name=candidate, exact=False)
            if await btn_loc.count() > 0:
                return btn_loc.first

            link_loc = page.get_by_role("link", name=candidate, exact=False)
            if await link_loc.count() > 0:
                return link_loc.first

    # 2. Form Input Elements (textboxes, spinbuttons, inputs)
    if target_type in ("input", "any"):
        for candidate in (clean, clean_no_star):
            lbl_loc = page.get_by_label(candidate, exact=False)
            if await lbl_loc.count() > 0:
                return lbl_loc.first

            ph_loc = page.get_by_placeholder(candidate, exact=False)
            if await ph_loc.count() > 0:
                return ph_loc.first

            tb_loc = page.get_by_role("textbox", name=candidate, exact=False)
            if await tb_loc.count() > 0:
                return tb_loc.first

            sb_loc = page.get_by_role("spinbutton", name=candidate, exact=False)
            if await sb_loc.count() > 0:
                return sb_loc.first

    # 3. Dropdowns (comboboxes / selects)
    if target_type in ("select", "any"):
        for candidate in (clean, clean_no_star):
            sel_lbl = page.get_by_label(candidate, exact=False)
            if await sel_lbl.count() > 0:
                return sel_lbl.first

            combo_loc = page.get_by_role("combobox", name=candidate, exact=False)
            if await combo_loc.count() > 0:
                return combo_loc.first

    # 4. Text Content Matching
    for candidate in (clean, clean_no_star):
        txt_loc = page.get_by_text(candidate, exact=False)
        if await txt_loc.count() > 0:
            return txt_loc.first

    # 5. ID / Name match fallback
    safe_id = re.sub(r"[^\w\-]", "", clean_no_star)
    if safe_id:
        try:
            attr_loc = page.locator(f"#{safe_id}, [name='{safe_id}']")
            if await attr_loc.count() > 0:
                return attr_loc.first
        except Exception:
            pass

    # 6. Fallback: try page.locator with clean, or get_by_text
    try:
        loc = page.locator(clean)
        if await loc.count() > 0:
            return loc.first
    except Exception:
        pass

    return page.get_by_text(clean).first


# ---------------------------------------------------------------------------
# Individual Tool Implementations
# ---------------------------------------------------------------------------

async def navigate(page: Page, url: str) -> Dict[str, Any]:
    """
    Navigate the browser to a destination URL. Resolves relative paths
    against ERP_BASE_URL / FRONTEND_URL / NEXT_PUBLIC_API_URL or default http://localhost:3051.
    """
    try:
        target_url = url.strip()
        if target_url.startswith("/"):
            base = settings.ERP_BASE_URL or settings.FRONTEND_URL or settings.NEXT_PUBLIC_API_URL or "http://localhost:3051"
            target_url = urljoin(base, target_url)

        logger.info(f"Tool navigate: heading to {target_url}")
        response = await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        current_url = page.url
        title = await page.title()
        status_code = response.status if response else 200

        return {
            "success": True,
            "url": current_url,
            "title": title,
            "status": status_code,
        }
    except Exception as e:
        logger.error(f"Tool navigate failed for {url}: {e}")
        return {
            "success": False,
            "url": page.url if page else url,
            "error": str(e),
            "session_lost": is_session_lost_error(e),
        }


async def read_page(page: Page) -> Dict[str, Any]:
    """
    Dump the current page accessibility tree snapshot (~2-5 KB) using Playwright aria_snapshot().
    """
    try:
        snapshot = await page.aria_snapshot()
        current_url = page.url
        title = await page.title()
        size_bytes = len(snapshot.encode("utf-8")) if snapshot else 0

        logger.info(f"Tool read_page: captured accessibility snapshot ({size_bytes} bytes)")
        return {
            "success": True,
            "url": current_url,
            "title": title,
            "snapshot": snapshot,
            "size_bytes": size_bytes,
        }
    except Exception as e:
        logger.error(f"Tool read_page failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "session_lost": is_session_lost_error(e),
        }


async def click(page: Page, selector: str) -> Dict[str, Any]:
    """
    Click an interactive element by accessibility label, button/link name, or selector.
    Verifies element attachment and visibility before retrying with force=True on click failure.
    Uses no_wait_after=True to prevent waiting for navigation on non-navigating submit/button actions.
    """
    try:
        logger.info(f"Tool click: resolving selector '{selector}'")
        locator = await resolve_locator(page, selector, target_type="button")
        clicked_successfully = False
        try:
            await locator.click(timeout=5000, no_wait_after=True)
            clicked_successfully = True
        except Exception as ce:
            logger.warning(f"Standard click failed on selector '{selector}': {ce}")
            err_str = str(ce).lower()
            is_actionability_failure = (
                isinstance(ce, TimeoutError) or 
                "timeout" in err_str or 
                "not visible" in err_str or 
                "disabled" in err_str or 
                "intercepted" in err_str
            )

            if not is_actionability_failure:
                logger.error(f"Click failure on selector '{selector}' is not an actionability failure (dispatch status ambiguous); propagating error.")
                raise ce

            # Verify element is attached and visible before considering force retry
            is_attached = await locator.count() > 0
            is_visible = False
            if is_attached:
                try:
                    is_visible = await locator.is_visible()
                except Exception:
                    pass

            if is_attached and is_visible:
                logger.info(f"Retrying click with force=True on verified attached/visible element '{selector}'")
                try:
                    await locator.click(force=True, timeout=5000, no_wait_after=True)
                    clicked_successfully = True
                except Exception as fe:
                    logger.error(f"Forced click failed on selector '{selector}': {fe}")
                    raise fe
            else:
                logger.error(f"Element for selector '{selector}' is not attached or not visible; aborting forced click retry.")
                raise ce

        if not clicked_successfully:
            raise RuntimeError(f"Click operation on selector '{selector}' did not complete successfully.")

        # Small settle delay for DOM transitions
        await asyncio.sleep(0.2)

        return {
            "success": True,
            "selector": selector,
            "clicked": True,
            "url_after": page.url,
        }
    except Exception as e:
        logger.error(f"Tool click failed on selector '{selector}': {e}")
        return {
            "success": False,
            "selector": selector,
            "clicked": False,
            "error": str(e),
            "session_lost": is_session_lost_error(e),
        }


async def fill(page: Page, selector: str, value: Union[str, int, float]) -> Dict[str, Any]:
    """
    Fill a form input field by accessibility label, placeholder, or selector.
    """
    try:
        val_str = str(value)
        logger.info(f"Tool fill: locating field '{selector}' with value '{val_str}'")
        locator = await resolve_locator(page, selector, target_type="input")
        await locator.fill(val_str, timeout=10000)

        return {
            "success": True,
            "selector": selector,
            "value": val_str,
        }
    except Exception as e:
        logger.error(f"Tool fill failed on selector '{selector}': {e}")
        return {
            "success": False,
            "selector": selector,
            "value": str(value),
            "error": str(e),
            "session_lost": is_session_lost_error(e),
        }


async def select(page: Page, selector: str, value: str) -> Dict[str, Any]:
    """
    Select an option in a dropdown / select element by label or value.
    """
    try:
        val_str = str(value).strip()
        logger.info(f"Tool select: locating dropdown '{selector}' with option '{val_str}'")
        locator = await resolve_locator(page, selector, target_type="select")

        # Attempt selection by visible label first, fallback to value attribute or text
        try:
            await locator.select_option(label=val_str, timeout=5000)
        except Exception:
            try:
                await locator.select_option(value=val_str, timeout=5000)
            except Exception:
                await locator.select_option(val_str, timeout=5000)

        return {
            "success": True,
            "selector": selector,
            "selected": val_str,
        }
    except Exception as e:
        logger.error(f"Tool select failed on selector '{selector}': {e}")
        return {
            "success": False,
            "selector": selector,
            "selected": None,
            "error": str(e),
            "session_lost": is_session_lost_error(e),
        }



async def take_screenshot(
    page: Page,
    run_id: Optional[str] = None,
    step_id: Optional[str] = None,
    action: Optional[str] = "take_screenshot",
    result: Optional[str] = None,
    pool: Optional[asyncpg.Pool] = None,
) -> Dict[str, Any]:
    """
    Capture page screenshot as base64. If run_id is supplied, persist to Neon agent_steps table.
    """
    try:
        img_bytes = await page.screenshot(type="png", full_page=False)
        b64_str = base64.b64encode(img_bytes).decode("utf-8")
        persisted = False
        saved_step_id: Optional[str] = step_id

        if run_id:
            try:
                db_pool = pool if pool is not None else await get_db_pool()
                async with db_pool.acquire() as conn:
                    # Ensure agent_run exists so foreign key references are valid.
                    # The row is written in the form the lease scheme owns
                    # (attempt >= 1, no live lease) rather than as a bare
                    # leaseless 'running' row: the startup orphan sweep only
                    # reclaims status = 'running' rows with attempt > 0, so a
                    # bare row would sit 'running' forever, invisible to both
                    # the sweep and the owner-fenced status writes. With
                    # attempt = 1 and a NULL lease the sweep can reclaim it as
                    # failed (with an 'orphaned' step explaining why), and a
                    # real execution's ensure_run_record can still claim it,
                    # since that upsert takes over exactly NULL/expired leases.
                    # Existing rows are never touched (DO NOTHING): a live
                    # owner holding this run must not lose its lease to a
                    # screenshot.
                    run_uuid = uuid.UUID(str(run_id))
                    await conn.execute(
                        """
                        INSERT INTO agent_runs (run_id, goal, status, owner_id, lease_expires_at, attempt, created_at)
                        VALUES ($1, 'Agent Execution Run', 'running', NULL, NULL, 1, now())
                        ON CONFLICT (run_id) DO NOTHING;
                        """,
                        run_uuid,
                    )

                    if saved_step_id:
                        # The caller owns this step's identity: persist the row
                        # under exactly that step_id whether or not it exists
                        # yet, so the durable row and the live SSE event for the
                        # step can be correlated. An existing row (the failure
                        # screenshot path attaches to one) keeps its action and
                        # result and only gains the screenshot.
                        step_uuid = uuid.UUID(str(saved_step_id))
                        new_step_id = await conn.fetchval(
                            """
                            INSERT INTO agent_steps (step_id, run_id, action, result, screenshot_b64)
                            VALUES ($1, $2, $3, COALESCE($4::text, 'screenshot captured'), $5)
                            ON CONFLICT (step_id) DO UPDATE SET
                                screenshot_b64 = EXCLUDED.screenshot_b64,
                                result = COALESCE($4::text, agent_steps.result)
                            RETURNING step_id;
                            """,
                            step_uuid,
                            run_uuid,
                            action or "take_screenshot",
                            result,
                            b64_str,
                        )
                        saved_step_id = str(new_step_id)
                        persisted = True
                    else:
                        new_step_id = await conn.fetchval(
                            """
                            INSERT INTO agent_steps (run_id, action, result, screenshot_b64)
                            VALUES ($1, $2, $3, $4)
                            RETURNING step_id;
                            """,
                            run_uuid,
                            action or "take_screenshot",
                            result or "screenshot captured",
                            b64_str,
                        )
                        saved_step_id = str(new_step_id)
                        persisted = True
            except Exception as dbe:
                logger.warning(f"Screenshot taken but failed to persist to Neon DB: {dbe}")

        return {
            "success": True,
            "screenshot_b64": b64_str,
            "size_bytes": len(img_bytes),
            "step_id": saved_step_id,
            "persisted": persisted,
        }
    except Exception as e:
        logger.error(f"Tool take_screenshot failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "session_lost": is_session_lost_error(e),
        }


def _serialize_db_record(record: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
    """Helper to convert asyncpg record containing UUID, Decimal, and Dates into clean JSON-serializable dict."""
    if not record:
        return None
    d = dict(record)
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif isinstance(v, Decimal):
            d[k] = float(v)
        elif isinstance(v, (date, datetime)):
            d[k] = v.isoformat()
    return d


async def check_exists(
    entity_type: str,
    identifier: str,
    pool: Optional[asyncpg.Pool] = None,
) -> Dict[str, Any]:
    """
    Idempotency check before creating any record.
    Returns {"exists": True, "entity_type": ..., "identifier": ..., "record": ...}
    or {"exists": False, "entity_type": ..., "identifier": ..., "record": None}.
    """
    try:
        norm_type = entity_type.strip().lower()
        clean_id = identifier.strip()
        db_pool = pool if pool is not None else await get_db_pool()

        # Check if clean_id is a valid UUID
        is_uuid = False
        parsed_uuid = None
        try:
            parsed_uuid = uuid.UUID(clean_id)
            is_uuid = True
        except (ValueError, TypeError, AttributeError):
            is_uuid = False

        async with db_pool.acquire() as conn:
            if norm_type in ("invoice", "invoices"):
                if is_uuid:
                    row = await conn.fetchrow(
                        """
                        SELECT id, vendor, amount, date, po_number, status, created_at
                        FROM invoices
                        WHERE id = $1 OR po_number = $2
                        LIMIT 1;
                        """,
                        parsed_uuid,
                        clean_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT id, vendor, amount, date, po_number, status, created_at
                        FROM invoices
                        WHERE po_number = $1 OR LOWER(vendor) = LOWER($1)
                        ORDER BY created_at DESC
                        LIMIT 1;
                        """,
                        clean_id,
                    )

                exists = row is not None
                return {
                    "exists": exists,
                    "entity_type": "invoice",
                    "identifier": clean_id,
                    "record": _serialize_db_record(row) if exists else None,
                }

            elif norm_type in ("purchase_order", "po", "purchase_orders"):
                row = await conn.fetchrow(
                    """
                    SELECT po_number, vendor, approved_amount, status
                    FROM purchase_orders
                    WHERE po_number = $1 OR LOWER(vendor) = LOWER($1)
                    LIMIT 1;
                    """,
                    clean_id,
                )
                exists = row is not None
                return {
                    "exists": exists,
                    "entity_type": "purchase_order",
                    "identifier": clean_id,
                    "record": _serialize_db_record(row) if exists else None,
                }

            elif norm_type in ("vendor", "vendors"):
                row = await conn.fetchrow(
                    """
                    SELECT vendor FROM (
                        SELECT vendor FROM purchase_orders WHERE LOWER(vendor) = LOWER($1)
                        UNION
                        SELECT vendor FROM invoices WHERE LOWER(vendor) = LOWER($1)
                    ) sub
                    LIMIT 1;
                    """,
                    clean_id,
                )
                exists = row is not None
                return {
                    "exists": exists,
                    "entity_type": "vendor",
                    "identifier": clean_id,
                    "record": {"vendor": row["vendor"]} if exists else None,
                }

            else:
                return {
                    "exists": False,
                    "entity_type": entity_type,
                    "identifier": clean_id,
                    "error": f"Unsupported entity_type: '{entity_type}'. Must be 'invoice', 'purchase_order', or 'vendor'.",
                    "record": None,
                }

    except Exception as e:
        logger.error(f"Tool check_exists failed for {entity_type} {identifier}: {e}")
        return {
            "exists": False,
            "entity_type": entity_type,
            "identifier": identifier,
            "error": str(e),
            "record": None,
        }


# ---------------------------------------------------------------------------
# PlaywrightTools Class Interface
# ---------------------------------------------------------------------------

class PlaywrightTools:
    """
    High-level agent tools interface binding an active Playwright Page session,
    run ID, and database pool for ReAct loop execution.
    """

    def __init__(
        self,
        page: Optional[Page] = None,
        run_id: Optional[str] = None,
        pool: Optional[asyncpg.Pool] = None,
    ):
        self.page = page
        self.run_id = run_id
        self.pool = pool

    def set_page(self, page: Page) -> None:
        """Set the active Playwright page instance."""
        self.page = page

    def set_run_id(self, run_id: str) -> None:
        """Set the active agent run ID."""
        self.run_id = run_id

    async def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate browser page to URL."""
        if not self.page:
            return {"success": False, "error": "Browser page is not set"}
        return await navigate(self.page, url)

    async def read_page(self) -> Dict[str, Any]:
        """Capture accessibility snapshot of current page."""
        if not self.page:
            return {"success": False, "error": "Browser page is not set"}
        return await read_page(self.page)

    async def click(self, selector: str) -> Dict[str, Any]:
        """Click element matching selector."""
        if not self.page:
            return {"success": False, "error": "Browser page is not set"}
        return await click(self.page, selector)

    async def fill(self, selector: str, value: Union[str, int, float]) -> Dict[str, Any]:
        """Fill input field matching selector with value."""
        if not self.page:
            return {"success": False, "error": "Browser page is not set"}
        return await fill(self.page, selector, value)

    async def select(self, selector: str, value: str) -> Dict[str, Any]:
        """Select dropdown option matching selector."""
        if not self.page:
            return {"success": False, "error": "Browser page is not set"}
        return await select(self.page, selector, value)

    async def take_screenshot(
        self,
        step_id: Optional[str] = None,
        action: Optional[str] = "take_screenshot",
        result: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Capture screenshot and optionally persist to database."""
        if not self.page:
            return {"success": False, "error": "Browser page is not set"}
        return await take_screenshot(
            page=self.page,
            run_id=self.run_id,
            step_id=step_id,
            action=action,
            result=result,
            pool=self.pool,
        )

    async def check_exists(self, entity_type: str, identifier: str) -> Dict[str, Any]:
        """Check if entity exists in database."""
        return await check_exists(entity_type=entity_type, identifier=identifier, pool=self.pool)

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        step_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Dynamically dispatch tool execution by name and arguments (matching LLM function calls).

        `step_id` is the identity the agent loop already assigned to this step.
        take_screenshot persists a row itself, so it must persist it under this
        id rather than minting its own, otherwise the durable row and the live
        event for the same step carry different identities.
        """
        name = tool_name.strip().lower()
        if name == "navigate":
            return await self.navigate(url=arguments.get("url", ""))
        elif name == "read_page":
            return await self.read_page()
        elif name == "click":
            return await self.click(selector=arguments.get("selector", ""))
        elif name == "fill":
            return await self.fill(selector=arguments.get("selector", ""), value=arguments.get("value", ""))
        elif name == "select":
            return await self.select(selector=arguments.get("selector", ""), value=arguments.get("value", ""))
        elif name == "take_screenshot":
            return await self.take_screenshot(
                step_id=step_id if step_id is not None else arguments.get("step_id"),
                action=arguments.get("action", "take_screenshot"),
                result=arguments.get("result"),
            )
        elif name == "check_exists":
            return await self.check_exists(
                entity_type=arguments.get("entity_type", ""),
                identifier=arguments.get("identifier", ""),
            )
        else:
            return {
                "success": False,
                "error": f"Unknown tool: '{tool_name}'",
            }
