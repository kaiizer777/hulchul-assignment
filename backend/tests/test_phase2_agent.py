import asyncio
import json
import unittest
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport
from groq import AsyncGroq

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, get_db_pool
from backend.tools import PlaywrightTools, TOOL_DEFINITIONS
from backend.redis_client import UpstashRedisClient, get_redis_client
from backend.agent import (
    extract_approval_threshold,
    extract_vendor_filter,
    build_system_prompt,
    parse_amount,
    parse_tool_call,
    is_goal_done,
    ReActAgent,
    ParsedToolCall,
)
from backend.main import app


class TestPhase24Unit(unittest.TestCase):
    """Pure unit tests for goal extraction, system prompt construction, and tool call parsing."""

    def test_01_extract_approval_threshold(self):
        """Verify dynamic extraction of monetary threshold from goals."""
        self.assertEqual(
            extract_approval_threshold("Hold anything over ₹25,000 for approval"),
            25000.0,
        )
        self.assertEqual(
            extract_approval_threshold("Flag invoices exceeding Rs 75,000"),
            75000.0,
        )
        self.assertEqual(
            extract_approval_threshold("Invoices above ₹ 100,000.50 need approval"),
            100000.5,
        )
        self.assertEqual(
            extract_approval_threshold("Process all invoices with standard ₹50,000 rule"),
            50000.0,
        )
        self.assertEqual(
            extract_approval_threshold("Process invoices without any amount mentioned"),
            50000.0,
        )
        self.assertEqual(
            extract_approval_threshold("Hold anything over $40,000"),
            40000.0,
        )

    def test_02_extract_vendor_filter(self):
        """Verify extraction of vendor filter from goal (Phase 5 variation 1)."""
        self.assertEqual(
            extract_vendor_filter("Process only invoices from Vendor Acme"),
            "Acme",
        )
        self.assertEqual(
            extract_vendor_filter("Process invoices from Vendor Globex Corporation only"),
            "Globex Corporation",
        )
        self.assertIsNone(extract_vendor_filter("Process all invoices across all vendors"))

    def test_03_build_system_prompt(self):
        """Verify system prompt includes role, all tools, threshold, and idempotency rules."""
        prompt = build_system_prompt(
            goal="Process invoices over ₹30,000",
            threshold=30000.0,
            vendor_filter="Acme",
        )
        self.assertIn("autonomous ERP Invoice Processing Agent", prompt)
        self.assertIn("₹30,000.00", prompt)
        self.assertIn("MANDATORY IDEMPOTENCY", prompt)
        self.assertIn("check_exists", prompt)
        self.assertIn("Acme", prompt)

        # Check all 7 Playwright tools are documented
        for tool_name in ["navigate", "read_page", "click", "fill", "select", "take_screenshot", "check_exists"]:
            self.assertIn(tool_name, prompt)

    def test_04_parse_tool_call_and_is_done(self):
        """Verify tool call extraction and done status identification."""
        # 1. Valid tool call with JSON arguments
        mock_msg = MagicMock()
        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_abc123"
        mock_tool_call.function.name = "navigate"
        mock_tool_call.function.arguments = json.dumps({"url": "/invoices"})
        mock_msg.tool_calls = [mock_tool_call]

        parsed = parse_tool_call(mock_msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.id, "call_abc123")
        self.assertEqual(parsed.name, "navigate")
        self.assertEqual(parsed.arguments, {"url": "/invoices"})

        # 2. Valid tool call with dict arguments
        mock_tool_call.function.arguments = {"url": "/purchase-orders"}
        parsed_dict = parse_tool_call(mock_msg)
        self.assertEqual(parsed_dict.arguments, {"url": "/purchase-orders"})

        # 3. No tool calls
        mock_empty_msg = MagicMock()
        mock_empty_msg.tool_calls = None
        self.assertIsNone(parse_tool_call(mock_empty_msg))

        # 4. is_goal_done
        msg_done = MagicMock()
        msg_done.content = "All tasks are done. 4 invoices created."
        self.assertTrue(is_goal_done(msg_done))

        msg_completed = MagicMock()
        msg_completed.content = "The assignment is completed successfully."
        self.assertTrue(is_goal_done(msg_completed))

        msg_not_done = MagicMock()
        msg_not_done.content = "Now clicking on the Create Invoice button..."
        self.assertFalse(is_goal_done(msg_not_done))

    def test_05_parse_amount(self):
        """Verify parse_amount handles raw numbers, currency prefixes, and commas properly."""
        self.assertEqual(parse_amount(50000), 50000.0)
        self.assertEqual(parse_amount(50000.75), 50000.75)
        self.assertEqual(parse_amount("50000"), 50000.0)
        self.assertEqual(parse_amount("₹25,000.50"), 25000.5)
        self.assertEqual(parse_amount("Rs. 65,000"), 65000.0)
        self.assertEqual(parse_amount("INR 100,000"), 100000.0)
        self.assertEqual(parse_amount("$45,500.25"), 45500.25)
        self.assertEqual(parse_amount(None), 0.0)
        self.assertEqual(parse_amount(""), 0.0)
        self.assertEqual(parse_amount("invalid"), 0.0)


