import asyncio
import json
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import asyncpg
import httpx
from groq import AsyncGroq
from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings
from backend.db import get_db_pool
from backend.redis_client import UpstashRedisClient, get_redis_client
from backend.run_lease import (
    TERMINAL_RUN_STATUSES,
    affected_rows,
    release_run_lease,
    run_lease_heartbeat,
)
from backend.tools import (
    PlaywrightTools,
    TOOL_DEFINITIONS,
    take_screenshot,
    check_exists,
    is_session_lost_error,
)

logger = logging.getLogger(__name__)

MAX_SESSION_REATTACHES: int = 2


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
        r"(?:over|above|exceeding|threshold\s+(?:of|at)?|greater\s+than|limit\s+(?:of|at)?|more\s+than)\s*(?:₹|rs\.?|inr|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
        r"(?:standard|default)\s*(?:₹|rs\.?|inr|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*(?:rule|threshold)?",
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


def extract_target_po(goal: str) -> Optional[str]:
    """
    Extracts a target PO number from a goal string if specified.
    Examples:
      - 'Process invoice for PO-1001' -> 'PO-1001'
      - 'Verify and create PO-2005' -> 'PO-2005'
    """
    if not goal:
        return None
    match = re.search(r"\b(PO-[A-Za-z0-9\-]+)\b", goal, re.IGNORECASE)
    if match:
        return match.group(1)
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
    """Structured representation of a parsed tool call invoked by the LLM agent."""
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    arguments: Dict[str, Any]


