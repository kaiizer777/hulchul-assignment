import asyncio
import json
import unittest
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, get_db_pool
from backend.tools import PlaywrightTools, TOOL_DEFINITIONS, check_exists
from backend.redis_client import UpstashRedisClient
from backend.agent import (
    ReActAgent,
    extract_target_po,
    parse_tool_call,
)


def create_unit_agent(mock_tools: MagicMock, on_event=None) -> ReActAgent:
    """Helper to instantiate ReActAgent isolated from live DB for pure unit testing."""
    mock_redis = MagicMock(spec=UpstashRedisClient)
    mock_redis.is_configured = True
    mock_redis.get_pause_flag = AsyncMock(return_value=False)
    mock_redis.set_session_state = AsyncMock(return_value=True)
    mock_redis.clear_approval = AsyncMock()

    agent = ReActAgent(
        run_id=str(uuid.uuid4()),
        tools=mock_tools,
        redis_client=mock_redis,
        on_event=on_event,
    )
    agent.ensure_run_record = AsyncMock()
    agent.update_run_status = AsyncMock()
    agent.persist_step = AsyncMock(return_value="mock-step-uuid")
    agent.get_last_successful_step_index = AsyncMock(return_value=0)
    return agent


class TestPhase25Unit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Phase 2.5 Idempotency Guard and PO Extraction."""

    def test_01_extract_target_po(self):
        """Verify PO extraction from goal strings."""
        self.assertEqual(extract_target_po("Process invoice for PO-1001"), "PO-1001")
        self.assertEqual(extract_target_po("Create invoice PO-2005 now"), "PO-2005")
        self.assertEqual(extract_target_po("Check po-9999 in ledger"), "po-9999")
        self.assertIsNone(extract_target_po("Process all invoices without PO"))
        self.assertIsNone(extract_target_po(""))

    async def test_02_check_idempotency_caches_results(self):
        """Verify check_idempotency caches positive results only, ignoring non-existent and error outcomes."""
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.check_exists = AsyncMock(side_effect=[
            # First PO: exists is True -> cached
            {"success": True, "exists": True, "entity_type": "invoice", "identifier": "PO-1001", "record": {"id": "test-uuid"}},
            # Second PO: exists is False -> not cached
            {"success": True, "exists": False, "entity_type": "invoice", "identifier": "PO-NEW", "record": None},
            {"success": True, "exists": False, "entity_type": "invoice", "identifier": "PO-NEW", "record": None},
            # Third PO: error occurred -> not cached
            {"success": False, "exists": False, "error": "DB connection failed", "entity_type": "invoice", "identifier": "PO-ERR"},
            {"success": False, "exists": False, "error": "DB connection failed", "entity_type": "invoice", "identifier": "PO-ERR"},
        ])
        agent = create_unit_agent(mock_tools)

        # 1. Existing entity: first call hits tools, second call served from cache
        res1 = await agent.check_idempotency("invoice", "PO-1001")
        self.assertTrue(res1["exists"])
        res2 = await agent.check_idempotency("invoice", "PO-1001")
        self.assertTrue(res2["exists"])
        self.assertEqual(mock_tools.check_exists.call_count, 1)

        # 2. Non-existent entity: should NOT be cached, so repeated calls hit tools again
        res_new1 = await agent.check_idempotency("invoice", "PO-NEW")
        self.assertFalse(res_new1["exists"])
        res_new2 = await agent.check_idempotency("invoice", "PO-NEW")
        self.assertFalse(res_new2["exists"])
        self.assertEqual(mock_tools.check_exists.call_count, 3)

        # 3. Error result: should NOT be cached
        res_err1 = await agent.check_idempotency("invoice", "PO-ERR")
        self.assertEqual(res_err1.get("error"), "DB connection failed")
        res_err2 = await agent.check_idempotency("invoice", "PO-ERR")
        self.assertEqual(res_err2.get("error"), "DB connection failed")
        self.assertEqual(mock_tools.check_exists.call_count, 5)

    async def test_03_idempotency_aborts_fill_when_invoice_already_exists(self):
        """
        Phase 2.5: When fill is called with a PO number that already exists,
        the agent must abort fill, emit idempotency_aborted, persist step,
        and not execute tools.fill.
        """
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = "test-run"
        mock_tools.set_run_id = MagicMock()
        mock_tools.check_exists = AsyncMock(return_value={
            "success": True,
            "exists": True,
            "entity_type": "invoice",
            "identifier": "PO-1002",
            "record": {"id": "inv-1002", "po_number": "PO-1002", "status": "completed"},
        })
        mock_tools.fill = AsyncMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})

        emitted_events: List[Dict[str, Any]] = []

        async def on_evt(e):
            """Capture emitted agent events for assertion."""
            emitted_events.append(e)

        agent = create_unit_agent(mock_tools, on_event=on_evt)

        # Mock Groq to send fill('PO Number', 'PO-1002'), then declare done
        mock_groq = MagicMock()
        msg_fill = MagicMock()
        tc = MagicMock()
        tc.id = "call_fill_po"
        tc.function.name = "fill"
        tc.function.arguments = json.dumps({"selector": "PO Number", "value": "PO-1002"})
        msg_fill.tool_calls = [tc]
        msg_fill.content = None

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "Task finished and done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_fill)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        agent._groq_client = mock_groq

        # Run agent
        result = await agent.run(goal="Create invoice for PO-1002")
        self.assertEqual(result["status"], "completed")

        # Verify tools.fill was NEVER called because of the idempotency abort
        mock_tools.fill.assert_not_called()

        # Verify idempotency_aborted event was emitted
        aborted_events = [e for e in emitted_events if e.get("type") == "idempotency_aborted"]
        self.assertEqual(len(aborted_events), 1)
        self.assertEqual(aborted_events[0]["identifier"], "PO-1002")
        self.assertEqual(aborted_events[0]["action"], "fill")

        # Verify persist_step was called with idempotency_check and aborted_duplicate
        agent.persist_step.assert_any_call(
            action="idempotency_check",
            result="aborted_duplicate: invoice PO-1002 already exists",
        )

    async def test_03b_idempotency_aborts_fill_on_check_error(self):
        """
        Phase 2.5: When check_exists returns an error during fill guard,
        the agent must abort fill, emit idempotency_aborted with error,
        and not execute tools.fill.
        """
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = "test-run"
        mock_tools.set_run_id = MagicMock()
        mock_tools.check_exists = AsyncMock(return_value={
            "success": False,
            "exists": False,
            "error": "Neon DB connection timed out",
            "entity_type": "invoice",
            "identifier": "PO-ERR-FILL",
        })
        mock_tools.fill = AsyncMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})

        emitted_events: List[Dict[str, Any]] = []

        async def on_evt(e):
            """Capture emitted agent events for assertion."""
            emitted_events.append(e)

        agent = create_unit_agent(mock_tools, on_event=on_evt)

        mock_groq = MagicMock()
        msg_fill = MagicMock()
        tc = MagicMock()
        tc.id = "call_fill_err"
        tc.function.name = "fill"
        tc.function.arguments = json.dumps({"selector": "PO Number", "value": "PO-ERR-FILL"})
        msg_fill.tool_calls = [tc]
        msg_fill.content = None

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "Aborted due to DB error. Done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_fill)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        agent._groq_client = mock_groq

        result = await agent.run(goal="Create invoice for PO-ERR-FILL")
        self.assertEqual(result["status"], "completed")

        # Verify tools.fill was NEVER called
        mock_tools.fill.assert_not_called()

        # Verify idempotency_aborted event was emitted with error
        aborted_events = [e for e in emitted_events if e.get("type") == "idempotency_aborted"]
        self.assertEqual(len(aborted_events), 1)
        self.assertEqual(aborted_events[0]["identifier"], "PO-ERR-FILL")
        self.assertEqual(aborted_events[0]["action"], "fill")
        self.assertEqual(aborted_events[0]["error"], "Neon DB connection timed out")

        # Verify persist_step was called with aborted_error
        agent.persist_step.assert_any_call(
            action="idempotency_check",
            result="aborted_error: Neon DB connection timed out",
        )

    async def test_04_idempotency_allows_fill_when_invoice_is_new(self):
        """
        Phase 2.5: When fill is called with a brand new PO number,
        check_exists confirms it does not exist, and tools.execute proceeds.
        """
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = "test-run"
        mock_tools.set_run_id = MagicMock()
        mock_tools.check_exists = AsyncMock(return_value={
            "success": True,
            "exists": False,
            "entity_type": "invoice",
            "identifier": "PO-BRAND-NEW",
            "record": None,
        })
        mock_tools.execute = AsyncMock(return_value={"success": True, "value": "PO-BRAND-NEW"})
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})

        agent = create_unit_agent(mock_tools)

        mock_groq = MagicMock()
        msg_fill = MagicMock()
        tc = MagicMock()
        tc.id = "call_fill_new"
        tc.function.name = "fill"
        tc.function.arguments = json.dumps({"selector": "PO Number", "value": "PO-BRAND-NEW"})
        msg_fill.tool_calls = [tc]
        msg_fill.content = None

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "All tasks done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_fill)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        agent._groq_client = mock_groq

        await agent.run(goal="Create invoice for PO-BRAND-NEW")

        # Verify execute was called with fill and PO-BRAND-NEW. The dispatch also
        # carries the step id the loop minted for this step, so a tool that
        # persists its own row stores it under that id instead of a second one.
        mock_tools.execute.assert_called_once_with(
            "fill",
            {"selector": "PO Number", "value": "PO-BRAND-NEW"},
            step_id=ANY,
        )
        self.assertEqual(agent._active_form_state.get("po_number"), "PO-BRAND-NEW")

    async def test_05_idempotency_aborts_submit_when_invoice_already_exists(self):
        """
        Phase 2.5: When click submit ('Create Invoice') is called and the active invoice
        already exists in DB, submission is intercepted and aborted without executing click.
        """
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = "test-run"
        mock_tools.set_run_id = MagicMock()
        mock_tools.check_exists = AsyncMock(return_value={
            "success": True,
            "exists": True,
            "entity_type": "invoice",
            "identifier": "PO-EXISTING-99",
            "record": {"po_number": "PO-EXISTING-99"},
        })
        mock_tools.execute = AsyncMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})

        emitted_events: List[Dict[str, Any]] = []

        async def on_evt(e):
            """Capture emitted agent events for assertion."""
            emitted_events.append(e)

        agent = create_unit_agent(mock_tools, on_event=on_evt)
        # Pre-populate active form state with existing PO and valid amount
        agent._active_form_state = {"po_number": "PO-EXISTING-99", "amount": "15000", "vendor": "Acme"}

        mock_groq = MagicMock()
        msg_click = MagicMock()
        tc = MagicMock()
        tc.id = "call_click_submit"
        tc.function.name = "click"
        tc.function.arguments = json.dumps({"selector": "Create Invoice"})
        msg_click.tool_calls = [tc]
        msg_click.content = None

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "Operation completed and done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_click)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        agent._groq_client = mock_groq

        await agent.run(goal="Submit invoice form")

        # Verify tools.execute was NOT called for click
        mock_tools.execute.assert_not_called()

        # Verify idempotency_aborted event emitted for submit
        aborted = [e for e in emitted_events if e.get("type") == "idempotency_aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0]["action"], "click_submit")
        self.assertEqual(aborted[0]["identifier"], "PO-EXISTING-99")

        # Form state was cleared
        self.assertEqual(agent._active_form_state, {})

    async def test_05b_idempotency_aborts_submit_on_check_error(self):
        """
        Phase 2.5: When check_exists fails with an error during submit guard,
        submission must abort and not proceed with invoice creation.
        """
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = "test-run"
        mock_tools.set_run_id = MagicMock()
        mock_tools.check_exists = AsyncMock(return_value={
            "success": False,
            "exists": False,
            "error": "Database connectivity loss",
            "entity_type": "invoice",
            "identifier": "PO-ERR-SUBMIT",
        })
        mock_tools.execute = AsyncMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})

        emitted_events: List[Dict[str, Any]] = []

        async def on_evt(e):
            """Capture emitted agent events for assertion."""
            emitted_events.append(e)

        agent = create_unit_agent(mock_tools, on_event=on_evt)
        agent._active_form_state = {"po_number": "PO-ERR-SUBMIT", "amount": "20000"}

        mock_groq = MagicMock()
        msg_click = MagicMock()
        tc = MagicMock()
        tc.id = "call_click_err"
        tc.function.name = "click"
        tc.function.arguments = json.dumps({"selector": "Create Invoice"})
        msg_click.tool_calls = [tc]
        msg_click.content = None

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "Aborted due to DB error. Done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_click)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        agent._groq_client = mock_groq

        await agent.run(goal="Submit invoice form")

        # Verify tools.execute was NOT called
        mock_tools.execute.assert_not_called()

        # Verify idempotency_aborted event emitted with error
        aborted = [e for e in emitted_events if e.get("type") == "idempotency_aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0]["action"], "click_submit")
        self.assertEqual(aborted[0]["error"], "Database connectivity loss")
        self.assertEqual(agent._active_form_state, {})

    async def test_06_idempotency_caches_explicit_check_exists_tool_call(self):
        """
        Phase 2.5: When the LLM executes check_exists as a tool, the result is cached.
        Subsequent check_idempotency retrieves the cached outcome immediately.
        """
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = "test-run"
        mock_tools.set_run_id = MagicMock()
        mock_tools.execute = AsyncMock(return_value={
            "success": True,
            "exists": True,
            "entity_type": "invoice",
            "identifier": "PO-PRECHECKED",
            "record": {"id": "uuid-prechecked", "po_number": "PO-PRECHECKED"},
        })
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": ""})

        agent = create_unit_agent(mock_tools)

        mock_groq = MagicMock()
        msg_chk = MagicMock()
        tc = MagicMock()
        tc.id = "call_chk"
        tc.function.name = "check_exists"
        tc.function.arguments = json.dumps({"entity_type": "invoice", "identifier": "PO-PRECHECKED"})
        msg_chk.tool_calls = [tc]
        msg_chk.content = None

        msg_done = MagicMock()
        msg_done.tool_calls = None
        msg_done.content = "Precheck done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg_chk)]),
            MagicMock(choices=[MagicMock(message=msg_done)]),
        ])
        agent._groq_client = mock_groq

        await agent.run(goal="Check if PO-PRECHECKED exists")

        # Verify cached in _checked_entities
        self.assertIn("invoice:po-prechecked", agent._checked_entities)
        cached = agent._checked_entities["invoice:po-prechecked"]
        self.assertTrue(cached["exists"])


@unittest.skipUnless(
    bool(settings.DATABASE_URL and settings.UPSTASH_REDIS_REST_URL),
    "Live Neon DB or Upstash Redis not configured",
)
class TestPhase25Integration(unittest.IsolatedAsyncioTestCase):
    """Integration test suite verifying idempotency against live Neon database."""

    async def asyncSetUp(self):
        """Initialize live Neon DB pool and Redis client for test run."""
        await init_db_pool()
        self.pool = await get_db_pool()
        self.redis = UpstashRedisClient()
        self.created_run_ids: List[str] = []

    async def asyncTearDown(self):
        """Clean up all created test run records, Redis client, and DB pool."""
        if hasattr(self, "created_run_ids") and self.created_run_ids and self.pool:
            try:
                valid_uuids = [uuid.UUID(str(r)) for r in self.created_run_ids]
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                    await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_uuids)
            except Exception:
                pass
        await self.redis.close()
        await close_db_pool()

    async def test_07_live_db_idempotency_seeded_invoice(self):
        """Verify check_idempotency correctly identifies seeded invoice PO-1002 in Neon DB."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        tools = PlaywrightTools(run_id=test_run_id, pool=self.pool)
        agent = ReActAgent(run_id=test_run_id, tools=tools, pool=self.pool, redis_client=self.redis)

        res = await agent.check_idempotency("invoice", "PO-1002")
        self.assertTrue(res["exists"], "Seeded invoice PO-1002 must exist in Neon DB")
        self.assertEqual(res["entity_type"], "invoice")
        self.assertIsNotNone(res["record"])
        self.assertEqual(res["record"]["po_number"], "PO-1002")

    async def test_08_live_db_idempotency_nonexistent_invoice(self):
        """Verify check_idempotency returns exists=False for a non-existent PO in Neon DB."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        tools = PlaywrightTools(run_id=test_run_id, pool=self.pool)
        agent = ReActAgent(run_id=test_run_id, tools=tools, pool=self.pool, redis_client=self.redis)

        res = await agent.check_idempotency("invoice", f"PO-NOT-EXISTS-{uuid.uuid4().hex[:6]}")
        self.assertFalse(res["exists"])
        self.assertIsNone(res["record"])

    async def test_09_react_loop_idempotency_guard_end_to_end(self):
        """
        End-to-end integration test: model attempts to fill an existing invoice (PO-1002).
        Agent idempotency guard intercepts before fill, logs step to Neon DB,
        and model declares done without creating a duplicate.
        """
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": "invoice form"})
        mock_tools.execute = AsyncMock(return_value={"success": True})
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True})

        # Delegate check_exists to actual check_exists against Neon DB
        async def real_check_exists(entity_type, identifier):
            """Delegate existence check directly to live Neon DB pool."""
            return await check_exists(entity_type, identifier, pool=self.pool)

        mock_tools.check_exists = AsyncMock(side_effect=real_check_exists)

        # Mock Groq to attempt filling existing PO-1002, then finish
        mock_groq = MagicMock()
        msg1 = MagicMock()
        tc1 = MagicMock()
        tc1.id = "call_fill_dup"
        tc1.function.name = "fill"
        tc1.function.arguments = json.dumps({"selector": "PO Number", "value": "PO-1002"})
        msg1.tool_calls = [tc1]
        msg1.content = None

        msg2 = MagicMock()
        msg2.tool_calls = None
        msg2.content = "Detected duplicate invoice PO-1002 already exists. Task done."

        mock_groq.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=msg1)]),
            MagicMock(choices=[MagicMock(message=msg2)]),
        ])

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
        )

        res = await agent.run(goal="Create invoice for PO-1002")
        self.assertEqual(res["status"], "completed")

        # Verify idempotency check step was persisted to Neon DB
        async with self.pool.acquire() as conn:
            steps = await conn.fetch(
                "SELECT action, result FROM agent_steps WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            actions = [s["action"] for s in steps]
            self.assertIn("idempotency_check", actions)
            abort_step = next(s for s in steps if s["action"] == "idempotency_check")
            self.assertIn("aborted_duplicate", abort_step["result"])


if __name__ == "__main__":
    unittest.main()
