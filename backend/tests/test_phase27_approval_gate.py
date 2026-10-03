import asyncio
import json
import unittest
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport
from groq import AsyncGroq

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, get_db_pool
from backend.tools import PlaywrightTools
from backend.redis_client import UpstashRedisClient, get_redis_client
from backend.agent import (
    extract_approval_threshold,
    extract_vendor_filter,
    extract_target_po,
    parse_amount,
    ReActAgent,
)
from backend.main import app


class TestPhase27ApprovalGateUnit(unittest.TestCase):
    """Unit tests for Phase 2.7 approval threshold extraction, parsing, and offline gate logic."""

    def test_01_extract_approval_threshold_variations(self):
        """Verify dynamic monetary approval threshold extraction from diverse plain-English goals."""
        # Default when no threshold is present
        self.assertEqual(extract_approval_threshold("Process all pending invoices"), 50000.0)
        self.assertEqual(extract_approval_threshold("Process only invoices from Vendor Acme"), 50000.0)
        self.assertEqual(extract_approval_threshold(""), 50000.0)

        # Standard INR currency prefix with commas
        self.assertEqual(extract_approval_threshold("Hold anything over ₹25,000 for approval"), 25000.0)
        self.assertEqual(extract_approval_threshold("Hold anything over ₹50,000 for approval"), 50000.0)
        self.assertEqual(extract_approval_threshold("Flag anything above ₹ 100,000"), 100000.0)
        self.assertEqual(extract_approval_threshold("Threshold at ₹75,000"), 75000.0)

        # Alternative currency notations: Rs., Rs, INR, $
        self.assertEqual(extract_approval_threshold("Invoices exceeding Rs. 35,000 need approval"), 35000.0)
        self.assertEqual(extract_approval_threshold("Hold anything over Rs 45000"), 45000.0)
        self.assertEqual(extract_approval_threshold("Require approval for anything over INR 80,000"), 80000.0)
        self.assertEqual(extract_approval_threshold("Hold anything over $60,000 for approval"), 60000.0)

        # Plain numbers with comparative keywords
        self.assertEqual(extract_approval_threshold("Hold anything over 30000 for approval"), 30000.0)
        self.assertEqual(extract_approval_threshold("Invoices greater than 65000 need manager signoff"), 65000.0)

    def test_02_parse_amount_robustness(self):
        """Verify parse_amount accurately parses various currency and formatted string inputs."""
        self.assertEqual(parse_amount(50000), 50000.0)
        self.assertEqual(parse_amount(62000.50), 62000.50)
        self.assertEqual(parse_amount("62,000.00"), 62000.0)
        self.assertEqual(parse_amount("₹62,000"), 62000.0)
        self.assertEqual(parse_amount("Rs. 76,500.00"), 76500.0)
        self.assertEqual(parse_amount("INR 90,000"), 90000.0)
        self.assertEqual(parse_amount("$12,345.67"), 12345.67)
        self.assertEqual(parse_amount(None), 0.0)
        self.assertEqual(parse_amount(""), 0.0)
        self.assertEqual(parse_amount("abc"), 0.0)

    def test_03_check_amount_exceeds_threshold(self):
        """Verify threshold comparison logic in ReActAgent."""
        agent = ReActAgent(run_id=str(uuid.uuid4()), tools=MagicMock(spec=PlaywrightTools))
        # Strictly exceeds threshold (> 50,000)
        self.assertTrue(agent.check_amount_exceeds_threshold(50001, 50000.0))
        self.assertTrue(agent.check_amount_exceeds_threshold("62,000", 50000.0))
        self.assertTrue(agent.check_amount_exceeds_threshold("₹76,500", 50000.0))

        # At or below threshold
        self.assertFalse(agent.check_amount_exceeds_threshold(50000, 50000.0))
        self.assertFalse(agent.check_amount_exceeds_threshold(49999.99, 50000.0))
        self.assertFalse(agent.check_amount_exceeds_threshold("25,000", 50000.0))

    def test_04_approval_gate_unconfigured_redis_stalls(self):
        """Verify that handle_approval_gate fails closed and stalls if Redis is not configured."""
        mock_redis = MagicMock(spec=UpstashRedisClient)
        mock_redis.is_configured = False

        agent = ReActAgent(
            run_id=str(uuid.uuid4()),
            tools=MagicMock(spec=PlaywrightTools),
            redis_client=mock_redis,
        )
        agent.update_run_status = AsyncMock()
        agent.emit_event = AsyncMock()

        outcome = asyncio.run(
            agent.handle_approval_gate(
                vendor="Acme Corp",
                amount=62000.0,
                po_number="PO-1006",
            )
        )
        self.assertEqual(outcome, "stalled")
        agent.update_run_status.assert_called_with("stalled")
        agent.emit_event.assert_called_with(
            "approval_failed",
            {"run_id": agent.run_id, "error": "Redis not configured"},
        )

    def test_05_missing_nonce_on_pending_fails_closed(self):
        """Verify POST approval fails closed (400) when pending record has no nonce."""
        async def _run():
            import time
            from backend.auth import Session, require_session
            test_run_id = f"test-nonce-missing-{uuid.uuid4().hex[:6]}"
            mock_redis = MagicMock()
            mock_redis.get_approval_pending = AsyncMock(
                return_value={
                    "run_id": test_run_id,
                    "status": "awaiting_approval",
                    "vendor": "Acme Corp",
                    "amount": 85000.0,
                }
            )
            mock_redis.set_approval_decision = AsyncMock(return_value=True)
            app.dependency_overrides[require_session] = lambda: Session(
                sub="operator", exp=int(time.time()) + 3600
            )
            try:
                with patch("backend.redis_client.get_redis_client", return_value=mock_redis):
                    transport = ASGITransport(app=app)
                    async with AsyncClient(transport=transport, base_url="http://test") as client:
                        res_with_nonce = await client.post(
                            f"/agent/runs/{test_run_id}/approval",
                            json={"decision": "approved", "nonce": "anything"},
                        )
                        self.assertEqual(res_with_nonce.status_code, 400)
                        res_no_nonce = await client.post(
                            f"/agent/runs/{test_run_id}/approval",
                            json={"decision": "approved"},
                        )
                        self.assertEqual(res_no_nonce.status_code, 400)
                        res_reject = await client.post(
                            f"/agent/runs/{test_run_id}/approval",
                            json={"decision": "rejected", "nonce": "anything"},
                        )
                        self.assertEqual(res_reject.status_code, 400)
                        mock_redis.set_approval_decision.assert_not_called()
            finally:
                app.dependency_overrides.pop(require_session, None)
        asyncio.run(_run())

    def test_06_bare_string_decision_not_accepted(self):
        """Verify bare-string and nonce-less decisions are not accepted as approved."""
        async def _run_redis():
            redis = UpstashRedisClient(url="http://127.0.0.1:1", token="dummy")
            redis.execute_command = AsyncMock(return_value="approved")
            self.assertIsNone(await redis.get_approval_decision_record("run-123"))
            redis.execute_command = AsyncMock(
                return_value=json.dumps({"decision": "approved"})
            )
            self.assertIsNone(await redis.get_approval_decision_record("run-123"))
            redis.execute_command = AsyncMock(
                return_value=json.dumps({"decision": "approved", "nonce": None})
            )
            self.assertIsNone(await redis.get_approval_decision_record("run-123"))
            redis.execute_command = AsyncMock(
                return_value=json.dumps({"decision": "approved", "nonce": "abc123"})
            )
            rec = await redis.get_approval_decision_record("run-123")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["decision"], "approved")
            self.assertEqual(rec["nonce"], "abc123")
        asyncio.run(_run_redis())

        async def _run_agent():
            import dataclasses
            import backend.agent as agent_module
            orig_settings = agent_module.settings
            agent_module.settings = dataclasses.replace(
                orig_settings, APPROVAL_TIMEOUT_SECONDS=0.3
            )
            try:
                mock_redis = MagicMock(spec=UpstashRedisClient)
                mock_redis.is_configured = True
                mock_redis.execute_command = AsyncMock(return_value=None)
                mock_redis.set_approval_pending = AsyncMock(return_value=True)
                mock_redis.set_session_state = AsyncMock(return_value=True)
                mock_redis.get_approval_decision_record = AsyncMock(
                    return_value={"decision": "approved", "nonce": None}
                )
                mock_redis.clear_approval = AsyncMock(return_value=True)
                agent = ReActAgent(
                    run_id=str(uuid.uuid4()),
                    tools=MagicMock(spec=PlaywrightTools),
                    redis_client=mock_redis,
                )
                agent.update_run_status = AsyncMock()
                agent.emit_event = AsyncMock()
                agent.persist_step = AsyncMock(return_value="step-id")
                outcome = await agent.handle_approval_gate(
                    vendor="Acme Corp",
                    amount=62000.0,
                    invoice_id="inv-123",
                    po_number="PO-1006",
                )
                self.assertEqual(outcome, "stalled")
            finally:
                agent_module.settings = orig_settings
        asyncio.run(_run_agent())