@unittest.skipUnless(
    bool(settings.DATABASE_URL and settings.UPSTASH_REDIS_REST_URL),
    "Live Neon DB or Upstash Redis not configured",
)
class TestPhase24Agent(unittest.IsolatedAsyncioTestCase):
    """Integration test suite against live Neon Postgres and Upstash Redis."""

    async def asyncSetUp(self):
        await init_db_pool()
        self.pool = await get_db_pool()
        self.redis = UpstashRedisClient()
        self.created_run_ids: List[str] = []

    async def asyncTearDown(self):
        # Clean up all created test runs and steps from Neon database
        if hasattr(self, "created_run_ids") and self.created_run_ids and self.pool:
            try:
                valid_uuids = []
                for r in self.created_run_ids:
                    try:
                        valid_uuids.append(uuid.UUID(str(r)))
                    except (ValueError, TypeError):
                        pass
                if valid_uuids:
                    async with self.pool.acquire() as conn:
                        await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                        await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_uuids)
            except Exception as e:
                pass

        await self.redis.close()
        await close_db_pool()

    # -----------------------------------------------------------------------
    # 3. Redis Session State & Pause/Resume Tests
    # -----------------------------------------------------------------------
    async def test_05_redis_session_state_and_pause_resume(self):
        """Verify Upstash Redis session state persistence and pause/resume flags."""
        test_run_id = f"test-run-{uuid.uuid4().hex[:8]}"

        # 1. Ping
        ping_ok = await self.redis.ping()
        self.assertTrue(ping_ok, "Redis PING should return True")

        # 2. Session state
        state_data = {
            "run_id": test_run_id,
            "status": "running",
            "current_step": 3,
            "goal": "Test goal",
        }
        set_ok = await self.redis.set_session_state(test_run_id, state_data)
        self.assertTrue(set_ok)

        loaded_state = await self.redis.get_session_state(test_run_id)
        self.assertIsNotNone(loaded_state)
        self.assertEqual(loaded_state["current_step"], 3)
        self.assertEqual(loaded_state["goal"], "Test goal")

        # 3. Pause / Resume flags
        self.assertFalse(await self.redis.get_pause_flag(test_run_id))
        await self.redis.set_pause_flag(test_run_id, paused=True)
        self.assertTrue(await self.redis.get_pause_flag(test_run_id))
        await self.redis.set_pause_flag(test_run_id, paused=False)
        self.assertFalse(await self.redis.get_pause_flag(test_run_id))

        # 4. Approval request and decision
        approval_req = {
            "invoice_id": "inv-001",
            "vendor": "Acme Corp",
            "amount": 65000.0,
            "po_number": "PO-1001",
        }
        await self.redis.set_approval_pending(test_run_id, approval_req)
        pending = await self.redis.get_approval_pending(test_run_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["amount"], 65000.0)
        self.assertEqual(pending["status"], "awaiting_approval")

        # Record human approval decision
        await self.redis.set_approval_decision(test_run_id, "approved")
        decision = await self.redis.get_approval_decision(test_run_id)
        self.assertEqual(decision, "approved")

        # Clear approval
        await self.redis.clear_approval(test_run_id)
        self.assertIsNone(await self.redis.get_approval_pending(test_run_id))
        self.assertIsNone(await self.redis.get_approval_decision(test_run_id))

    # -----------------------------------------------------------------------
    # 4. Neon Database Step Persistence & Foreign Keys
    # -----------------------------------------------------------------------
    async def test_06_neon_agent_runs_and_steps_persistence(self):
        """Verify agent_runs and agent_steps persistence to Neon with foreign keys."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        mock_tools = PlaywrightTools(run_id=test_run_id, pool=self.pool)
        agent = ReActAgent(run_id=test_run_id, tools=mock_tools, pool=self.pool)

        # 1. Ensure run record exists
        await agent.ensure_run_record(goal="Verify step persistence")
        async with self.pool.acquire() as conn:
            run_row = await conn.fetchrow(
                "SELECT run_id, goal, status FROM agent_runs WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertIsNotNone(run_row)
            self.assertEqual(run_row["status"], "running")

        # 2. Persist steps
        step1_id = await agent.persist_step(action="navigate", result="success: /invoices")
        self.assertIsNotNone(step1_id)

        step2_id = await agent.persist_step(
            action="fill",
            result="success: Amount=42000",
            screenshot_b64="dGVzdF9zY3JlZW5zaG90X2Jhc2U2NA==",
        )
        self.assertIsNotNone(step2_id)

        # 3. Query steps back
        async with self.pool.acquire() as conn:
            steps = await conn.fetch(
                "SELECT step_id, action, result, screenshot_b64 FROM agent_steps WHERE run_id = $1 ORDER BY timestamp ASC;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(len(steps), 2)
            self.assertEqual(steps[0]["action"], "navigate")
            self.assertEqual(steps[1]["action"], "fill")
            self.assertEqual(steps[1]["screenshot_b64"], "dGVzdF9zY3JlZW5zaG90X2Jhc2U2NA==")

        # 4. Verify recovery helper
        count = await agent.get_last_successful_step_index()
        self.assertEqual(count, 2)

    # -----------------------------------------------------------------------
    # 5. ReAct Loop Execution with Mocked Groq Responses
    # -----------------------------------------------------------------------
    async def test_07_react_loop_end_to_end_mock(self):
        """
        Integration test: agent completes a 3-step mock task end to end:
        Iteration 1: navigate('/invoices')
        Iteration 2: check_exists('invoice', 'PO-1001')
        Iteration 3: model says 'done' -> agent exits with completed status
        """
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()

        # Mock tools responses
        mock_tools.read_page = AsyncMock(return_value={
            "success": True,
            "url": "http://localhost:3000/invoices",
            "title": "Invoices - Mock ERP",
            "snapshot": "- button 'Create Invoice'\n- table 'Invoices'",
        })
        mock_tools.execute = AsyncMock(side_effect=[
            {"success": True, "url": "http://localhost:3000/invoices"},
            {"success": True, "exists": True, "entity_type": "invoice", "identifier": "PO-1001"},
        ])
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})

        # Mock Groq AsyncGroq client
        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        # Message 1: navigate tool call
        msg1 = MagicMock()
        tc1 = MagicMock()
        tc1.id = "call_nav_1"
        tc1.function.name = "navigate"
        tc1.function.arguments = json.dumps({"url": "/invoices"})
        msg1.tool_calls = [tc1]
        msg1.content = None

        # Message 2: check_exists tool call
        msg2 = MagicMock()
        tc2 = MagicMock()
        tc2.id = "call_chk_2"
        tc2.function.name = "check_exists"
        tc2.function.arguments = json.dumps({"entity_type": "invoice", "identifier": "PO-1001"})
        msg2.tool_calls = [tc2]
        msg2.content = None

        # Message 3: completion message with 'done'
        msg3 = MagicMock()
        msg3.tool_calls = None
        msg3.content = "All verification tasks are done. Invoice PO-1001 verified."

        choice1 = MagicMock(message=msg1)
        resp1 = MagicMock(choices=[choice1])
        choice2 = MagicMock(message=msg2)
        resp2 = MagicMock(choices=[choice2])
        choice3 = MagicMock(message=msg3)
        resp3 = MagicMock(choices=[choice3])

        mock_completions.create = AsyncMock(side_effect=[resp1, resp2, resp3])
        mock_groq.chat = MagicMock(completions=mock_completions)

        # Track emitted SSE events
        emitted_events: List[Dict[str, Any]] = []

        async def record_event(evt: Dict[str, Any]):
            emitted_events.append(evt)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=10,
            on_event=record_event,
        )

        result = await agent.run(goal="Navigate to invoices and verify PO-1001")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_id"], test_run_id)
        self.assertIn("done", result["summary"].lower())
        self.assertEqual(result["iterations"], 3)

        # Verify events emitted
        event_types = [e["type"] for e in emitted_events]
        self.assertIn("run_started", event_types)
        self.assertIn("step", event_types)
        self.assertIn("done", event_types)

        # Verify Neon run record updated to completed
        async with self.pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM agent_runs WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(status, "completed")

    # -----------------------------------------------------------------------
    # 6. Hard Cap at 30 Iterations (Stalled Run)
    # -----------------------------------------------------------------------
    async def test_08_hard_cap_iteration_limit(self):
        """Verify that reaching max iterations marks the run as stalled."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": "loop"})
        mock_tools.execute = AsyncMock(return_value={"success": True})
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True})

        # Mock Groq to continuously return a navigate tool call
        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        msg = MagicMock()
        tc = MagicMock()
        tc.id = "call_loop"
        tc.function.name = "navigate"
        tc.function.arguments = json.dumps({"url": "/invoices"})
        msg.tool_calls = [tc]
        msg.content = None

        mock_completions.create = AsyncMock(return_value=MagicMock(choices=[MagicMock(message=msg)]))
        mock_groq.chat = MagicMock(completions=mock_completions)

        # Set max_iterations to 3 for fast test execution
        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=3,
        )

        result = await agent.run(goal="Infinite loop goal")
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(result["iterations"], 3)

        async with self.pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM agent_runs WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(status, "stalled")

    # -----------------------------------------------------------------------
    # 7. Recovery on Tool Failure (Phase 2.8)
    # -----------------------------------------------------------------------
    async def test_09_recovery_on_tool_failure(self):
        """Verify tool execution failure logs step as failed and captures screenshot."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})
        # Simulate tool failure
        mock_tools.execute = AsyncMock(return_value={"success": False, "error": "Element not found: Submit"})
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": "fail_snap"})

        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        msg_fail = MagicMock()
        tc = MagicMock()
        tc.id = "call_fail"
        tc.function.name = "click"
        tc.function.arguments = json.dumps({"selector": "Submit"})
        msg_fail.tool_calls = [tc]

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "Finished with recovery done"

        mock_completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_fail)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        mock_groq.chat = MagicMock(completions=mock_completions)

        emitted: List[str] = []

        async def on_evt(e):
            emitted.append(e["type"])

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=5,
            on_event=on_evt,
        )

        result = await agent.run(goal="Test failure recovery")
        self.assertEqual(result["status"], "completed")
        self.assertIn("step_failed", emitted)

        # Check that the failed step was logged to Neon
        async with self.pool.acquire() as conn:
            failed_row = await conn.fetchrow(
                "SELECT result, screenshot_b64 FROM agent_steps WHERE run_id = $1 AND result LIKE '%failed%';",
                uuid.UUID(test_run_id),
            )
            self.assertIsNotNone(failed_row)
            self.assertIn("Element not found", failed_row["result"])
            self.assertEqual(failed_row["screenshot_b64"], "fail_snap")

    # -----------------------------------------------------------------------
    # 8. Approval Gate Polling & Resolution (Phase 2.7)
    # -----------------------------------------------------------------------
    async def test_10_approval_gate_resolution(self):
        """Verify that handle_approval_gate polls Redis and returns decision."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            redis_client=self.redis,
        )
        await agent.ensure_run_record(goal="Approval gate test")

        # Simulate async background approval decision injection after 0.5s
        async def background_approver():
            await asyncio.sleep(0.5)
            await self.redis.set_approval_decision(test_run_id, "approved")

        asyncio.create_task(background_approver())

        approved = await agent.handle_approval_gate(
            vendor="High Value Vendor",
            amount=75000.0,
            invoice_id="inv-high-val",
            po_number="PO-2001",
        )
        self.assertTrue(approved, "Should return True when decision is approved")

    # -----------------------------------------------------------------------
    # 9. FastAPI Phase 2.4 Control & Status Endpoints
    # -----------------------------------------------------------------------
    async def test_11_fastapi_control_endpoints(self):
        """Verify /health/redis, /agent/runs/{run_id}/pause, resume, UUID validation, and approval endpoints."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Redis health
            res_redis = await client.get("/health/redis")
            self.assertEqual(res_redis.status_code, 200)
            data_redis = res_redis.json()
            self.assertTrue(data_redis["connected"])
            self.assertEqual(data_redis["status"], "healthy")

            # 2. Path parameter validation: malformed UUID must return 422
            res_bad_uuid = await client.get("/agent/runs/not-a-valid-uuid")
            self.assertEqual(res_bad_uuid.status_code, 422)

            # 3. Pause endpoint
            test_run_id = f"test-pause-{uuid.uuid4().hex[:6]}"
            res_pause = await client.post(f"/agent/runs/{test_run_id}/pause")
            self.assertEqual(res_pause.status_code, 200)
            self.assertTrue(res_pause.json()["paused"])

            # 4. Resume endpoint
            res_resume = await client.post(f"/agent/runs/{test_run_id}/resume")
            self.assertEqual(res_resume.status_code, 200)
            self.assertFalse(res_resume.json()["paused"])

            # 5. Approval submission endpoint without active pending request -> 400
            res_no_pending = await client.post(
                f"/agent/runs/{test_run_id}/approval",
                json={"decision": "approved"},
            )
            self.assertEqual(res_no_pending.status_code, 400)

            # 6. Set pending approval request with nonce
            test_nonce = f"nonce-{uuid.uuid4().hex[:8]}"
            await self.redis.set_approval_pending(
                test_run_id,
                {"status": "awaiting_approval", "nonce": test_nonce},
            )

            # 7. Submitting with wrong nonce -> 400
            res_wrong_nonce = await client.post(
                f"/agent/runs/{test_run_id}/approval",
                json={"decision": "approved", "nonce": "invalid-nonce-val"},
            )
            self.assertEqual(res_wrong_nonce.status_code, 400)

            # 8. Submitting with matching nonce -> 200 OK
            res_app = await client.post(
                f"/agent/runs/{test_run_id}/approval",
                json={"decision": "approved", "nonce": test_nonce},
            )
            self.assertEqual(res_app.status_code, 200)
            self.assertTrue(res_app.json()["recorded"])

            # Clean up Redis keys
            await self.redis.clear_approval(test_run_id)


if __name__ == "__main__":
    unittest.main()