def parse_tool_call(response_message: Any) -> Optional[ParsedToolCall]:
    """
    Parses tool call information from OpenCode Zen (dict) or Groq / OpenAI ChatCompletionMessage.
    Returns ParsedToolCall or None if no valid tool call was returned.
    """
    if not response_message:
        return None

    # Check native tool_calls attribute or dict key
    if isinstance(response_message, dict):
        tool_calls = response_message.get("tool_calls")
    else:
        tool_calls = getattr(response_message, "tool_calls", None)

    if tool_calls and len(tool_calls) > 0:
        first_call = tool_calls[0]
        if isinstance(first_call, dict):
            call_id = str(first_call.get("id") or f"call_{uuid.uuid4().hex[:8]}")
            fn = first_call.get("function") or {}
            raw_name = fn.get("name", "") if isinstance(fn, dict) else getattr(fn, "name", "")
            raw_args = fn.get("arguments", "{}") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
        else:
            call_id = str(getattr(first_call, "id", f"call_{uuid.uuid4().hex[:8]}"))
            fn = getattr(first_call, "function", None)
            raw_name = getattr(fn, "name", "") if fn else ""
            raw_args = getattr(fn, "arguments", "{}") if fn else "{}"

        name = str(raw_name) if raw_name is not None else ""
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

    if isinstance(response_message, dict):
        content = response_message.get("content")
    else:
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
        model: str = settings.OPENCODE_MODEL,
        max_iterations: int = settings.MAX_AGENT_ITERATIONS,
        on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        reconnect: Optional[Callable[[], Awaitable[Any]]] = None,
        max_reattaches: int = MAX_SESSION_REATTACHES,
    ):
        """Initialize ReAct loop agent with tools, database pool, OpenCode / Groq LLM, and Redis clients."""
        self.run_id = str(run_id or uuid.uuid4())
        self.tools = tools or PlaywrightTools(run_id=self.run_id, pool=pool)
        self.pool = pool
        self._groq_client = groq_client
        self._redis_client = redis_client
        self.model = model
        self.max_iterations = max_iterations
        self.on_event = on_event
        self._reconnect = reconnect
        self.max_reattaches = max_reattaches
        self._reattach_attempts = 0

        # Ensure tools has this run_id
        self.tools.set_run_id(self.run_id)

        # Track active form state during multi-step invoice creation to detect approval needs
        self._active_form_state: Dict[str, Any] = {}

        # Per-invoice dispositions observed by this run, flushed to terminal
        # invoice statuses by the run-completion path (G1). Keyed by invoice
        # UUID string, falling back to "po:<PO_NUMBER>" when no id is known.
        # Values carry {invoice_id, po_number, vendor, amount, disposition}
        # where disposition is one of "submitted" (submit click succeeded),
        # "approved" (gate approved, submission not yet observed), "rejected"
        # (gate rejected; skipped is written at the gate), or "duplicate"
        # (idempotency abort; the existing row is left alone).
        self._processed_invoices: Dict[str, Dict[str, Any]] = {}

        # Snapshot of the invoice a submit click is about to act on, taken
        # exactly where the run has decided the submit will execute. Consumed
        # (and cleared) by the post-execution success/failure handling, which
        # records the "submitted" disposition only on observed success.
        self._pending_submit_ref: Optional[Dict[str, Any]] = None

        # Track current execution iteration index
        self._current_iteration: int = 0

        # Track idempotency checks performed during this run (cache key: entity_type:identifier -> check_result)
        self._checked_entities: Dict[str, Dict[str, Any]] = {}

        # Identity this execution holds its run lease under (issue #56). Per-instance,
        # so two concurrent executions of the same run_id are always distinct owners
        # and the second one is refused rather than silently joining the first.
        self._run_owner_id: str = uuid.uuid4().hex
        self._run_heartbeat_task: Optional[asyncio.Task] = None
        # Set by the heartbeat once a renewal matches zero rows, i.e. this execution
        # no longer owns the run. Checked by run() at each point where it would
        # otherwise execute a tool or write state (issue #56).
        self._lease_ownership_lost: bool = False

        # Approval threshold extracted from goal or default
        self.approval_threshold: float = settings.DEFAULT_APPROVAL_THRESHOLD

    async def call_llm(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """
        Executes an LLM chat completion using OpenCode Zen (Primary) with automatic
        fallback to Groq if OpenCode fails, times out, rate-limits, or lacks an API key.
        """
        # 1. Try OpenCode Zen Primary (only if no custom Groq client was injected)
        has_custom_groq = self._groq_client is not None or getattr(self.get_groq_client, "__func__", None) is not ReActAgent.get_groq_client
        opencode_key = (settings.OPENCODE_API_KEY or "").strip()
        opencode_base_url = (settings.OPENCODE_BASE_URL or "https://opencode.ai/zen/v1").strip().rstrip("/")
        raw_opencode_model = (settings.OPENCODE_MODEL or "space-bunny-free").strip()
        opencode_model = "space-bunny-free" if raw_opencode_model == "space-bunny" else raw_opencode_model

        if not has_custom_groq and opencode_key:
            endpoint = f"{opencode_base_url}/chat/completions" if not opencode_base_url.endswith("/chat/completions") else opencode_base_url
            headers = {
                "Authorization": f"Bearer {opencode_key}",
                "Content-Type": "application/json",
            }
            payload: Dict[str, Any] = {
                "model": opencode_model,
                "messages": messages,
                "temperature": 0.1,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            try:
                async with httpx.AsyncClient(timeout=35.0) as http_client:
                    response = await http_client.post(endpoint, json=payload, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        choices = data.get("choices", [])
                        if choices:
                            raw_msg = choices[0].get("message", {})
                            logger.info(f"Agent {self.run_id}: LLM completion succeeded via OpenCode Zen ({opencode_model}).")
                            return raw_msg
                        else:
                            logger.warning(f"Agent {self.run_id}: OpenCode Zen response missing choices: {data}")
                    else:
                        logger.warning(f"Agent {self.run_id}: OpenCode Zen returned status {response.status_code}: {response.text[:200]}")
            except Exception as oc_err:
                logger.warning(f"Agent {self.run_id}: OpenCode Zen call failed ({oc_err}). Falling back to Groq...")
        else:
            logger.info(f"Agent {self.run_id}: OPENCODE_API_KEY not configured. Falling back to Groq...")

        # 2. Fallback to Groq
        groq_client = await self.get_groq_client()
        groq_model = settings.GROQ_MODEL or "openai/gpt-oss-120b"
        logger.info(f"Agent {self.run_id}: Calling fallback LLM Groq ({groq_model})...")
        kwargs: Dict[str, Any] = {
            "model": groq_model,
            "messages": messages,
            "temperature": 0.1,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        chat_completion = await groq_client.chat.completions.create(**kwargs)
        choice = chat_completion.choices[0]
        return choice.message

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
        """
        Emits a structured real-time event to the registered SSE callback
        and publishes it directly to the global run event hub for SSE streaming.
        """
        event_obj = {
            "type": event_type,
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        if self.on_event:
            try:
                await self.on_event(event_obj)
            except Exception as e:
                logger.warning(f"Error in on_event handler for {event_type}: {e}")
        try:
            from backend.main import run_event_hub
            await run_event_hub.publish(self.run_id, event_obj)
        except Exception:
            pass

    async def ensure_run_record(
        self,
        goal: str = "Agent Execution Run",
        start_heartbeat: bool = False,
    ) -> bool:
        """Ensure an agent_runs row exists and is owned by this execution.

        Single atomic upsert: the row is created as ``running`` under this
        execution's lease, or an existing row is taken over only when no live owner
        holds it. Returns ``False`` when the claim is refused because another owner
        holds an unexpired lease, which is the caller's signal not to execute --
        that is what stops a duplicate request or a retry from driving the same
        run_id twice.

        The lease is established by this statement rather than by a follow-up claim
        call because a fresh run has no row to claim yet, and splitting the accept into
        "upsert" then "claim" would leave a window where a second request could upsert
        the row between the two.

        This is the only claim decision in the codebase. An earlier draft also carried a
        standalone ``claim_run_lease`` helper, which nothing called: it was a second
        implementation of the same decision and it had already drifted, since it refused
        a terminal row outright where this statement deliberately resurrects one.

        Concurrency is handled by the conditional ``ON CONFLICT DO UPDATE`` alone, not
        by a preceding ``SELECT ... FOR UPDATE SKIP LOCKED``. ``pool.acquire()`` is not a
        transaction, so such a ``SELECT`` autocommits and drops its row lock before the
        upsert runs, which makes the two statements non-atomic -- the lock is released
        before it can exclude anyone. What actually serialises concurrent claimants is
        that the second statement blocks on the row lock the first one holds and then
        re-evaluates its ``WHERE`` against the committed row, finding a live lease and
        taking no branch.

        Resurrection of a terminal run (no live lease) is preserved deliberately: that
        is the resume/recovery path for a run that previously stalled or failed, and
        existing recovery flows depend on it.

        ``start_heartbeat`` is only set by ``run``. ``persist_step`` also calls this
        as foreign-key recovery when a row is missing, and a bookkeeping write must
        not spawn a lease heartbeat.
        """
        pool = await self.get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_runs
                    (run_id, goal, status, owner_id, lease_expires_at, attempt, created_at)
                VALUES
                    ($1, $2, 'running', $3, now() + make_interval(secs => $4::double precision), 1, now())
                ON CONFLICT (run_id) DO UPDATE SET
                    goal = EXCLUDED.goal,
                    status = 'running',
                    owner_id = EXCLUDED.owner_id,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    attempt = agent_runs.attempt + 1
                WHERE agent_runs.lease_expires_at IS NULL
                   OR agent_runs.lease_expires_at <= now()
                   OR agent_runs.owner_id = EXCLUDED.owner_id
                RETURNING run_id, status, owner_id, attempt, lease_expires_at
                """,
                uuid.UUID(self.run_id),
                goal,
                self._run_owner_id,
                float(settings.RUN_LEASE_SECONDS),
            )

        if row is None:
            logger.error(
                f"Run {self.run_id} is leased by another live owner; refusing to execute."
            )
            return False

        if start_heartbeat:
            self._start_run_lease_heartbeat(pool)
        return True

    @property
    def run_owner_id(self) -> str:
        """Lease identity this execution holds its run under.

        Read by ``backend/main.py`` so the background task's fallback terminal-status
        write can be fenced on ownership the same way ``update_run_status`` is
        (issue #56). That write is reached when this execution produced no result of
        its own -- a cancellation, or an exception out of ``run()`` -- and an expired
        lease does not stop its former holder, so without the fence it stamps a
        terminal status over the run its replacement is executing.
        """
        return self._run_owner_id

    def _mark_run_lease_lost(self) -> None:
        """Record that the heartbeat lost this run's lease.

        Invoked from the heartbeat task, so it must stay synchronous and must never
        raise: an exception here would surface as an unhandled task exception. Idempotent
        because the heartbeat reports the loss once but ``run()`` reads the flag at
        several checkpoints.
        """
        if self._lease_ownership_lost:
            return
        self._lease_ownership_lost = True
        logger.error(
            f"Agent {self.run_id} lost its run lease to another execution; "
            "this execution will stop acting on the run."
        )

    def _lease_lost_result(self, iteration: int, goal: str, threshold: float) -> Dict[str, Any]:
        """Terminal result returned by ``run()`` when ownership of the run was lost.

        Reported as ``failed`` for the same reason the lease-refused branch of ``run()``
        does: this execution produced no outcome of its own, and a status the durable
        store does not hold would only be contradicted by the owner's next write. The
        distinction lives in the summary, which is what a log reader needs.

        Returned rather than raised on purpose. Raising would unwind into the caller's
        cancellation/exception handler, whose terminal-status write is not owner-scoped
        and would then stamp ``failed`` over the run the newer owner is executing.
        """
        return {
            "run_id": self.run_id,
            "status": "failed",
            "iterations": iteration,
            "goal": goal,
            "threshold": threshold,
            "summary": (
                "Run lease was lost to another execution; this execution stopped "
                "without executing further steps."
            ),
        }

    def _start_run_lease_heartbeat(self, pool: asyncpg.Pool) -> None:
        """Start the out-of-band lease renewal task for this run, if not already running."""
        if self._run_heartbeat_task is not None and not self._run_heartbeat_task.done():
            return
        try:
            self._run_heartbeat_task = asyncio.create_task(
                run_lease_heartbeat(
                    pool=pool,
                    run_id=self.run_id,
                    owner_id=self._run_owner_id,
                    interval_seconds=settings.RUN_HEARTBEAT_SECONDS,
                    lease_seconds=settings.RUN_LEASE_SECONDS,
                    owner_task=asyncio.current_task(),
                    on_ownership_lost=self._mark_run_lease_lost,
                )
            )
        except RuntimeError as e:
            # No running loop: the lease still exists, it just will not be renewed,
            # so it lapses and reconciliation can reclaim the run.
            logger.warning(f"Could not start run lease heartbeat for {self.run_id}: {e}")

    async def stop_run_lease(self) -> None:
        """Cancel the heartbeat and hand the lease back. Safe to call more than once."""
        task = self._run_heartbeat_task
        self._run_heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
        try:
            pool = await self.get_db()
            await release_run_lease(pool, self.run_id, self._run_owner_id)
        except Exception as e:
            # Never raise from here: this runs in the cleanup path of a run that is
            # already finishing or unwinding. A lease we cannot release simply lapses,
            # and a lapsed lease on a terminal run is inert.
            logger.warning(f"Failed to release run lease for {self.run_id}: {e}")

    async def update_run_status(self, status: str, release_lease: bool = True) -> None:
        """Update agent_runs record status in Neon database and emit status_change event.

        The write is fenced on the lease owner (issue #56). A lease expiring does not
        stop the old holder: a frozen-then-thawed execution can wake up mid-run and
        write `completed` straight over a newer owner's state, including a run that
        reconciliation already failed. A NULL owner is *not* writable either -- NULL
        means the lease was released or the run was reclaimed, and in both cases this
        execution is not the owner. ``run()`` always claims the lease via
        ``ensure_run_record`` before any status write, so the legitimate writer is
        always fenced in.

        A fenced-out write emits no ``status_change``: announcing a status that never
        reached the database would be a lie the SSE stream would then broadcast.

        The lease is released only when the write actually landed. Releasing on the
        failure path would clear ``owner_id`` / ``lease_expires_at`` while
        ``agent_runs.status`` is still ``running``, which is exactly the orphan
        predicate: the row becomes claimable by a duplicate request and by the next
        startup sweep while this execution is still running, and nothing renews or
        fences it any more. Leaving the lease in place is the safe direction -- it
        lapses on its own and the run stays owned until it does.

        Callers which publish more durable state after the status write must pass
        ``release_lease=False`` and release explicitly once the final publish lands,
        otherwise the run is claimable mid-publish.
        """
        landed = False
        try:
            pool = await self.get_db()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = $1
                    WHERE run_id = $2
                      AND owner_id = $3;
                    """,
                    status,
                    uuid.UUID(self.run_id),
                    self._run_owner_id,
                )
            if affected_rows(result) == 0:
                logger.warning(
                    f"Dropped '{status}' for run {self.run_id}: ownership was lost to "
                    "another execution."
                )
                return
            landed = True
            await self.emit_event("status_change", {"status": status})
        except Exception as e:
            logger.error(f"Failed to update agent_runs status for {self.run_id}: {e}")
        finally:
            # Every terminal transition is the last thing this execution does, so this
            # is the one place guaranteed to run on each exit path. Non-terminal
            # transitions (running, paused, awaiting_approval) keep the lease: the run
            # is still the caller's to renew.
            if status in TERMINAL_RUN_STATUSES and landed and release_lease:
                await self.stop_run_lease()

    async def persist_step(
        self,
        action: str,
        result: Optional[str] = None,
        screenshot_b64: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        step_id: Optional[str] = None,
    ) -> str:
        """
        Persist an agent step to Neon agent_steps table (Phase 2.6).
        Records action, result, optional base64 screenshot, and timestamp.
        Guarantees foreign key integrity by ensuring agent_runs record exists.
        """
        run_uuid = uuid.UUID(str(self.run_id))
        step_uuid = uuid.UUID(str(step_id)) if step_id else None
        pool = await self.get_db()

        async def _execute_insert() -> uuid.UUID:
            """Executes SQL insert statement for agent step and returns step UUID."""
            async with pool.acquire() as conn:
                if step_uuid:
                    return await conn.fetchval(
                        """
                        INSERT INTO agent_steps (step_id, run_id, action, result, screenshot_b64, timestamp)
                        VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()))
                        ON CONFLICT (step_id) DO UPDATE SET result = EXCLUDED.result, screenshot_b64 = EXCLUDED.screenshot_b64
                        RETURNING step_id;
                        """,
                        step_uuid,
                        run_uuid,
                        action,
                        result,
                        screenshot_b64,
                        timestamp,
                    )
                else:
                    return await conn.fetchval(
                        """
                        INSERT INTO agent_steps (run_id, action, result, screenshot_b64, timestamp)
                        VALUES ($1, $2, $3, $4, COALESCE($5, now()))
                        RETURNING step_id;
                        """,
                        run_uuid,
                        action,
                        result,
                        screenshot_b64,
                        timestamp,
                    )

        try:
            sid = await _execute_insert()
            return str(sid)
        except asyncpg.ForeignKeyViolationError:
            logger.info(f"Run {self.run_id} missing in agent_runs during persist_step; ensuring record...")
            await self.ensure_run_record()
            sid = await _execute_insert()
            return str(sid)

    async def update_step(
        self,
        step_id: str,
        result: Optional[str] = None,
        screenshot_b64: Optional[str] = None,
    ) -> bool:
        """
        Update an existing agent step in Neon (e.g. attaching failure screenshot or updating result).
        """
        try:
            step_uuid = uuid.UUID(str(step_id))
            pool = await self.get_db()
            async with pool.acquire() as conn:
                query = """
                    UPDATE agent_steps
                    SET result = COALESCE($1, result),
                        screenshot_b64 = COALESCE($2, screenshot_b64)
                    WHERE step_id = $3;
                """
                res = await conn.execute(query, result, screenshot_b64, step_uuid)
                return "UPDATE 1" in res
        except Exception as e:
            logger.error(f"Failed to update step {step_id}: {e}")
            return False

    async def get_steps(
        self,
        include_screenshots: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Query all persisted steps for this run from Neon agent_steps ordered chronologically.
        """
        pool = await self.get_db()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT step_id, run_id, action, result, screenshot_b64, timestamp
                FROM agent_steps
                WHERE run_id = $1
                ORDER BY timestamp ASC;
                """,
                uuid.UUID(str(self.run_id)),
            )
            return [
                {
                    "step_id": str(r["step_id"]),
                    "run_id": str(r["run_id"]),
                    "action": r["action"],
                    "result": r["result"],
                    "has_screenshot": bool(r["screenshot_b64"]),
                    "screenshot_b64": r["screenshot_b64"] if include_screenshots else None,
                    "timestamp": r["timestamp"].isoformat(),
                }
                for r in rows
            ]

    async def get_step_by_id(
        self,
        step_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Query a specific step from Neon agent_steps by step_id.
        """
        try:
            step_uuid = uuid.UUID(str(step_id))
            pool = await self.get_db()
            async with pool.acquire() as conn:
                r = await conn.fetchrow(
                    """
                    SELECT step_id, run_id, action, result, screenshot_b64, timestamp
                    FROM agent_steps
                    WHERE step_id = $1;
                    """,
                    step_uuid,
                )
                if not r:
                    return None
                return {
                    "step_id": str(r["step_id"]),
                    "run_id": str(r["run_id"]),
                    "action": r["action"],
                    "result": r["result"],
                    "has_screenshot": bool(r["screenshot_b64"]),
                    "screenshot_b64": r["screenshot_b64"],
                    "timestamp": r["timestamp"].isoformat(),
                }
        except Exception as e:
            logger.error(f"Error fetching step {step_id}: {e}")
            return None

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

    async def check_idempotency(self, entity_type: str, identifier: str) -> Dict[str, Any]:
        """
        Check if an entity (e.g. invoice by PO number or ID) already exists in the database.
        Caches result in self._checked_entities to avoid redundant DB roundtrips while enforcing
        strict idempotency guarantees across the ReAct loop (Phase 2.5).
        """
        clean_type = (entity_type or "").strip().lower()
        clean_id = (identifier or "").strip()
        if not clean_id:
            return {"exists": False, "entity_type": clean_type, "identifier": clean_id, "record": None}

        cache_key = f"{clean_type}:{clean_id.lower()}"
        if cache_key in self._checked_entities:
            return self._checked_entities[cache_key]

        result = await self.tools.check_exists(entity_type=clean_type, identifier=clean_id)
        if result.get("exists") and not result.get("error"):
            self._checked_entities[cache_key] = result
        return result

    def record_invoice_disposition(
        self,
        disposition: str,
        invoice_id: Optional[str] = None,
        po_number: Optional[str] = None,
        vendor: Optional[str] = None,
        amount: Optional[Any] = None,
    ) -> Optional[str]:
        """Track how this run disposed of one invoice for the completion flush.

        Returns the tracking key, or None when there is nothing to key on
        (no invoice id and no PO number). Overwrites any earlier entry for the
        same invoice: the latest observed disposition wins.
        """
        inv = (str(invoice_id).strip() if invoice_id else "")
        po = (str(po_number).strip() if po_number else "")
        if inv:
            try:
                key = str(uuid.UUID(inv))
            except (ValueError, TypeError, AttributeError):
                key = f"po:{po}" if po else ""
                if not key:
                    # A non-UUID invoice ref with no PO cannot be resolved back
                    # to a row later; keep it visible under its raw value so it
                    # is at least auditable, and let the flush skip it.
                    key = f"ref:{inv}"
        elif po:
            key = f"po:{po}"
        else:
            return None
        self._processed_invoices[key] = {
            "invoice_id": inv or None,
            "po_number": po or None,
            "vendor": vendor,
            "amount": amount,
            "disposition": disposition,
        }
        return key

    async def mark_invoice_terminal_status(
        self,
        status: str,
        invoice_id: Optional[str] = None,
        po_number: Optional[str] = None,
    ) -> bool:
        """Set one invoice's terminal status, transitioning from pending only.

        The pending-only guard is deliberate: a human can settle an invoice
        through PATCH /api/invoices/[id] at any time, and the agent must never
        blindly overwrite such a settled state (nor 'approved', 'rejected', or
        any other non-pending value). The guard is enforced twice -- a Python
        pre-check that skips the write, and an `AND status = 'pending'`
        predicate on the UPDATE itself so a settlement landing between the
        read and the write still wins. Never raises: callers treat a False as
        "not settled by this run" and move on.
        """
        if self._lease_ownership_lost:
            logger.warning(
                f"Agent {self.run_id}: not marking invoice status '{status}': "
                "run lease was lost."
            )
            return False
        try:
            pool = await self.get_db()
            async with pool.acquire() as conn:
                row = None
                if invoice_id:
                    try:
                        inv_uuid = uuid.UUID(str(invoice_id))
                    except (ValueError, TypeError, AttributeError):
                        inv_uuid = None
                    if inv_uuid is not None:
                        row = await conn.fetchrow(
                            "SELECT id, status FROM invoices WHERE id = $1;",
                            inv_uuid,
                        )
                if row is None and po_number:
                    row = await conn.fetchrow(
                        "SELECT id, status FROM invoices WHERE po_number = $1 "
                        "ORDER BY created_at DESC LIMIT 1;",
                        str(po_number).strip(),
                    )
                if row is None:
                    logger.info(
                        f"Agent {self.run_id}: no invoice row for "
                        f"(id={invoice_id}, po={po_number}); leaving status unset."
                    )
                    return False
                if (row["status"] or "") != "pending":
                    logger.info(
                        f"Agent {self.run_id}: invoice {row['id']} already "
                        f"settled as '{row['status']}'; not overwriting with '{status}'."
                    )
                    return False
                res = await conn.execute(
                    "UPDATE invoices SET status = $1 WHERE id = $2 AND status = 'pending';",
                    status,
                    row["id"],
                )
                landed = affected_rows(res) > 0
                logger.info(
                    f"Agent {self.run_id}: marked invoice {row['id']} "
                    f"status='{status}': {landed}"
                )
                return landed
        except Exception as e:
            logger.warning(f"Agent {self.run_id}: could not mark invoice status '{status}': {e}")
            return False

    async def _classify_processed_invoice(
        self,
        conn: Any,
        po_number: Optional[str],
        amount: Any,
    ) -> Optional[str]:
        """Derive the terminal status for a submitted invoice, or None to skip.

        Mirrors the deterministic seed rules in backend/verification.py: a PO
        number with no purchase_orders row means the invoice can never be
        reconciled (failed); an amount more than 10% off the PO's approved
        amount is a mismatch for a human to review (flagged); anything else
        the run submitted counts as processed (completed, including
        human-approved over-threshold invoices, which verification likewise
        expects as completed). The rule text lives here rather than importing
        verification.py because the agent loop must not depend on the audit
        module it is audited by; any rule change must update both places.
        """
        if not po_number:
            return None
        try:
            po_row = await conn.fetchrow(
                "SELECT approved_amount FROM purchase_orders WHERE po_number = $1;",
                str(po_number).strip(),
            )
        except Exception as e:
            logger.warning(f"Agent {self.run_id}: PO lookup failed for {po_number}: {e}")
            return None
        if po_row is None or po_row["approved_amount"] is None:
            return "failed"
        try:
            approved = float(po_row["approved_amount"])
            actual = float(amount)
        except (TypeError, ValueError):
            return None
        if approved > 0 and (abs(actual - approved) / approved) * 100.0 > 10.0:
            return "flagged"
        return "completed"

    async def _finalize_processed_invoices(self) -> Dict[str, int]:
        """Flush tracked dispositions to terminal invoice statuses. Best-effort.

        Runs once on the run-completion path. Only invoices this run actually
        processed are touched: "submitted" entries are classified (completed /
        flagged / failed) and written; "rejected" entries are ensured skipped
        in case the gate's immediate write failed; "approved"-without-submit
        and "duplicate" entries are left alone (no observed ERP submission).
        Every write goes through mark_invoice_terminal_status, so the
        pending-only guard applies throughout. Returns per-status counts and
        never raises.
        """
        counts = {"completed": 0, "flagged": 0, "failed": 0, "skipped": 0, "untouched": 0}
        if self._lease_ownership_lost:
            return counts
        if not self._processed_invoices:
            return counts
        try:
            pool = await self.get_db()
        except Exception as e:
            logger.warning(f"Agent {self.run_id}: invoice finalize skipped, no DB pool: {e}")
            return counts
        try:
            async with pool.acquire() as conn:
                for key, entry in list(self._processed_invoices.items()):
                    try:
                        disposition = entry.get("disposition")
                        inv_id = entry.get("invoice_id")
                        po = entry.get("po_number")
                        target: Optional[str] = None
                        if disposition == "submitted":
                            inv_row = None
                            if inv_id:
                                try:
                                    inv_row = await conn.fetchrow(
                                        "SELECT id, status, amount, po_number FROM invoices WHERE id = $1;",
                                        uuid.UUID(str(inv_id)),
                                    )
                                except (ValueError, TypeError, AttributeError):
                                    inv_row = None
                            if inv_row is None and po:
                                inv_row = await conn.fetchrow(
                                    "SELECT id, status, amount, po_number FROM invoices "
                                    "WHERE po_number = $1 ORDER BY created_at DESC LIMIT 1;",
                                    str(po).strip(),
                                )
                            if inv_row is None:
                                logger.info(
                                    f"Agent {self.run_id}: finalize skipping {key}: no invoice row."
                                )
                                counts["untouched"] += 1
                                continue
                            if (inv_row["status"] or "") != "pending":
                                logger.info(
                                    f"Agent {self.run_id}: finalize skipping {inv_row['id']}: "
                                    f"already '{inv_row['status']}'."
                                )
                                counts["untouched"] += 1
                                continue
                            target = await self._classify_processed_invoice(
                                conn, inv_row["po_number"], inv_row["amount"]
                            )
                            if target is None:
                                counts["untouched"] += 1
                                continue
                        elif disposition == "rejected":
                            target = "skipped"
                        else:
                            counts["untouched"] += 1
                            continue
                        if await self.mark_invoice_terminal_status(
                            target, invoice_id=inv_id, po_number=po
                        ):
                            counts[target] += 1
                        else:
                            counts["untouched"] += 1
                    except Exception as e:
                        logger.warning(f"Agent {self.run_id}: finalize failed for {key}: {e}")
                        counts["untouched"] += 1
        except Exception as e:
            logger.warning(f"Agent {self.run_id}: invoice finalize aborted: {e}")
        return counts

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
        Pauses the agent loop and waits for human approval via Upstash Redis (Phase 2.7).
        Emits 'needs_approval' SSE event, writes awaiting state to Redis, polls every 2 seconds,
        and on rejection marks the invoice as 'skipped' in Neon before moving to the next invoice.
        Returns 'approved', 'rejected', 'stalled', or 'lease_lost'.

        'lease_lost' is not a decision about the invoice: it means the run's lease was
        taken over while this gate was polling, so this execution no longer owns the
        run and must not clear the approval keys, mark the invoice skipped, or publish
        a gate outcome. ``run()`` maps it to the lease-lost terminal result.
        """
        logger.info(f"Agent {self.run_id}: Triggering approval gate for invoice {invoice_id or po_number} (Amount: {amount})")

        if self._lease_ownership_lost:
            logger.error(
                f"Agent {self.run_id}: run lease already lost; not opening the approval gate."
            )
            return "lease_lost"

        redis = await self.get_redis()
        # Fail fast if Redis is not configured
        if not redis.is_configured:
            logger.error(f"Agent {self.run_id}: Cannot trigger approval gate because Upstash Redis is not configured.")
            await self.update_run_status("stalled")
            await self.emit_event("approval_failed", {"run_id": self.run_id, "error": "Redis not configured"})
            return "stalled"

        # Resolve existing invoice_id from Neon by po_number if invoice_id is not already provided
        resolved_invoice_id = invoice_id
        if not resolved_invoice_id and po_number:
            try:
                pool = await self.get_db()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT id FROM invoices WHERE po_number = $1 ORDER BY created_at DESC LIMIT 1;",
                        str(po_number).strip(),
                    )
                    if row:
                        resolved_invoice_id = str(row["id"])
            except Exception as e:
                logger.debug(f"Could not resolve invoice_id from po_number {po_number}: {e}")

        if not resolved_invoice_id:
            resolved_invoice_id = f"inv-{uuid.uuid4().hex[:6]}"

        # Drop any stale decision before creating the next approval request
        await redis.execute_command("DEL", f"hulchul:decision:{self.run_id}")

        request_nonce = uuid.uuid4().hex[:12]
        approval_data = {
            "run_id": self.run_id,
            "nonce": request_nonce,
            "status": "awaiting_approval",
            "invoice_id": resolved_invoice_id,
            "vendor": vendor,
            "amount": amount,
            "po_number": po_number,
            "threshold": self.approval_threshold,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Update Neon run status
        await self.update_run_status("awaiting_approval")

        # 2. Persist approval gate step in Neon agent_steps (Phase 2.6)
        await self.persist_step(
            action="approval_gate",
            result=f"awaiting_approval: vendor={vendor}, amount={amount}, po={po_number or 'None'}, invoice_id={resolved_invoice_id}",
        )

        # 3. Persist approval request & active session state to Upstash Redis
        await redis.set_approval_pending(self.run_id, approval_data)
        await redis.set_session_state(
            self.run_id,
            {
                "run_id": self.run_id,
                "status": "awaiting_approval",
                "invoice_id": resolved_invoice_id,
                "vendor": vendor,
                "amount": amount,
                "po_number": po_number,
                "current_step": getattr(self, "_current_iteration", 0),
            },
        )

        # 4. Emit SSE event (Phase 2.7: needs_approval event containing invoice_id, vendor, amount, po_number)
        await self.emit_event("needs_approval", approval_data)

        # 5. Polling loop: poll Redis every 2 seconds with configurable deadline
        logger.info(f"Agent {self.run_id}: Pausing ReAct loop, polling Redis every 2s for approval decision (timeout: {settings.APPROVAL_TIMEOUT_SECONDS}s)...")
        decision: Optional[str] = None
        deadline = asyncio.get_running_loop().time() + settings.APPROVAL_TIMEOUT_SECONDS

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2.0)
            # The gate blocks for up to APPROVAL_TIMEOUT_SECONDS, which is long enough
            # for the lease to be reclaimed underneath it. Everything below this point
            # writes durable state (approval keys, agent_steps, invoices.status) and
            # publishes a gate outcome, so the check belongs here rather than at the
            # gate's entry only.
            if self._lease_ownership_lost:
                logger.error(
                    f"Agent {self.run_id}: run lease lost while awaiting approval; "
                    "abandoning the gate without touching its state."
                )
                return "lease_lost"
            try:
                dec_record = await redis.get_approval_decision_record(self.run_id)
            except Exception as poll_err:
                logger.warning(f"Agent {self.run_id}: Error polling approval decision from Redis: {poll_err}")
                continue

            # Re-checked after the read, not only before it: the read is an await, so
            # ownership can be reported lost while it is in flight. Consuming the
            # decision anyway would clear the approval keys and, on the rejection
            # path, mark the invoice skipped in Neon for a run this execution no
            # longer owns.
            if self._lease_ownership_lost:
                logger.error(
                    f"Agent {self.run_id}: run lease lost while reading the approval "
                    "decision; abandoning the gate without applying it."
                )
                return "lease_lost"

            if dec_record:
                dec_nonce = dec_record.get("nonce")
                # Fail closed: require a matching nonce for the current request
                if not dec_nonce or dec_nonce != request_nonce:
                    logger.warning(f"Agent {self.run_id}: Ignoring decision without valid matching nonce (got {dec_nonce!r}).")
                    continue
                dec_str = dec_record.get("decision")
                if dec_str in ("approved", "rejected"):
                    decision = dec_str
                    break

        if decision not in ("approved", "rejected"):
            logger.warning(f"Agent {self.run_id}: Approval gate timed out after {settings.APPROVAL_TIMEOUT_SECONDS}s.")
            await self.persist_step(
                action="approval_gate",
                result=f"timed_out: vendor={vendor}, amount={amount} after {settings.APPROVAL_TIMEOUT_SECONDS}s",
            )
            await redis.clear_approval(self.run_id)
            await self.update_run_status("stalled")
            await self.emit_event(
                "approval_timeout",
                {"run_id": self.run_id, "invoice_id": resolved_invoice_id},
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
                "invoice_id": resolved_invoice_id,
                "vendor": vendor,
                "amount": amount,
                "po_number": po_number,
            },
        )

        if decision == "approved":
            await self.persist_step(
                action="approval_gate",
                result=f"approved: vendor={vendor}, amount={amount}, po={po_number or 'None'}, invoice_id={resolved_invoice_id}",
            )
            self.record_invoice_disposition(
                "approved",
                invoice_id=resolved_invoice_id,
                po_number=po_number,
                vendor=vendor,
                amount=amount,
            )
            return "approved"
        else:
            await self.persist_step(
                action="approval_gate",
                result=f"rejected_and_skipped: vendor={vendor}, amount={amount}, po={po_number or 'None'}, invoice_id={resolved_invoice_id}",
            )
            # Mark invoice as skipped in Neon. Pending-only: a human may have
            # settled the invoice through PATCH /api/invoices/[id] while the
            # gate was polling, and that settlement must stand.
            self.record_invoice_disposition(
                "rejected",
                invoice_id=resolved_invoice_id,
                po_number=po_number,
                vendor=vendor,
                amount=amount,
            )
            await self.mark_invoice_terminal_status(
                "skipped",
                invoice_id=resolved_invoice_id,
                po_number=po_number,
            )
            return "rejected"

    def _result_is_session_lost(self, result: Optional[Dict[str, Any]]) -> bool:
        """Classify a tool result dict as a closed-target CDP eviction."""
        if not isinstance(result, dict):
            return False
        if result.get("session_lost") is True:
            return True
        return is_session_lost_error(result.get("error"))

    def _tool_page_is_closed(self) -> bool:
        """Synchronous is_closed probe on the bound Playwright page."""
        page = getattr(self.tools, "page", None)
        if page is None:
            return False
        try:
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed):
                return bool(is_closed())
        except Exception:
            return True
        return False

    async def _try_reattach_session(self, reason: str) -> bool:
        """Bounded reattach: invoke reconnect hook and swap tools page."""
        if self._reattach_attempts >= self.max_reattaches:
            return False
        if self._reconnect is None:
            return False
        attempt = self._reattach_attempts + 1
        await self.emit_event(
            "session_lost",
            {"reason": reason, "attempt": attempt, "max_attempts": self.max_reattaches, "reattaching": True},
        )
        try:
            new_page = await self._reconnect()
        except Exception as e:
            self._reattach_attempts += 1
            logger.error(f"Agent {self.run_id}: CDP reattach attempt {attempt} failed: {e}")
            return False
        try:
            self.tools.set_page(new_page)
        except Exception:
            try:
                self.tools.page = new_page  # type: ignore[attr-defined]
            except Exception:
                self._reattach_attempts += 1
                return False
        self._reattach_attempts += 1
        logger.info(f"Agent {self.run_id}: CDP reattach attempt {attempt} succeeded, resuming.")
        await self.emit_event(
            "session_reattached",
            {"attempt": attempt, "max_attempts": self.max_reattaches},
        )
        return True

    async def _abort_session_lost(self, iteration: int, error: str, threshold: float, clean_goal: str) -> Dict[str, Any]:
        """Persist and broadcast terminal session_lost distinct from step_failed."""
        # Reached from deep inside an iteration (after OBSERVE or after ACT), so the
        # top-of-loop guard has already passed. Losing the lease must not turn into a
        # second, competing terminal announcement.
        if self._lease_ownership_lost:
            return self._lease_lost_result(iteration, clean_goal, threshold)
        session_lost_step_id = await self.persist_step(
            action="session_lost",
            result=f"session_lost: {error} after {self._reattach_attempts} reattach attempt(s)",
        )
        await self.update_run_status("session_lost")
        await self.emit_event(
            "session_lost",
            {
                "step_id": session_lost_step_id,
                "step": iteration,
                "error": error,
                "terminal": True,
                "reattached": False,
                "attempts": self._reattach_attempts,
                "max_attempts": self.max_reattaches,
            },
        )
        return {
            "run_id": self.run_id,
            "status": "session_lost",
            "iterations": iteration,
            "goal": clean_goal,
            "threshold": threshold,
            "summary": f"Browser session lost and reattach exhausted: {error}",
        }

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
        self._reattach_attempts = 0

        # 1. Ensure run record in database, taking the run lease. A refusal means
        # another live owner is already executing this run_id; bail out instead of
        # racing it, so a duplicate request cannot double-run an ERP workflow.
        accepted = await self.ensure_run_record(clean_goal, start_heartbeat=True)
        if not accepted:
            return {
                "run_id": self.run_id,
                "status": "failed",
                "iterations": 0,
                "goal": clean_goal,
                "threshold": settings.DEFAULT_APPROVAL_THRESHOLD,
                "summary": (
                    "Run is already leased by another active execution; "
                    "this request did not start it."
                ),
            }

        # 2. Extract constraints from goal
        threshold = extract_approval_threshold(clean_goal, default=settings.DEFAULT_APPROVAL_THRESHOLD)
        self.approval_threshold = threshold
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
            self._current_iteration = iteration
            # A snapshot not consumed last iteration (lease-lost and abort
            # returns skip post-execution) must not leak into this one and
            # attribute a later click to the wrong invoice.
            self._pending_submit_ref = None
            logger.info(f"Agent {self.run_id}: Iteration {iteration}/{self.max_iterations}")

            # Ownership check at the iteration boundary. This is the guard that stops a
            # superseded execution from resuming anything the heartbeat observed while
            # the previous iteration was running, including the pause wait below --
            # a pause polls Redis once a second for up to PAUSE_TIMEOUT_SECONDS, which
            # is more than enough time for the lease to be reclaimed.
            if self._lease_ownership_lost:
                return self._lease_lost_result(iteration, clean_goal, threshold)

            # Check pause flag in Redis (Phase 2.9 & Phase 3)
            if redis.is_configured:
                is_paused = await redis.get_pause_flag(self.run_id)
                if is_paused:
                    logger.info(f"Agent {self.run_id} is paused. Waiting for resume...")
                    await self.update_run_status("paused")
                    await self.emit_event("paused", {"step": iteration})
                    pause_deadline = asyncio.get_running_loop().time() + settings.PAUSE_TIMEOUT_SECONDS
                    while await redis.get_pause_flag(self.run_id):
                        # Re-checked on every poll rather than only at the iteration
                        # boundary: the wait below runs up to PAUSE_TIMEOUT_SECONDS, and
                        # a superseded execution must not sit there holding the pause
                        # open while a newer owner drives the same run.
                        if self._lease_ownership_lost:
                            return self._lease_lost_result(iteration, clean_goal, threshold)
                        if asyncio.get_running_loop().time() > pause_deadline:
                            logger.warning(f"Agent {self.run_id}: Pause wait timed out after {settings.PAUSE_TIMEOUT_SECONDS}s. Stalling run.")
                            try:
                                await self.update_run_status("stalled", release_lease=False)
                                await self.persist_step(
                                    action="stalled",
                                    result=f"Run paused and timed out after {settings.PAUSE_TIMEOUT_SECONDS}s.",
                                )
                                await self.emit_event("stalled", {"step": iteration, "reason": "pause_timeout"})
                                return {
                                    "run_id": self.run_id,
                                    "status": "stalled",
                                    "iterations": iteration,
                                    "goal": clean_goal,
                                    "threshold": threshold,
                                    "summary": f"Run paused and timed out after {settings.PAUSE_TIMEOUT_SECONDS}s.",
                                }
                            finally:
                                await self.stop_run_lease()
                        await asyncio.sleep(1.0)
                    logger.info(f"Agent {self.run_id} resumed.")
                    await self.update_run_status("running")
                    await self.emit_event("resumed", {"step": iteration})

            # Step 1: OBSERVE - call read_page() with bounded CDP reattach (Issue #31)
            if self._tool_page_is_closed():
                snapshot_res: Dict[str, Any] = {
                    "success": False,
                    "error": "Target page, context or browser has been closed",
                    "session_lost": True,
                }
            else:
                try:
                    snapshot_res = await self.tools.read_page()
                except Exception as obs_err:
                    snapshot_res = {"success": False, "error": str(obs_err)}
                    if is_session_lost_error(obs_err):
                        snapshot_res["session_lost"] = True
            if not snapshot_res.get("success") and self._result_is_session_lost(snapshot_res):
                recovered = False
                last_err = str(snapshot_res.get("error", "session lost"))
                while self._reattach_attempts < self.max_reattaches:
                    ok = await self._try_reattach_session(f"read_page: {last_err}")
                    if not ok:
                        if self._reconnect is None or self._reattach_attempts >= self.max_reattaches:
                            break
                        continue
                    try:
                        snapshot_res = await self.tools.read_page()
                    except Exception as obs_err2:
                        snapshot_res = {"success": False, "error": str(obs_err2)}
                        if is_session_lost_error(obs_err2):
                            snapshot_res["session_lost"] = True
                    if snapshot_res.get("success"):
                        recovered = True
                        break
                    if not self._result_is_session_lost(snapshot_res):
                        break
                    last_err = str(snapshot_res.get("error", "session lost"))
                if not snapshot_res.get("success") and self._result_is_session_lost(snapshot_res):
                    return await self._abort_session_lost(
                        iteration, str(snapshot_res.get("error", last_err)), threshold, clean_goal
                    )
                if recovered:
                    logger.info(f"Agent {self.run_id}: resumed after CDP reattach on observe.")
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

            # Step 2: THINK - Send to OpenCode Zen (Primary) with Groq fallback
            try:
                response_msg = await self.call_llm(
                    messages=iter_messages,
                    tools=TOOL_DEFINITIONS,
                )
            except Exception as llm_err:
                logger.error(f"LLM completion failed on iteration {iteration}: {llm_err}")
                llm_step_id = await self.persist_step(
                    action="llm_think",
                    result=f"failed: {llm_err}",
                )
                # The live event must carry the id of the row just persisted, or
                # the stream cannot correlate the two deliveries of this step.
                await self.emit_event(
                    "step_failed",
                    {"step_id": llm_step_id, "action": "llm_think", "error": str(llm_err)},
                )
                # Wait briefly and retry next iteration
                await asyncio.sleep(2.0)
                continue

            # Step 3: PARSE - Check if model signals 'done' or returns tool calls
            parsed_call = parse_tool_call(response_msg)

            if parsed_call is None:
                # No tool call returned; inspect if model says 'done'
                if is_goal_done(response_msg):
                    # The model saying "done" is not this execution's to publish once
                    # the run belongs to somebody else: the durable status write below
                    # is fenced, but the step row, the Redis snapshot and the `done`
                    # event are not, and a client would read them as this run's outcome.
                    if self._lease_ownership_lost:
                        return self._lease_lost_result(iteration, clean_goal, threshold)
                    logger.info(f"Agent {self.run_id}: Goal finished successfully.")
                    msg_content = response_msg.get("content") if isinstance(response_msg, dict) else getattr(response_msg, "content", None)
                    final_summary = msg_content or "Task completed successfully."
                    run_status = "completed"
                    try:
                        await self.update_run_status("completed", release_lease=False)
                        # Settle every invoice this run processed to its terminal
                        # state (completed / flagged / failed; rejected ones were
                        # already marked skipped at the gate). Pending-only, so a
                        # human settlement always wins. Best-effort: a finalize
                        # failure must never break completion. Emits nothing when
                        # the run tracked no invoices, keeping this path
                        # byte-identical for runs that never touched invoices.
                        try:
                            settle_counts = await self._finalize_processed_invoices()
                        except Exception as fin_err:
                            logger.warning(
                                f"Agent {self.run_id}: invoice finalize failed: {fin_err}"
                            )
                            settle_counts = {}
                        settled_total = sum(
                            settle_counts.get(k, 0)
                            for k in ("completed", "flagged", "failed", "skipped")
                        )
                        if settled_total:
                            settle_result = (
                                f"settled {settled_total} invoice(s): "
                                + ", ".join(
                                    f"{k}={settle_counts.get(k, 0)}"
                                    for k in ("completed", "flagged", "failed", "skipped")
                                )
                            )
                            try:
                                await self.persist_step(
                                    action="finalize_invoices",
                                    result=settle_result,
                                )
                                await self.emit_event(
                                    "invoices_finalized",
                                    {"settled": settled_total, "counts": settle_counts},
                                )
                            except Exception as obs_err:
                                logger.warning(
                                    f"Agent {self.run_id}: finalize observability failed: {obs_err}"
                                )
                        await self.persist_step(
                            action="done",
                            result=final_summary,
                        )
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
                    finally:
                        await self.stop_run_lease()
                else:
                    # Model provided text but no tool call; check if model flagged approval in prose
                    text_content = (response_msg.get("content") if isinstance(response_msg, dict) else getattr(response_msg, "content", None)) or ""
                    is_approval_statement = bool(
                        re.search(r"(?:needs|requires|awaiting|requesting|hold(?:ing)?\s+for)\s+approval", text_content, re.IGNORECASE)
                        or re.search(r"(?:exceeds|above|over)\s+(?:the\s+)?(?:approval\s+)?threshold", text_content, re.IGNORECASE)
                    )
                    detected_amount = parse_amount(self._active_form_state.get("amount"))
                    if not detected_amount and text_content:
                        curr_match = re.search(r"(?:₹|rs\.?|inr|\$|usd|eur|gbp|£|€)\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", text_content, re.IGNORECASE)
                        if curr_match:
                            detected_amount = parse_amount(curr_match.group(0))
                    if is_approval_statement and detected_amount and self.check_amount_exceeds_threshold(detected_amount, threshold):
                        detected_po = extract_target_po(text_content) or self._active_form_state.get("po_number")
                        detected_vendor = self._active_form_state.get("vendor") or extract_vendor_filter(text_content) or "Unknown Vendor"
                        logger.info(f"Agent {self.run_id}: LLM declared approval needed for {detected_vendor}, amount={detected_amount}, po={detected_po}")
                        gate_outcome = await self.handle_approval_gate(
                            vendor=detected_vendor,
                            amount=detected_amount,
                            invoice_id=self._active_form_state.get("invoice_id"),
                            po_number=detected_po,
                        )
                        if gate_outcome == "stalled":
                            return {
                                "run_id": self.run_id,
                                "status": "stalled",
                                "iterations": iteration,
                                "goal": clean_goal,
                                "threshold": threshold,
                                "summary": "Approval gate did not receive a decision or timed out.",
                            }
                        if gate_outcome == "lease_lost":
                            return self._lease_lost_result(iteration, clean_goal, threshold)
                        messages.append({"role": "assistant", "content": text_content})
                        if gate_outcome == "approved":
                            messages.append({
                                "role": "user",
                                "content": (
                                    f"Human supervisor APPROVED invoice {detected_po or ''} ({detected_vendor}, ₹{detected_amount:,.2f}). "
                                    "Proceed to submit or finalize the invoice in the ERP."
                                ),
                            })
                        else:
                            self._active_form_state.clear()
                            messages.append({
                                "role": "user",
                                "content": (
                                    f"Human supervisor REJECTED invoice {detected_po or ''} ({detected_vendor}, ₹{detected_amount:,.2f}). "
                                    "The invoice has been marked as 'skipped' in the database. "
                                    "Do NOT submit this invoice. Move on to the next invoice."
                                ),
                            })
                        continue
                    else:
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

            # Track form fields if filling invoice inputs (Phase 2.5 Idempotency Guard)
            if tool_name == "fill":
                sel = str(tool_args.get("selector", "")).lower()
                val = tool_args.get("value", "")
                if "amount" in sel:
                    self._active_form_state["amount"] = val
                elif "po" in sel or "identifier" in sel or str(val).upper().startswith("PO-"):
                    self._active_form_state["po_number"] = val
                    clean_val = str(val).strip()
                    if clean_val:
                        # Idempotency check before filling PO number or identifier
                        check_res = await self.check_idempotency("invoice", clean_val)
                        if check_res.get("error"):
                            logger.warning(
                                f"Agent {self.run_id}: Idempotency check failed with error before fill: {check_res.get('error')}"
                            )
                            tool_result = {
                                "success": False,
                                "aborted": True,
                                "error": check_res.get("error"),
                                "entity_type": "invoice",
                                "identifier": clean_val,
                                "message": (
                                    f"Idempotency check failed with error: {check_res.get('error')}. "
                                    "Aborting form fill to prevent potential duplicate invoice creation."
                                ),
                            }
                            await self.persist_step(
                                action="idempotency_check",
                                result=f"aborted_error: {check_res.get('error')}",
                            )
                            await self.emit_event(
                                "idempotency_aborted",
                                {
                                    "step": iteration,
                                    "action": "fill",
                                    "entity_type": "invoice",
                                    "identifier": clean_val,
                                    "error": check_res.get("error"),
                                },
                            )
                            self._active_form_state.clear()
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
                        elif check_res.get("exists"):
                            logger.warning(
                                f"Agent {self.run_id}: Idempotency abort before fill: invoice '{clean_val}' already exists in database."
                            )
                            tool_result = {
                                "success": False,
                                "aborted": True,
                                "exists": True,
                                "entity_type": "invoice",
                                "identifier": clean_val,
                                "record": check_res.get("record"),
                                "message": (
                                    f"Idempotency check failed: Invoice with identifier '{clean_val}' already exists in database. "
                                    "Aborting form fill and invoice creation to prevent duplicate entry."
                                ),
                            }
                            await self.persist_step(
                                action="idempotency_check",
                                result=f"aborted_duplicate: invoice {clean_val} already exists",
                            )
                            await self.emit_event(
                                "idempotency_aborted",
                                {
                                    "step": iteration,
                                    "action": "fill",
                                    "entity_type": "invoice",
                                    "identifier": clean_val,
                                    "record": check_res.get("record"),
                                },
                            )
                            self._active_form_state.clear()
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
                            is_implicit_submit = await elem.evaluate(
                                """el => el.tagName && el.tagName.toLowerCase() === "button" &&
                                el.form !== null &&
                                !["button", "reset"].includes(
                                    (el.getAttribute("type") || "submit").toLowerCase()
                                )"""
                            )
                            if (
                                elem_type == "submit"
                                or is_implicit_submit
                                or any(k in combined_text for k in ("submit", "create invoice", "save invoice", "save"))
                            ):
                                is_submit_action = True
                    except Exception as elem_err:
                        logger.debug(f"Could not inspect element attributes for '{selector}': {elem_err}")

                if is_submit_action:
                    # Phase 2.5: Idempotency enforcement before submit
                    target_po = self._active_form_state.get("po_number")
                    if not target_po and getattr(self.tools, "page", None):
                        try:
                            po_elem = self.tools.page.locator(
                                'input[name*="po" i], input[id*="po" i], #po_number'
                            ).first
                            if po_elem and await po_elem.count() > 0:
                                page_po_val = await po_elem.input_value()
                                if page_po_val and page_po_val.strip():
                                    target_po = page_po_val.strip()
                                    self._active_form_state["po_number"] = target_po
                        except Exception as po_err:
                            logger.debug(f"Could not read po_number from page DOM: {po_err}")

                    if not target_po:
                        target_po = extract_target_po(clean_goal)

                    if target_po:
                        check_res = await self.check_idempotency("invoice", str(target_po).strip())
                        if check_res.get("error"):
                            logger.warning(
                                f"Agent {self.run_id}: Idempotency check failed with error before submit: {check_res.get('error')}"
                            )
                            tool_result = {
                                "success": False,
                                "aborted": True,
                                "error": check_res.get("error"),
                                "entity_type": "invoice",
                                "identifier": str(target_po).strip(),
                                "message": (
                                    f"Idempotency check failed with error: {check_res.get('error')}. "
                                    "Aborting submission to prevent potential duplicate invoice creation."
                                ),
                            }
                            await self.persist_step(
                                action="idempotency_check",
                                result=f"aborted_error: {check_res.get('error')}",
                            )
                            await self.emit_event(
                                "idempotency_aborted",
                                {
                                    "step": iteration,
                                    "action": "click_submit",
                                    "entity_type": "invoice",
                                    "identifier": str(target_po).strip(),
                                    "error": check_res.get("error"),
                                },
                            )
                            self._active_form_state.clear()
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
                        elif check_res.get("exists"):
                            logger.warning(
                                f"Agent {self.run_id}: Idempotency abort before submit: invoice '{target_po}' already exists in database."
                            )
                            tool_result = {
                                "success": False,
                                "aborted": True,
                                "exists": True,
                                "entity_type": "invoice",
                                "identifier": str(target_po).strip(),
                                "record": check_res.get("record"),
                                "message": (
                                    f"Idempotency check failed: Invoice with identifier '{target_po}' already exists in database. "
                                    "Aborting submission to prevent duplicate."
                                ),
                            }
                            await self.persist_step(
                                action="idempotency_check",
                                result=f"aborted_duplicate: invoice {target_po} already exists",
                            )
                            await self.emit_event(
                                "idempotency_aborted",
                                {
                                    "step": iteration,
                                    "action": "click_submit",
                                    "entity_type": "invoice",
                                    "identifier": str(target_po).strip(),
                                    "record": check_res.get("record"),
                                },
                            )
                            self._active_form_state.clear()
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

                if is_submit_action and bool(self._active_form_state):
                    active_amount = self._active_form_state.get("amount")
                    if active_amount is None and getattr(self.tools, "page", None):
                        try:
                            amt_elem = self.tools.page.locator('input[name*="amount" i], input[id*="amount" i], input[type="number"]').first
                            if amt_elem:
                                val = await amt_elem.input_value()
                                if val:
                                    active_amount = val
                                    self._active_form_state["amount"] = val
                        except Exception:
                            pass

                    parsed_amt = None
                    if active_amount is not None:
                        try:
                            clean_str = str(active_amount).replace(",", "").replace("$", "").replace("₹", "").replace("INR", "").replace("Rs.", "").strip()
                            val = float(clean_str)
                            if math.isfinite(val) and val > 0:
                                parsed_amt = val
                        except (ValueError, TypeError):
                            parsed_amt = None

                    if active_amount is None or parsed_amt is None:
                        logger.warning(f"Agent {self.run_id}: Invoice submission blocked due to missing or invalid amount: '{active_amount}'")
                        tool_result = {
                            "success": False,
                            "rejected": True,
                            "message": f"Invoice submission rejected: invalid or missing amount '{active_amount}'. Invoice submission requires a valid numeric amount.",
                        }
                        await self.persist_step(
                            action="approval_gate",
                            result="rejected_invalid_amount",
                        )
                        self._active_form_state.clear()
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
                        iteration += 1
                        continue

                    if self.check_amount_exceeds_threshold(parsed_amt, threshold):
                        logger.info(f"Detected submission of invoice exceeding threshold ({parsed_amt} > {threshold})")
                        target_vendor = self._active_form_state.get("vendor", "Unknown Vendor")
                        target_po = self._active_form_state.get("po_number")
                        target_inv_id = self._active_form_state.get("invoice_id")

                        gate_outcome = await self.handle_approval_gate(
                            vendor=target_vendor,
                            amount=parsed_amt,
                            invoice_id=target_inv_id,
                            po_number=target_po,
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
                        if gate_outcome == "lease_lost":
                            # Checked before the `!= "approved"` arm below, which treats
                            # any other outcome as a rejection and marks the invoice
                            # skipped in Neon -- a durable ERP write by an execution
                            # that no longer owns the run.
                            return self._lease_lost_result(iteration, clean_goal, threshold)
                        if gate_outcome != "approved":
                            # Rejection: skip submission, reset active form, inform LLM (Phase 2.7)
                            tool_result = {
                                "success": False,
                                "rejected": True,
                                "message": (
                                    f"Invoice submission rejected by human supervisor. "
                                    f"Invoice for vendor '{target_vendor}' (Amount: ₹{parsed_amt:,.2f}, PO: {target_po or 'N/A'}) "
                                    f"has been marked as 'skipped' in the database. "
                                    f"Do NOT retry this invoice. Move on to the next invoice."
                                ),
                            }
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

                    # Reaching here means the submit will execute (every abort
                    # above continued): snapshot which invoice it is for, so a
                    # successful click records the "submitted" disposition the
                    # run-completion path settles to a terminal status.
                    self._pending_submit_ref = dict(self._active_form_state)

            # The load-bearing guard. Everything above this point in the iteration
            # (OBSERVE, the Groq THINK call, the approval gate) can take long enough
            # for the heartbeat to observe that the lease is gone, and
            # `tools.execute` on click/fill/select is the side-effecting ERP action the
            # lease exists to keep single-owner. It also runs ahead of the `step_start`
            # event so the stream never shows a step that was not taken.
            if self._lease_ownership_lost:
                return self._lease_lost_result(iteration, clean_goal, threshold)

            step_id = str(uuid.uuid4())
            # Execute Tool with recovery wrap (Phase 2.8)
            await self.emit_event(
                "step_start",
                {
                    "step_id": step_id,
                    "step_index": iteration,
                    "action": tool_name,
                    "arguments": tool_args,
                },
            )

            # Re-checked after step_start, which awaits the on_event callback and the
            # hub publish. A single check cannot cover a window it does not span: the
            # heartbeat reports ownership loss on its own schedule, and the action
            # itself must not begin on a lease this execution has already lost.
            if self._lease_ownership_lost:
                # step_start is already published for this step_id, so returning here
                # would leave it dangling: the frontend opens a step row on step_start
                # and only resolves it on step_complete / step_failed / step_unknown,
                # so the row would spin for the life of the stream. Close it under the
                # same step_id.
                #
                # Emitted live only, with no persist_step. Persisting here would mean a
                # superseded execution writing a durable row after losing ownership,
                # which is the rule every other lease-loss return here observes. Nothing
                # is lost by not persisting: the action never ran, so there is no step
                # to replay, and the run's terminal status belongs to the new owner.
                await self.emit_event(
                    "step_failed",
                    {
                        "step_id": step_id,
                        "step_index": iteration,
                        "action": tool_name,
                        "error": "aborted: run lease was lost before this action executed",
                    },
                )
                return self._lease_lost_result(iteration, clean_goal, threshold)

            screenshot_on_fail: Optional[str] = None
            try:
                tool_result = await self.tools.execute(tool_name, tool_args, step_id=step_id)
                tool_success = tool_result.get("success", False)
                if "exists" in tool_result and not tool_result.get("error"):
                    tool_success = True
            except Exception as exec_err:
                logger.error(f"Error executing tool '{tool_name}': {exec_err}")
                tool_result = {"success": False, "error": str(exec_err)}
                if is_session_lost_error(exec_err):
                    tool_result["session_lost"] = True
                tool_success = False

            outcome_unknown = False
            if not tool_success and self._result_is_session_lost(tool_result):
                last_err = str(tool_result.get("error", "session lost"))
                recovered_act = False
                is_mutating_tool = tool_name in ("click", "fill", "select")
                while self._reattach_attempts < self.max_reattaches:
                    ok = await self._try_reattach_session(f"{tool_name}: {last_err}")
                    if not ok:
                        if self._reconnect is None or self._reattach_attempts >= self.max_reattaches:
                            break
                        continue
                    if is_mutating_tool:
                        # Non-idempotent: do not replay on the fresh page; the
                        # first attempt may already have taken effect. Record
                        # outcome-unknown instead of success/failure.
                        recovered_act = True
                        outcome_unknown = True
                        break
                    try:
                        retry_res = await self.tools.execute(tool_name, tool_args, step_id=step_id)
                        retry_ok = retry_res.get("success", False)
                        if "exists" in retry_res and not retry_res.get("error"):
                            retry_ok = True
                    except Exception as exec_err2:
                        retry_res = {"success": False, "error": str(exec_err2)}
                        if is_session_lost_error(exec_err2):
                            retry_res["session_lost"] = True
                        retry_ok = False
                    if retry_ok:
                        tool_result = retry_res
                        tool_success = True
                        recovered_act = True
                        break
                    if not self._result_is_session_lost(retry_res):
                        tool_result = retry_res
                        tool_success = False
                        recovered_act = True
                        break
                    tool_result = retry_res
                    last_err = str(retry_res.get("error", "session lost"))
                if not tool_success and self._result_is_session_lost(tool_result) and not recovered_act:
                    return await self._abort_session_lost(
                        iteration, str(tool_result.get("error", last_err)), threshold, clean_goal
                    )
                if recovered_act:
                    logger.info(f"Agent {self.run_id}: resumed after CDP reattach on act '{tool_name}'.")

            # Phase 2.5: Cache explicit check_exists results (positive matches only)
            if tool_name == "check_exists" and tool_success and isinstance(tool_result, dict):
                e_type = str(tool_args.get("entity_type", "")).strip().lower()
                e_ident = str(tool_args.get("identifier", "")).strip()
                if e_ident and tool_result.get("exists") and not tool_result.get("error"):
                    self._checked_entities[f"{e_type}:{e_ident.lower()}"] = tool_result

            if not tool_success and outcome_unknown:
                # Mutating action reattached without replay: outcome is unknown,
                # not failed. Persist distinctly; skip the failure screenshot
                # (the fresh page would mislead) and step_failed emission.
                # The submit did not observably succeed, so drop its snapshot.
                self._pending_submit_ref = None
                unknown_step_id = await self.persist_step(
                    action=tool_name,
                    result=f"outcome unknown after CDP reattach (not replayed): {tool_result.get('error')}",
                    step_id=step_id,
                )
                await self.emit_event(
                    "step_unknown",
                    {
                        "step_id": unknown_step_id,
                        "step_index": iteration,
                        "action": tool_name,
                        "arguments": tool_args,
                        "error": tool_result.get("error"),
                        "outcome_unknown": True,
                    },
                )
            elif not tool_success:
                # Capture diagnostic screenshot on failure without creating duplicate rows (Phase 2.8)
                # A failed submit is not an observed submission: drop its snapshot.
                self._pending_submit_ref = None
                failed_step_id = await self.persist_step(
                    action=tool_name,
                    result=f"failed: {tool_result.get('error')}",
                    step_id=step_id,
                )
                has_sc = False
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
                        has_sc = True
                    elif sc and sc.get("screenshot_b64"):
                        has_sc = True
                except Exception as sc_err:
                    logger.warning(f"Failure screenshot not captured: {sc_err}")

                await self.emit_event(
                    "step_failed",
                    {
                        "step_id": failed_step_id,
                        "step_index": iteration,
                        "action": tool_name,
                        "arguments": tool_args,
                        "error": tool_result.get("error"),
                        "has_screenshot": has_sc,
                    },
                )
            else:
                # Persist successful step to Neon (Phase 2.6)
                # A successful submit click is this run's observed submission
                # of the snapshotted invoice: record it for the completion
                # flush, then drop the snapshot.
                if tool_name == "click" and self._pending_submit_ref:
                    ref = self._pending_submit_ref
                    self._pending_submit_ref = None
                    self.record_invoice_disposition(
                        "submitted",
                        invoice_id=ref.get("invoice_id"),
                        po_number=ref.get("po_number"),
                        vendor=ref.get("vendor"),
                        amount=ref.get("amount"),
                    )
                result_summary = "success"
                sc_b64 = None
                if tool_name == "read_page":
                    result_summary = f"read_page: {tool_result.get('size_bytes', 0)} bytes (url: {tool_result.get('url')})"
                elif tool_name == "navigate":
                    result_summary = f"navigated to {tool_result.get('url')} (status: {tool_result.get('status', 200)})"
                elif tool_name == "click":
                    result_summary = f"clicked {tool_args.get('selector')}"
                elif tool_name == "fill":
                    result_summary = f"filled {tool_args.get('selector')} = {tool_result.get('value')}"
                elif tool_name == "select":
                    result_summary = f"selected {tool_result.get('selected')}"
                elif tool_name == "check_exists":
                    result_summary = f"check_exists({tool_args.get('entity_type')}, {tool_args.get('identifier')}): exists={tool_result.get('exists')}"
                elif tool_name == "take_screenshot":
                    result_summary = f"screenshot captured ({tool_result.get('size_bytes', 0)} bytes)"
                    sc_b64 = tool_result.get("screenshot_b64")

                # If take_screenshot was already persisted by tools.take_screenshot, avoid duplicate row
                # (that row was written under this same step_id, so the live event below still
                # identifies the one durable row)
                if not (tool_name == "take_screenshot" and tool_result.get("persisted")):
                    await self.persist_step(
                        action=tool_name,
                        result=result_summary,
                        screenshot_b64=sc_b64,
                        step_id=step_id,
                    )
                await self.emit_event(
                    "step_complete",
                    {
                        "step_id": step_id,
                        "step_index": iteration,
                        "action": tool_name,
                        "result": result_summary,
                        "has_screenshot": bool(sc_b64),
                    },
                )
                await self.emit_event(
                    "step",
                    {
                        "step_index": iteration,
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
            # Outside the loop, so the iteration-boundary guard cannot cover it. A
            # `stalled` written here over a run another owner now holds would end that
            # owner's run, and the Redis snapshot and `stalled` event would announce it.
            if self._lease_ownership_lost:
                return self._lease_lost_result(iteration, clean_goal, threshold)
            logger.warning(f"Agent {self.run_id} hit hard cap of {self.max_iterations} iterations. Marking as stalled.")
            run_status = "stalled"
            try:
                await self.update_run_status("stalled", release_lease=False)
                await self.persist_step(
                    action="stalled",
                    result=f"Exceeded maximum iteration cap of {self.max_iterations} steps.",
                )
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
            finally:
                await self.stop_run_lease()

        return {
            "run_id": self.run_id,
            "status": run_status,
            "iterations": iteration,
            "goal": clean_goal,
            "threshold": threshold,
            "summary": final_summary,
        }
