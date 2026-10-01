import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import asyncpg
from groq import AsyncGroq
from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings
from backend.db import get_db_pool
from backend.redis_client import UpstashRedisClient, get_redis_client
from backend.tools import (
    PlaywrightTools,
    TOOL_DEFINITIONS,
    take_screenshot,
    check_exists,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goal Extraction Helpers (Thresholds & Filters)
# ---------------------------------------------------------------------------

def extract_approval_threshold(goal: str, default: float = 50000.0) -> float:
    """
    Extracts the monetary approval threshold from a plain-English goal string.
    Examples:
      - "Hold anything over ₹25,000 for approval" -> 25000.0
      - "Approve invoices under Rs 60,000" -> 60000.0
      - "Process all with default ₹50,000 threshold" -> 50000.0
    """
    if not goal:
        return default

    # Match patterns like:
    #   over ₹25,000 / above ₹25000 / over 25000 / Rs. 25,000 / INR 25,000 / ₹ 50,000
    patterns = [
        r"(?:over|above|exceeding|threshold\s+(?:of|at)?|greater\s+than)\s*(?:₹|rs\.?|inr|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
        r"(?:₹|rs\.?|inr|\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
    ]

    for pat in patterns:
        match = re.search(pat, goal, re.IGNORECASE)
        if match:
            raw_num = match.group(1).replace(",", "").strip()
            try:
                val = float(raw_num)
                if val > 0:
                    return val
            except ValueError:
                continue

    return default


def extract_vendor_filter(goal: str) -> Optional[str]:
    """
    Extracts vendor filter constraint if specified in goal (Phase 5 variation).
    Example: 'Process only invoices from Vendor Acme' -> 'Acme'
    """
    if not goal:
        return None

    match = re.search(
        r"(?:from\s+vendor|vendor|from)\s+([A-Za-z0-9\s\-]+?)(?:\s+(?:only|invoices|\.|$)|$)",
        goal,
        re.IGNORECASE,
    )
    if match:
        v = match.group(1).strip()
        v = re.sub(r"^vendor\s+", "", v, flags=re.IGNORECASE).strip()
        if v.lower() not in ("all", "any", "the", "new", "each", ""):
            return v
    return None


def parse_amount(amount: Union[float, int, str, None]) -> float:
    """
    Extracts the numeric monetary value from a float, int, or currency-prefixed string.
    Correctly ignores punctuation in currency prefixes (e.g. 'Rs. 65,000' -> 65000.0).
    """
    if amount is None:
        return 0.0
    if isinstance(amount, (int, float)):
        return float(amount)
    try:
        m = re.search(r"\d[\d,]*(?:\.\d+)?", str(amount))
        return float(m.group(0).replace(",", "")) if m else 0.0
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# ReAct System Prompt Builder
# ---------------------------------------------------------------------------

def build_system_prompt(goal: str, threshold: float = 50000.0, vendor_filter: Optional[str] = None) -> str:
    """
    Builds the system prompt defining the agent's role, available tools,
    approval threshold extracted from the goal, and mandatory idempotency rules.
    """
    vendor_instruction = (
        f"- Target Vendor Filter: Only process invoices for vendor '{vendor_filter}'."
        if vendor_filter
        else "- Process all target invoices as specified in the goal."
    )

    return f"""You are an autonomous ERP Invoice Processing Agent operating a web-based ERP system.
Your mission is to execute the user's goal with precision, safety, and verifiable evidence.

## Available Tools
1. navigate(url): Go to an ERP URL (e.g. '/invoices', '/invoices/new', '/purchase-orders', '/vendors').
2. read_page(): Inspect current page accessibility tree snapshot to discover interactive elements, buttons, and form inputs.
3. click(selector): Click buttons, links, or controls using accessibility label, text, or selector.
4. fill(selector, value): Fill input fields by label or selector (e.g. 'Amount', 'PO Number', 'Vendor').
5. select(selector, value): Select dropdown options by label or value.
6. take_screenshot(action, result): Capture evidence screenshot buffer and persist to database.
7. check_exists(entity_type, identifier): Idempotency check before creating any invoice or purchase order.

## Operational Rules (Mandatory)
1. MANDATORY IDEMPOTENCY: Before creating or submitting any new invoice, you MUST call check_exists(entity_type='invoice', identifier=<po_number or id>). If the record already exists, DO NOT duplicate it; skip or flag it.
2. APPROVAL GATE: The human approval threshold for this task is ₹{threshold:,.2f}.
   If an invoice amount exceeds ₹{threshold:,.2f}, DO NOT submit the form. State that it requires approval with the invoice details (vendor, amount, PO number).
3. FILTERING: {vendor_instruction}
4. OBSERVATION: Before interacting with elements on a newly loaded page, make sure you understand the layout from the accessibility snapshot.
5. COMPLETION: When the goal has been fully satisfied and all relevant invoices are processed, output a final message containing the word 'done' and a concise summary of what was accomplished, without invoking further tools.
"""


# ---------------------------------------------------------------------------
# Tool Call & Message Parsing Helpers
# ---------------------------------------------------------------------------

class ParsedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    arguments: Dict[str, Any]


def parse_tool_call(response_message: Any) -> Optional[ParsedToolCall]:
    """
    Parses tool call information from a Groq / OpenAI ChatCompletionMessage.
    Returns ParsedToolCall or None if no valid tool call was returned.
    """
    if not response_message:
        return None

    # Check native tool_calls attribute
    tool_calls = getattr(response_message, "tool_calls", None)
    if tool_calls and len(tool_calls) > 0:
        first_call = tool_calls[0]
        call_id = getattr(first_call, "id", f"call_{uuid.uuid4().hex[:8]}")
        fn = getattr(first_call, "function", None)
        if fn:
            name = getattr(fn, "name", "")
            raw_args = getattr(fn, "arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except Exception as e:
                    logger.warning(f"Failed to parse tool call arguments '{raw_args}': {e}")
                    args = {}
            else:
                args = {}
            return ParsedToolCall(id=call_id, name=name, arguments=args)

    return None


def is_goal_done(response_message: Any) -> bool:
    """
    Determines if the LLM has signaled completion of the goal.
    """
    if not response_message:
        return False

    content = getattr(response_message, "content", None)
    if not content or not isinstance(content, str):
        return False

    lower = content.strip().lower()
    # Check for explicit done keywords or completion phrasing
    if re.search(r"\b(done|completed|task finished|all invoices processed)\b", lower):
        return True

    return False


# ---------------------------------------------------------------------------
# ReAct Agent Orchestrator
# ---------------------------------------------------------------------------

class AgentEventCallback:
    """Type alias / protocol for asynchronous event streaming callbacks (SSE)."""
    pass


class ReActAgent:
    """
    Production-grade ReAct loop agent orchestrating Groq LLM, Playwright browser tools,
    Neon Postgres persistence, and Upstash Redis session state & approval gates.
    """

    def __init__(
        self,
        run_id: Optional[str] = None,
        tools: Optional[PlaywrightTools] = None,
        pool: Optional[asyncpg.Pool] = None,
        groq_client: Optional[AsyncGroq] = None,
        redis_client: Optional[UpstashRedisClient] = None,
        model: str = settings.GROQ_MODEL,
        max_iterations: int = settings.MAX_AGENT_ITERATIONS,
        on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ):
        """Initialize ReAct loop agent with tools, database pool, Groq LLM, and Redis clients."""
        self.run_id = str(run_id or uuid.uuid4())
        self.tools = tools or PlaywrightTools(run_id=self.run_id, pool=pool)
        self.pool = pool
        self._groq_client = groq_client
        self._redis_client = redis_client
        self.model = model
        self.max_iterations = max_iterations
        self.on_event = on_event

        # Ensure tools has this run_id
        self.tools.set_run_id(self.run_id)

        # Track active form state during multi-step invoice creation to detect approval needs
        self._active_form_state: Dict[str, Any] = {}

    async def get_groq_client(self) -> AsyncGroq:
        """Get or initialize the asynchronous Groq API client."""
        if self._groq_client is None:
            self._groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)
        return self._groq_client

    async def get_redis(self) -> UpstashRedisClient:
        """Get or initialize the Upstash Redis client."""
        if self._redis_client is None:
            self._redis_client = get_redis_client()
        return self._redis_client

    async def get_db(self) -> asyncpg.Pool:
        """Get or initialize the Neon PostgreSQL asyncpg connection pool."""
        if self.pool is None:
            self.pool = await get_db_pool()
        return self.pool

    async def emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Emits an event to the registered SSE/event callback."""
        if self.on_event:
            event_obj = {
                "type": event_type,
                "run_id": self.run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            try:
                await self.on_event(event_obj)
            except Exception as e:
                logger.warning(f"Error in on_event handler for {event_type}: {e}")

    async def ensure_run_record(self, goal: str) -> None:
        """Ensure an agent_runs row exists in Neon database."""
        pool = await self.get_db()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runs (run_id, goal, status, created_at)
                VALUES ($1, $2, 'running', now())
                ON CONFLICT (run_id) DO UPDATE SET status = 'running';
                """,
                uuid.UUID(self.run_id),
                goal,
            )

    async def update_run_status(self, status: str) -> None:
        """Update agent_runs record status in Neon."""
        try:
            pool = await self.get_db()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = $1
                    WHERE run_id = $2;
                    """,
                    status,
                    uuid.UUID(self.run_id),
                )
        except Exception as e:
            logger.error(f"Failed to update agent_runs status for {self.run_id}: {e}")

    async def persist_step(
        self,
        action: str,
        result: str,
        screenshot_b64: Optional[str] = None,
    ) -> str:
        """Persist an agent step to Neon agent_steps table."""
        pool = await self.get_db()
        async with pool.acquire() as conn:
            step_id = await conn.fetchval(
                """
                INSERT INTO agent_steps (run_id, action, result, screenshot_b64, timestamp)
                VALUES ($1, $2, $3, $4, now())
                RETURNING step_id;
                """,
                uuid.UUID(self.run_id),
                action,
                result,
                screenshot_b64,
            )
            return str(step_id)

    async def get_last_successful_step_index(self) -> int:
        """
        Query Neon for the count or index of successful steps for this run_id (Phase 2.8 recovery).
        """
        try:
            pool = await self.get_db()
            async with pool.acquire() as conn:
                count = await conn.fetchval(
                    """
                    SELECT count(*) FROM agent_steps
                    WHERE run_id = $1 AND result NOT LIKE '%failed%';
                    """,
                    uuid.UUID(self.run_id),
                )
                return count or 0
        except Exception as e:
            logger.warning(f"Error checking last successful step for {self.run_id}: {e}")
            return 0

    def check_amount_exceeds_threshold(self, amount: Union[float, int, str], threshold: float) -> bool:
        """Check if an invoice amount strictly exceeds the approval threshold."""
        try:
            val = parse_amount(amount)
            return val > threshold
        except Exception:
            return False

    async def handle_approval_gate(
        self,
        vendor: str,
        amount: float,
        invoice_id: Optional[str] = None,
        po_number: Optional[str] = None,
    ) -> str:
        """
        Pauses the agent loop and waits for human approval via Upstash Redis.
        Returns 'approved', 'rejected', or 'stalled'.
        """
        logger.info(f"Agent {self.run_id}: Triggering approval gate for invoice {invoice_id or po_number} (Amount: {amount})")

        redis = await self.get_redis()
        # Fail fast if Redis is not configured
        if not redis.is_configured:
            logger.error(f"Agent {self.run_id}: Cannot trigger approval gate because Upstash Redis is not configured.")
            await self.update_run_status("stalled")
            await self.emit_event("approval_failed", {"run_id": self.run_id, "error": "Redis not configured"})
            return "stalled"

        # Drop any stale decision before creating the next approval request
        await redis.execute_command("DEL", f"hulchul:decision:{self.run_id}")

        request_nonce = uuid.uuid4().hex[:12]
        approval_data = {
            "run_id": self.run_id,
            "nonce": request_nonce,
            "status": "awaiting_approval",
            "invoice_id": invoice_id or f"inv-{uuid.uuid4().hex[:6]}",
            "vendor": vendor,
            "amount": amount,
            "po_number": po_number,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Update Neon run status
        await self.update_run_status("awaiting_approval")

        # 2. Persist approval request to Upstash Redis
        await redis.set_approval_pending(self.run_id, approval_data)

        # 3. Emit SSE event
        await self.emit_event("needs_approval", approval_data)

        # 4. Polling loop: poll Redis every 2 seconds with configurable deadline
        logger.info(f"Agent {self.run_id}: Pausing ReAct loop, waiting for approval decision (timeout: {settings.APPROVAL_TIMEOUT_SECONDS}s)...")
        decision: Optional[str] = None
        deadline = asyncio.get_running_loop().time() + settings.APPROVAL_TIMEOUT_SECONDS

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2.0)
            try:
                dec_record = await redis.get_approval_decision_record(self.run_id)
            except Exception as poll_err:
                logger.warning(f"Agent {self.run_id}: Error polling approval decision from Redis: {poll_err}")
                continue

            if dec_record:
                dec_nonce = dec_record.get("nonce")
                # If a nonce was attached, ensure it matches current request
                if dec_nonce and dec_nonce != request_nonce:
                    logger.warning(f"Agent {self.run_id}: Stale decision nonce {dec_nonce} != {request_nonce}; ignoring.")
                    continue
                dec_str = dec_record.get("decision")
                if dec_str in ("approved", "rejected"):
                    decision = dec_str
                    break

        if decision not in ("approved", "rejected"):
            logger.warning(f"Agent {self.run_id}: Approval gate timed out after {settings.APPROVAL_TIMEOUT_SECONDS}s.")
            await redis.clear_approval(self.run_id)
            await self.update_run_status("stalled")
            await self.emit_event(
                "approval_timeout",
                {"run_id": self.run_id, "invoice_id": approval_data["invoice_id"]},
            )
            return "stalled"

        logger.info(f"Agent {self.run_id}: Received approval decision: '{decision}'")

        # Clean up Redis approval keys
        await redis.clear_approval(self.run_id)

        # Restore run status in Neon
        await self.update_run_status("running")

        # Emit decision event
        await self.emit_event(
            "approval_resolved",
            {
                "run_id": self.run_id,
                "decision": decision,
                "invoice_id": approval_data["invoice_id"],
                "vendor": vendor,
                "amount": amount,
            },
        )

        if decision == "approved":
            return "approved"
        else:
            # If rejected, mark invoice as skipped in Neon if invoice exists
            if invoice_id:
                try:
                    pool = await self.get_db()
                    async with pool.acquire() as conn:
                        try:
                            inv_uuid = uuid.UUID(invoice_id)
                            await conn.execute(
                                "UPDATE invoices SET status = 'skipped' WHERE id = $1;",
                                inv_uuid,
                            )
                        except (ValueError, TypeError):
                            await conn.execute(
                                "UPDATE invoices SET status = 'skipped' WHERE po_number = $1;",
                                invoice_id,
                            )
                except Exception as dbe:
                    logger.warning(f"Could not mark invoice as skipped in Neon: {dbe}")
            return "rejected"

    async def run(
        self,
        goal: str,
        resume_step: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Executes the ReAct agent loop (Observe -> Think -> Act) until the goal is done
        or the hard iteration cap (30) is reached.
        """
        clean_goal = goal.strip()
        logger.info(f"Starting ReAct agent run {self.run_id} with goal: '{clean_goal}'")

        # 1. Ensure run record in database
        await self.ensure_run_record(clean_goal)

        # 2. Extract constraints from goal
        threshold = extract_approval_threshold(clean_goal, default=settings.DEFAULT_APPROVAL_THRESHOLD)
        vendor_filter = extract_vendor_filter(clean_goal)
        system_prompt = build_system_prompt(clean_goal, threshold=threshold, vendor_filter=vendor_filter)

        # 3. Setup Redis session state
        redis = await self.get_redis()
        current_step_idx = resume_step if resume_step is not None else await self.get_last_successful_step_index()

        await redis.set_session_state(
            self.run_id,
            {
                "run_id": self.run_id,
                "goal": clean_goal,
                "threshold": threshold,
                "vendor_filter": vendor_filter,
                "current_step": current_step_idx,
                "status": "running",
            },
        )

        # 4. Initialize conversation messages
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Goal: {clean_goal}"},
        ]

        groq_client = await self.get_groq_client()
        iteration = current_step_idx
        run_status = "running"
        final_summary: Optional[str] = None

        await self.emit_event(
            "run_started",
            {
                "run_id": self.run_id,
                "goal": clean_goal,
                "threshold": threshold,
                "vendor_filter": vendor_filter,
                "resumed_from_step": current_step_idx,
            },
        )

        while iteration < self.max_iterations:
            iteration += 1
            logger.info(f"Agent {self.run_id}: Iteration {iteration}/{self.max_iterations}")

            # Check pause flag in Redis (Phase 2.9 & Phase 3)
            if redis.is_configured:
                is_paused = await redis.get_pause_flag(self.run_id)
                if is_paused:
                    logger.info(f"Agent {self.run_id} is paused. Waiting for resume...")
                    await self.update_run_status("paused")
                    await self.emit_event("paused", {"step": iteration})
                    pause_deadline = asyncio.get_running_loop().time() + settings.PAUSE_TIMEOUT_SECONDS
                    while await redis.get_pause_flag(self.run_id):
                        if asyncio.get_running_loop().time() > pause_deadline:
                            logger.warning(f"Agent {self.run_id}: Pause wait timed out after {settings.PAUSE_TIMEOUT_SECONDS}s. Stalling run.")
                            await self.update_run_status("stalled")
                            await self.emit_event("stalled", {"step": iteration, "reason": "pause_timeout"})
                            return {
                                "run_id": self.run_id,
                                "status": "stalled",
                                "iterations": iteration,
                                "goal": clean_goal,
                                "threshold": threshold,
                                "summary": f"Run paused and timed out after {settings.PAUSE_TIMEOUT_SECONDS}s.",
                            }
                        await asyncio.sleep(1.0)
                    logger.info(f"Agent {self.run_id} resumed.")
                    await self.update_run_status("running")
                    await self.emit_event("resumed", {"step": iteration})

            # Step 1: OBSERVE - call read_page() to capture accessibility tree snapshot
            snapshot_res = await self.tools.read_page()
            snapshot_text = snapshot_res.get("snapshot", "") if snapshot_res.get("success") else ""
            current_url = snapshot_res.get("url", "")
            current_title = snapshot_res.get("title", "")

            # Formulate current page state context
            page_context = (
                f"[Current Page URL: {current_url} | Title: {current_title}]\n"
                f"[Current Accessibility Tree Snapshot]:\n{snapshot_text if snapshot_text else '(Page empty or inaccessible)'}"
            )

            # Construct iteration prompt messages: system + goal + history + current snapshot
            iter_messages = list(messages)
            iter_messages.append({"role": "user", "content": page_context})

            # Step 2: THINK - Send to Groq (openai/gpt-oss-120b) with tool definitions
            try:
                chat_completion = await groq_client.chat.completions.create(
                    model=self.model,
                    messages=iter_messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    temperature=0.1,
                )
                choice = chat_completion.choices[0]
                response_msg = choice.message
            except Exception as llm_err:
                logger.error(f"Groq LLM completion failed on iteration {iteration}: {llm_err}")
                await self.persist_step(
                    action="llm_think",
                    result=f"failed: {llm_err}",
                )
                await self.emit_event("step_failed", {"action": "llm_think", "error": str(llm_err)})
                # Wait briefly and retry next iteration
                await asyncio.sleep(2.0)
                continue

            # Step 3: PARSE - Check if model signals 'done' or returns tool calls
            parsed_call = parse_tool_call(response_msg)

            if parsed_call is None:
                # No tool call returned; inspect if model says 'done'
                if is_goal_done(response_msg):
                    logger.info(f"Agent {self.run_id}: Goal finished successfully.")
                    final_summary = response_msg.content or "Task completed successfully."
                    run_status = "completed"
                    await self.update_run_status("completed")
                    await redis.set_session_state(
                        self.run_id,
                        {
                            "run_id": self.run_id,
                            "status": "completed",
                            "current_step": iteration,
                            "summary": final_summary,
                        },
                    )
                    await self.emit_event("done", {"summary": final_summary, "total_steps": iteration})
                    break
                else:
                    # Model provided text but no tool call and not marked done; prompt to act
                    text_content = response_msg.content or ""
                    messages.append({"role": "assistant", "content": text_content})
                    messages.append({
                        "role": "user",
                        "content": "Please proceed by calling an appropriate tool, or declare 'done' if the goal is satisfied."
                    })
                    continue

            # Step 4: ACT - Execute the chosen tool
            tool_name = parsed_call.name
            tool_args = parsed_call.arguments
            logger.info(f"Agent {self.run_id}: Executing tool '{tool_name}' with args {tool_args}")

            # Track form fields if filling invoice inputs
            if tool_name == "fill":
                sel = str(tool_args.get("selector", "")).lower()
                val = tool_args.get("value", "")
                if "amount" in sel:
                    self._active_form_state["amount"] = val
                elif "po" in sel:
                    self._active_form_state["po_number"] = val
                elif "vendor" in sel:
                    self._active_form_state["vendor"] = val
            elif tool_name == "select":
                sel = str(tool_args.get("selector", "")).lower()
                val = tool_args.get("value", "")
                if "vendor" in sel:
                    self._active_form_state["vendor"] = val

            # Check Approval Gate on form submission actions (e.g. click 'Create Invoice' or submit)
            if tool_name == "click":
                selector = str(tool_args.get("selector", ""))
                btn_name = selector.lower()
                is_submit_action = any(k in btn_name for k in ("create invoice", "submit", "save invoice", "save"))

                # Resolve element targeted by click to detect submit buttons when CSS selectors are used
                if not is_submit_action and getattr(self.tools, "page", None):
                    try:
                        elem = self.tools.page.locator(selector).first
                        if elem:
                            elem_type = (await elem.get_attribute("type") or "").lower()
                            elem_text = (await elem.inner_text() or "").lower()
                            aria_label = (await elem.get_attribute("aria-label") or "").lower()
                            combined_text = f"{elem_type} {elem_text} {aria_label}"
                            if elem_type == "submit" or any(k in combined_text for k in ("submit", "create invoice", "save invoice")):
                                is_submit_action = True
                    except Exception as elem_err:
                        logger.debug(f"Could not inspect element attributes for '{selector}': {elem_err}")

                active_amount = self._active_form_state.get("amount")

                if is_submit_action and active_amount is not None:
                    if self.check_amount_exceeds_threshold(active_amount, threshold):
                        logger.info(f"Detected submission of invoice exceeding threshold ({active_amount} > {threshold})")
                        gate_outcome = await self.handle_approval_gate(
                            vendor=self._active_form_state.get("vendor", "Unknown Vendor"),
                            amount=parse_amount(active_amount),
                            po_number=self._active_form_state.get("po_number"),
                        )
                        if gate_outcome == "stalled":
                            logger.warning(f"Agent {self.run_id}: Run stalled at approval gate.")
                            return {
                                "run_id": self.run_id,
                                "status": "stalled",
                                "iterations": iteration,
                                "goal": clean_goal,
                                "threshold": threshold,
                                "summary": "Approval gate did not receive a decision or timed out.",
                            }
                        if gate_outcome != "approved":
                            # Rejection: skip submission, reset active form, inform LLM
                            tool_result = {
                                "success": False,
                                "rejected": True,
                                "message": f"Invoice submission rejected by human supervisor. Invoice skipped per policy.",
                            }
                            await self.persist_step(
                                action="approval_gate",
                                result="rejected_and_skipped",
                            )
                            self._active_form_state.clear()

                            # Record in conversation history
                            messages.append({
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": parsed_call.id,
                                        "type": "function",
                                        "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
                                    }
                                ],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": parsed_call.id,
                                "name": tool_name,
                                "content": json.dumps(tool_result),
                            })
                            continue

            # Execute Tool with recovery wrap (Phase 2.8)
            screenshot_on_fail: Optional[str] = None
            try:
                tool_result = await self.tools.execute(tool_name, tool_args)
                tool_success = tool_result.get("success", False)
            except Exception as exec_err:
                logger.error(f"Error executing tool '{tool_name}': {exec_err}")
                tool_result = {"success": False, "error": str(exec_err)}
                tool_success = False

            if not tool_success:
                # Capture diagnostic screenshot on failure without creating duplicate rows (Phase 2.8)
                failed_step_id = await self.persist_step(
                    action=tool_name,
                    result=f"failed: {tool_result.get('error')}",
                )
                try:
                    sc = await self.tools.take_screenshot(
                        step_id=failed_step_id,
                        action=tool_name,
                    )
                    if sc and sc.get("screenshot_b64") and not sc.get("persisted"):
                        async with (await self.get_db()).acquire() as conn:
                            await conn.execute(
                                "UPDATE agent_steps SET screenshot_b64 = $1 WHERE step_id = $2;",
                                sc["screenshot_b64"],
                                uuid.UUID(failed_step_id),
                            )
                except Exception as sc_err:
                    logger.warning(f"Failure screenshot not captured: {sc_err}")

                await self.emit_event(
                    "step_failed",
                    {
                        "step": iteration,
                        "action": tool_name,
                        "arguments": tool_args,
                        "error": tool_result.get("error"),
                    },
                )
            else:
                # Persist successful step to Neon (Phase 2.6)
                result_summary = "success"
                if "url" in tool_result:
                    result_summary = f"navigated to {tool_result['url']}"
                elif "clicked" in tool_result:
                    result_summary = f"clicked {tool_args.get('selector')}"
                elif "value" in tool_result:
                    result_summary = f"filled {tool_args.get('selector')} = {tool_result['value']}"
                elif "selected" in tool_result:
                    result_summary = f"selected {tool_result.get('selected')}"
                elif "exists" in tool_result:
                    result_summary = f"check_exists={tool_result.get('exists')}"

                await self.persist_step(action=tool_name, result=result_summary)
                await self.emit_event(
                    "step",
                    {
                        "step": iteration,
                        "action": tool_name,
                        "arguments": tool_args,
                        "result": tool_result,
                    },
                )

            # Update Redis session state (Phase 2.9)
            await redis.set_session_state(
                self.run_id,
                {
                    "run_id": self.run_id,
                    "goal": clean_goal,
                    "current_step": iteration,
                    "last_action": tool_name,
                    "status": "running",
                },
            )

            # Append tool call and result to conversation history
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": parsed_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_args),
                        },
                    }
                ],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": parsed_call.id,
                "name": tool_name,
                "content": json.dumps(tool_result),
            })

        # Check if hard cap was reached
        if iteration >= self.max_iterations and run_status != "completed":
            logger.warning(f"Agent {self.run_id} hit hard cap of {self.max_iterations} iterations. Marking as stalled.")
            run_status = "stalled"
            await self.update_run_status("stalled")
            await redis.set_session_state(
                self.run_id,
                {
                    "run_id": self.run_id,
                    "status": "stalled",
                    "current_step": iteration,
                    "error": "Exceeded maximum iteration cap of 30 steps.",
                },
            )
            await self.emit_event("stalled", {"iterations": iteration, "max_iterations": self.max_iterations})

        return {
            "run_id": self.run_id,
            "status": run_status,
            "iterations": iteration,
            "goal": clean_goal,
            "threshold": threshold,
            "summary": final_summary,
        }