@unittest.skipUnless(
    bool(settings.DATABASE_URL and settings.UPSTASH_REDIS_REST_URL),
    "Live Neon DB or Upstash Redis not configured",
)
class TestPhase27ApprovalGateIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test suite against live Neon Postgres and Upstash Redis for Phase 2.7 Approval Gate."""

    async def asyncSetUp(self):
        """Initialize database connection pool, Redis client, and test tracking lists."""
        await init_db_pool()
        self.pool = await get_db_pool()
        self.redis = UpstashRedisClient()
        self.created_run_ids: List[str] = []
        self.created_invoice_ids: List[uuid.UUID] = []

    async def asyncTearDown(self):
        """Clean up test invoices, runs, steps from Neon and clear Redis test keys."""
        if self.pool:
            try:
                async with self.pool.acquire() as conn:
                    if self.created_invoice_ids:
                        await conn.execute("DELETE FROM invoices WHERE id = ANY($1::uuid[]);", self.created_invoice_ids)
                    if self.created_run_ids:
                        valid_run_uuids = []
                        for r in self.created_run_ids:
                            try:
                                valid_run_uuids.append(uuid.UUID(str(r)))
                            except (ValueError, TypeError):
                                pass
                        if valid_run_uuids:
                            await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_run_uuids)
                            await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_run_uuids)
            except Exception as e:
                pass

        # Clean up Redis keys
        for r_id in self.created_run_ids:
            try:
                await self.redis.clear_approval(r_id)
                await self.redis.execute_command("DEL", f"hulchul:run:{r_id}")
            except Exception:
                pass

        await self.redis.close()
        await close_db_pool()

    async def test_05_handle_approval_gate_approved_flow(self):
        """
        Phase 2.7: Verify handle_approval_gate emits needs_approval SSE event,
        persists awaiting_approval in Redis and Neon, polls Redis every 2s,
        and proceeds on approval decision.
        """
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()

        emitted_events: List[Dict[str, Any]] = []

        async def record_event(evt: Dict[str, Any]):
            """Record emitted agent event for test validation."""
            emitted_events.append(evt)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            redis_client=self.redis,
            on_event=record_event,
        )
        await agent.ensure_run_record(goal="Process invoice PO-1006 over ₹50,000 threshold")

        # Simulate human approval injected via Redis
        async def background_approval_injector():
            """Background task to simulate human approval injection via Redis."""
            for _ in range(40):
                await asyncio.sleep(0.2)
                pending = await self.redis.get_approval_pending(test_run_id)
                if pending and pending.get("status") == "awaiting_approval":
                    self.assertEqual(pending["amount"], 62000.0)
                    self.assertEqual(pending["vendor"], "Acme Corp")
                    self.assertEqual(pending["po_number"], "PO-1006")
                    nonce = pending.get("nonce")
                    await self.redis.set_approval_decision(test_run_id, "approved", nonce=nonce)
                    return

        task1 = asyncio.create_task(background_approval_injector())

        outcome = await agent.handle_approval_gate(
            vendor="Acme Corp",
            amount=62000.0,
            invoice_id="inv-test-approval",
            po_number="PO-1006",
        )
        await task1

        self.assertEqual(outcome, "approved")

        # Verify emitted SSE events
        needs_app_events = [e for e in emitted_events if e["type"] == "needs_approval"]
        self.assertEqual(len(needs_app_events), 1)
        app_evt = needs_app_events[0]
        self.assertEqual(app_evt["invoice_id"], "inv-test-approval")
        self.assertEqual(app_evt["vendor"], "Acme Corp")
        self.assertEqual(app_evt["amount"], 62000.0)
        self.assertEqual(app_evt["po_number"], "PO-1006")

        resolved_events = [e for e in emitted_events if e["type"] == "approval_resolved"]
        self.assertEqual(len(resolved_events), 1)
        self.assertEqual(resolved_events[0]["decision"], "approved")

        # Verify Neon steps were persisted
        steps = await agent.get_steps(include_screenshots=False)
        step_actions = [s["action"] for s in steps]
        self.assertIn("approval_gate", step_actions)

        # Verify Redis approval keys were cleaned up
        self.assertIsNone(await self.redis.get_approval_pending(test_run_id))
        self.assertIsNone(await self.redis.get_approval_decision(test_run_id))

    async def test_06_handle_approval_gate_rejected_marks_skipped_in_neon(self):
        """
        Phase 2.7: Verify handle_approval_gate on rejection marks the invoice
        as 'skipped' in Neon database and cleans up Redis state.
        """
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)

        # Insert a test invoice in Neon with 'pending' status
        test_po = f"PO-REJ-{uuid.uuid4().hex[:6]}"
        async with self.pool.acquire() as conn:
            inv_row = await conn.fetchrow(
                """
                INSERT INTO invoices (vendor, amount, date, po_number, status)
                VALUES ('Bharat Supplies', 76500.00, CURRENT_DATE, $1, 'pending')
                RETURNING id;
                """,
                test_po,
            )
            test_inv_uuid = inv_row["id"]
            self.created_invoice_ids.append(test_inv_uuid)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()

        emitted_events: List[Dict[str, Any]] = []

        async def record_event(evt: Dict[str, Any]):
            """Record emitted agent event for test validation."""
            emitted_events.append(evt)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            redis_client=self.redis,
            on_event=record_event,
        )
        await agent.ensure_run_record(goal="Test rejection flow")

        # Simulate human rejection injected via Redis with polling
        async def background_rejection_injector():
            """Background task to simulate human rejection injection via Redis."""
            for _ in range(40):
                await asyncio.sleep(0.2)
                pending = await self.redis.get_approval_pending(test_run_id)
                if pending and pending.get("status") == "awaiting_approval":
                    nonce = pending.get("nonce")
                    await self.redis.set_approval_decision(test_run_id, "rejected", nonce=nonce)
                    return

        task2 = asyncio.create_task(background_rejection_injector())

        outcome = await agent.handle_approval_gate(
            vendor="Bharat Supplies",
            amount=76500.0,
            invoice_id=str(test_inv_uuid),
            po_number=test_po,
        )
        await task2

        self.assertEqual(outcome, "rejected")

        # Verify Neon invoice status was updated to 'skipped'
        async with self.pool.acquire() as conn:
            updated_status = await conn.fetchval(
                "SELECT status FROM invoices WHERE id = $1;",
                test_inv_uuid,
            )
            self.assertEqual(updated_status, "skipped", "Invoice must be marked as skipped in Neon on rejection")

        # Verify resolution event was emitted
        res_events = [e for e in emitted_events if e["type"] == "approval_resolved"]
        self.assertEqual(len(res_events), 1)
        self.assertEqual(res_events[0]["decision"], "rejected")

        # Verify Redis keys cleared
        self.assertIsNone(await self.redis.get_approval_pending(test_run_id))

    async def test_07_react_loop_intercepts_submit_and_executes_on_approval(self):
        """
        Phase 2.7 integration test: ReAct loop intercepts submit click for an over-threshold invoice,
        triggers approval gate, waits for human approval, and on approval executes form submission.
        """
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.page = None

        # Return page snapshots
        mock_tools.read_page = AsyncMock(return_value={
            "success": True,
            "url": "http://localhost:3051/invoices/new",
            "title": "Create Invoice",
            "snapshot": "- button 'Create Invoice'\n- textbox 'Amount'\n- textbox 'PO Number'",
        })
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
        mock_tools.check_exists = AsyncMock(return_value={"exists": False, "entity_type": "invoice", "identifier": "PO-1006"})

        # Track tool executions
        executed_tools: List[str] = []

        async def mock_execute(tool_name: str, args: Dict[str, Any]):
            """Mock tool execution callback tracking executed tool names."""
            executed_tools.append(tool_name)
            return {"success": True, "action": tool_name}

        mock_tools.execute = AsyncMock(side_effect=mock_execute)

        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        def make_tc(call_id: str, tool_name: str, arguments: Dict[str, Any]):
            """Helper to construct mock Groq tool call objects."""
            fn = MagicMock()
            fn.name = tool_name
            fn.arguments = json.dumps(arguments)
            tc = MagicMock()
            tc.id = call_id
            tc.function = fn
            return tc

        msg1 = MagicMock(
            tool_calls=[make_tc("c1", "fill", {"selector": "Amount", "value": "62000"})],
            content=None,
        )
        msg2 = MagicMock(
            tool_calls=[make_tc("c2", "fill", {"selector": "PO Number", "value": "PO-1006"})],
            content=None,
        )
        msg3 = MagicMock(
            tool_calls=[make_tc("c3", "click", {"selector": "Create Invoice"})],
            content=None,
        )
        msg4 = MagicMock(
            tool_calls=None,
            content="Task completed. Invoice created after approval done.",
        )

        mock_completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg1)]),
            MagicMock(choices=[MagicMock(message=msg2)]),
            MagicMock(choices=[MagicMock(message=msg3)]),
            MagicMock(choices=[MagicMock(message=msg4)]),
        ])
        mock_groq.chat = MagicMock(completions=mock_completions)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=6,
        )

        # Background approver task
        async def approve_when_pending():
            """Background task to approve pending approval request."""
            for _ in range(40):
                await asyncio.sleep(0.2)
                pending = await self.redis.get_approval_pending(test_run_id)
                if pending and pending.get("status") == "awaiting_approval":
                    nonce = pending.get("nonce")
                    await self.redis.set_approval_decision(test_run_id, "approved", nonce=nonce)
                    break

        task3 = asyncio.create_task(approve_when_pending())

        result = await agent.run(goal="Create invoice for PO-1006 with amount ₹62,000")
        await task3

        self.assertEqual(result["status"], "completed")
        # Ensure 'click' was eventually executed after approval was granted
        self.assertIn("click", executed_tools)

    async def test_08_react_loop_intercepts_submit_and_aborts_on_rejection(self):
        """
        Phase 2.7 integration test: ReAct loop intercepts submit click for an over-threshold invoice,
        triggers approval gate, human rejects it -> submit click is ABORTED, invoice marked skipped in Neon,
        and agent receives rejection feedback to move to next invoice.
        """
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)

        test_po = f"PO-ABORT-{uuid.uuid4().hex[:6]}"
        async with self.pool.acquire() as conn:
            inv_row = await conn.fetchrow(
                """
                INSERT INTO invoices (vendor, amount, date, po_number, status)
                VALUES ('Acme Corp', 70000.00, CURRENT_DATE, $1, 'pending')
                RETURNING id;
                """,
                test_po,
            )
            test_inv_uuid = inv_row["id"]
            self.created_invoice_ids.append(test_inv_uuid)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.page = None

        mock_tools.read_page = AsyncMock(return_value={
            "success": True,
            "url": "http://localhost:3051/invoices/new",
            "snapshot": "- button 'Create Invoice'",
        })
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
        mock_tools.check_exists = AsyncMock(return_value={"exists": False, "entity_type": "invoice", "identifier": test_po})

        executed_tools: List[str] = []

        async def mock_execute(tool_name: str, args: Dict[str, Any]):
            """Mock tool execution callback tracking executed tool names."""
            executed_tools.append(tool_name)
            return {"success": True, "action": tool_name}

        mock_tools.execute = AsyncMock(side_effect=mock_execute)

        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        def make_tc(call_id: str, tool_name: str, arguments: Dict[str, Any]):
            """Helper to construct mock Groq tool call objects."""
            fn = MagicMock()
            fn.name = tool_name
            fn.arguments = json.dumps(arguments)
            tc = MagicMock()
            tc.id = call_id
            tc.function = fn
            return tc

        msg1 = MagicMock(
            tool_calls=[make_tc("c1", "fill", {"selector": "Amount", "value": "70000"})],
            content=None,
        )
        msg2 = MagicMock(
            tool_calls=[make_tc("c2", "fill", {"selector": "PO Number", "value": test_po})],
            content=None,
        )
        msg3 = MagicMock(
            tool_calls=[make_tc("c3", "click", {"selector": "Create Invoice"})],
            content=None,
        )
        msg4 = MagicMock(
            tool_calls=None,
            content="Supervisor rejected PO. Moving on. Task is completed and done.",
        )

        mock_completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg1)]),
            MagicMock(choices=[MagicMock(message=msg2)]),
            MagicMock(choices=[MagicMock(message=msg3)]),
            MagicMock(choices=[MagicMock(message=msg4)]),
        ])
        mock_groq.chat = MagicMock(completions=mock_completions)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=6,
        )

        # Background rejector task
        async def reject_when_pending():
            """Background task to reject pending approval request."""
            for _ in range(40):
                await asyncio.sleep(0.2)
                pending = await self.redis.get_approval_pending(test_run_id)
                if pending and pending.get("status") == "awaiting_approval":
                    nonce = pending.get("nonce")
                    await self.redis.set_approval_decision(test_run_id, "rejected", nonce=nonce)
                    break

        task4 = asyncio.create_task(reject_when_pending())

        result = await agent.run(goal=f"Create invoice for {test_po} with amount ₹70,000")
        await task4

        self.assertEqual(result["status"], "completed")
        # Ensure 'click' was NEVER executed because the submission was rejected!
        self.assertNotIn("click", executed_tools)

        # Verify Neon invoice status was marked as 'skipped'
        async with self.pool.acquire() as conn:
            curr_status = await conn.fetchval(
                "SELECT status FROM invoices WHERE id = $1;",
                test_inv_uuid,
            )
            self.assertEqual(curr_status, "skipped")

    async def test_09_fastapi_approval_get_and_post_endpoints(self):
        """
        Verify FastAPI GET and POST endpoints for approval workflow (Phase 2.7 / Phase 3).
        - GET /agent/runs/{run_id}/approval returns pending=false when not pending.
        - GET /agent/runs/{run_id}/approval returns pending=true with approval data when awaiting.
        - POST /agent/runs/{run_id}/approval validates decision ('approved'/'rejected') and nonce.
        """
        test_run_id = f"test-approval-ep-{uuid.uuid4().hex[:6]}"
        self.created_run_ids.append(test_run_id)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            from conftest import issue_test_session
            auth = {"Cookie": issue_test_session()}

            # 1. GET when no approval pending
            res_get_empty = await client.get(f"/agent/runs/{test_run_id}/approval", headers=auth)
            self.assertEqual(res_get_empty.status_code, 200)
            self.assertFalse(res_get_empty.json()["pending"])
            self.assertIsNone(res_get_empty.json()["approval_data"])

            # 2. Set pending approval request in Redis with nonce
            test_nonce = f"nonce-{uuid.uuid4().hex[:8]}"
            approval_payload = {
                "run_id": test_run_id,
                "nonce": test_nonce,
                "status": "awaiting_approval",
                "invoice_id": "inv-endpoint-test",
                "vendor": "Acme Corp",
                "amount": 85000.0,
                "po_number": "PO-1006",
            }
            await self.redis.set_approval_pending(test_run_id, approval_payload)

            # 3. GET when approval is pending
            res_get_pending = await client.get(f"/agent/runs/{test_run_id}/approval", headers=auth)
            self.assertEqual(res_get_pending.status_code, 200)
            data_pending = res_get_pending.json()
            self.assertTrue(data_pending["pending"])
            self.assertEqual(data_pending["approval_data"]["amount"], 85000.0)
            self.assertEqual(data_pending["approval_data"]["vendor"], "Acme Corp")
            self.assertEqual(data_pending["approval_data"]["po_number"], "PO-1006")

            # 4. POST with invalid decision value -> 400
            res_bad_decision = await client.post(
                f"/agent/runs/{test_run_id}/approval",
                json={"decision": "undecided"},
                headers=auth,
            )
            self.assertEqual(res_bad_decision.status_code, 400)

            # 5. POST with invalid nonce -> 400
            res_bad_nonce = await client.post(
                f"/agent/runs/{test_run_id}/approval",
                json={"decision": "approved", "nonce": "wrong-nonce"},
                headers=auth,
            )
            self.assertEqual(res_bad_nonce.status_code, 400)

            # 6. POST with matching nonce and valid decision -> 200 OK
            res_ok = await client.post(
                f"/agent/runs/{test_run_id}/approval",
                json={"decision": "Approved", "nonce": test_nonce},
                headers=auth,
            )
            self.assertEqual(res_ok.status_code, 200)
            self.assertTrue(res_ok.json()["recorded"])
            self.assertEqual(res_ok.json()["decision"], "approved")

            # Verify decision stored in Redis
            stored_decision = await self.redis.get_approval_decision(test_run_id)
            self.assertEqual(stored_decision, "approved")


if __name__ == "__main__":
    unittest.main()
