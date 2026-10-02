import asyncio
import base64
import json
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import asyncpg
from httpx import ASGITransport, AsyncClient
from groq import AsyncGroq

from backend.config import settings
from backend.db import get_db_pool, close_db_pool
from backend.main import app
from backend.agent import ReActAgent
from backend.tools import PlaywrightTools
from backend.redis_client import UpstashRedisClient, get_redis_client


class TestPhase26StepsPersistence(unittest.IsolatedAsyncioTestCase):
    """
    Comprehensive tests for Phase 2.6:
    Persist every step to agent_steps table in Neon (action, result, screenshot_b64, timestamp).
    """

    async def asyncSetUp(self):
        self.pool = await get_db_pool()
        self.redis = get_redis_client()
        self.test_run_ids: List[str] = []

    async def asyncTearDown(self):
        # Cleanup test records
        if self.test_run_ids:
            try:
                valid_uuids = []
                for r in self.test_run_ids:
                    try:
                        valid_uuids.append(uuid.UUID(str(r)))
                    except (ValueError, TypeError):
                        pass
                if valid_uuids:
                    async with self.pool.acquire() as conn:
                        await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                        await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_uuids)
            except Exception:
                pass
        await self.redis.close()
        await close_db_pool()

    # -----------------------------------------------------------------------
    # 1. Step Creation, Verification & Timestamps in Neon
    # -----------------------------------------------------------------------
    async def test_01_persist_step_basic_and_timestamps(self):
        """Verify basic step persistence to Neon with timestamp, action, and result."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test step persistence basic")

        before_ts = datetime.now(timezone.utc)
        step_id = await agent.persist_step(
            action="navigate",
            result="navigated to /invoices (status: 200)",
        )
        after_ts = datetime.now(timezone.utc)

        self.assertIsNotNone(step_id)
        step_uuid = uuid.UUID(step_id)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT step_id, run_id, action, result, screenshot_b64, timestamp FROM agent_steps WHERE step_id = $1;",
                step_uuid,
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["run_id"], uuid.UUID(test_run_id))
            self.assertEqual(row["action"], "navigate")
            self.assertEqual(row["result"], "navigated to /invoices (status: 200)")
            self.assertIsNone(row["screenshot_b64"])
            self.assertIsNotNone(row["timestamp"])
            # Timestamp should fall in valid range
            self.assertGreaterEqual(row["timestamp"], before_ts.replace(microsecond=0))

    async def test_02_persist_step_with_screenshot_b64(self):
        """Verify step persistence with base64 encoded screenshot buffer."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test step persistence with screenshot")

        fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        step_id = await agent.persist_step(
            action="take_screenshot",
            result="screenshot captured (85 bytes)",
            screenshot_b64=fake_b64,
        )

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT step_id, action, result, screenshot_b64 FROM agent_steps WHERE step_id = $1;",
                uuid.UUID(step_id),
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["action"], "take_screenshot")
            self.assertEqual(row["screenshot_b64"], fake_b64)

    async def test_03_update_step_and_attach_screenshot(self):
        """Verify updating an existing step (e.g. attaching screenshot after execution)."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test step update")

        step_id = await agent.persist_step(action="click", result="clicked Submit Button")

        update_ok = await agent.update_step(
            step_id=step_id,
            result="clicked Submit Button (DOM confirmed)",
            screenshot_b64="dXBkYXRlZF9zY3JlZW5zaG90X2Jhc2U2NA==",
        )
        self.assertTrue(update_ok)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT result, screenshot_b64 FROM agent_steps WHERE step_id = $1;",
                uuid.UUID(step_id),
            )
            self.assertEqual(row["result"], "clicked Submit Button (DOM confirmed)")
            self.assertEqual(row["screenshot_b64"], "dXBkYXRlZF9zY3JlZW5zaG90X2Jhc2U2NA==")

    # -----------------------------------------------------------------------
    # 2. Chronological Ordering & Keyset Retrieval
    # -----------------------------------------------------------------------
    async def test_04_get_steps_chronological_ordering(self):
        """Verify querying steps returns full chronological list."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test chronological ordering")

        s1 = await agent.persist_step(action="navigate", result="navigated to /invoices")
        await asyncio.sleep(0.02)
        s2 = await agent.persist_step(action="read_page", result="read_page: 2450 bytes")
        await asyncio.sleep(0.02)
        s3 = await agent.persist_step(
            action="click",
            result="clicked Create Invoice",
            screenshot_b64="c2NyZWVuc2hvdF8x",
        )

        steps = await agent.get_steps(include_screenshots=True)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["step_id"], s1)
        self.assertEqual(steps[1]["step_id"], s2)
        self.assertEqual(steps[2]["step_id"], s3)
        self.assertEqual(steps[0]["action"], "navigate")
        self.assertEqual(steps[1]["action"], "read_page")
        self.assertEqual(steps[2]["action"], "click")
        self.assertTrue(steps[2]["has_screenshot"])
        self.assertEqual(steps[2]["screenshot_b64"], "c2NyZWVuc2hvdF8x")

        # Verify include_screenshots=False excludes b64 string
        steps_compact = await agent.get_steps(include_screenshots=False)
        self.assertIsNone(steps_compact[2]["screenshot_b64"])
        self.assertTrue(steps_compact[2]["has_screenshot"])

    async def test_05_get_step_by_id(self):
        """Verify fetching single step by ID."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test get_step_by_id")

        step_id = await agent.persist_step(action="fill", result="filled Amount = 25000")
        step_data = await agent.get_step_by_id(step_id)

        self.assertIsNotNone(step_data)
        self.assertEqual(step_data["step_id"], step_id)
        self.assertEqual(step_data["action"], "fill")
        self.assertEqual(step_data["result"], "filled Amount = 25000")

        # Nonexistent step
        fake_id = str(uuid.uuid4())
        self.assertIsNone(await agent.get_step_by_id(fake_id))

    # -----------------------------------------------------------------------
    # 3. Foreign Key Cascade & Auto-recovery
    # -----------------------------------------------------------------------
    async def test_06_foreign_key_cascade_deletion(self):
        """Verify ON DELETE CASCADE removes agent_steps when agent_run is deleted."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test cascade deletion")

        await agent.persist_step(action="navigate", result="success")
        await agent.persist_step(action="click", result="success")

        # Verify 2 steps exist
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM agent_steps WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(count, 2)

            # Delete the run
            await conn.execute("DELETE FROM agent_runs WHERE run_id = $1;", uuid.UUID(test_run_id))

            # Verify steps are automatically deleted via CASCADE
            count_after = await conn.fetchval(
                "SELECT count(*) FROM agent_steps WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(count_after, 0)

    async def test_07_persist_step_auto_recovers_if_run_missing(self):
        """Verify persist_step automatically ensures agent_runs record if called before ensure_run_record."""
        unregistered_run_id = str(uuid.uuid4())
        self.test_run_ids.append(unregistered_run_id)

        agent = ReActAgent(run_id=unregistered_run_id, pool=self.pool)
        # Call persist_step directly without calling ensure_run_record first
        step_id = await agent.persist_step(action="auto_init_step", result="created safely")
        self.assertIsNotNone(step_id)

        async with self.pool.acquire() as conn:
            run_exists = await conn.fetchrow(
                "SELECT run_id, status FROM agent_runs WHERE run_id = $1;",
                uuid.UUID(unregistered_run_id),
            )
            self.assertIsNotNone(run_exists)

    # -----------------------------------------------------------------------
    # 4. ReAct Loop Step Persistence (All Tool Actions & Completions)
    # -----------------------------------------------------------------------
    async def test_08_react_loop_persists_every_action_and_done(self):
        """
        Verify that during a multi-step ReAct run, every single tool execution
        and completion is faithfully persisted to Neon agent_steps.
        """
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()

        mock_tools.read_page = AsyncMock(return_value={
            "success": True,
            "url": "http://localhost:3051/invoices/new",
            "title": "New Invoice",
            "snapshot": "- input 'Amount'\n- button 'Create Invoice'",
            "size_bytes": 1200,
        })
        mock_tools.execute = AsyncMock(side_effect=[
            {"success": True, "url": "http://localhost:3051/invoices/new", "status": 200},
            {"success": True, "selector": "Vendor", "selected": "Acme Corp"},
            {"success": True, "selector": "Amount", "value": "35000"},
            {"success": True, "selector": "Create Invoice", "clicked": True},
        ])
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})

        # Mock Groq completions
        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        def make_msg(name, args):
            m = MagicMock()
            tc = MagicMock()
            tc.id = f"call_{name}_{uuid.uuid4().hex[:6]}"
            tc.function.name = name
            tc.function.arguments = json.dumps(args)
            m.tool_calls = [tc]
            m.content = None
            return MagicMock(choices=[MagicMock(message=m)])

        def make_done_msg(text):
            m = MagicMock()
            m.tool_calls = None
            m.content = text
            return MagicMock(choices=[MagicMock(message=m)])

        mock_completions.create = AsyncMock(side_effect=[
            make_msg("navigate", {"url": "/invoices/new"}),
            make_msg("select", {"selector": "Vendor", "value": "Acme Corp"}),
            make_msg("fill", {"selector": "Amount", "value": "35000"}),
            make_msg("click", {"selector": "Create Invoice"}),
            make_done_msg("Done! Invoice for Acme Corp created successfully."),
        ])
        mock_groq.chat = MagicMock(completions=mock_completions)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=10,
        )

        res = await agent.run(goal="Create invoice for Acme Corp amount 35000")
        self.assertEqual(res["status"], "completed")

        # Query all steps persisted in Neon
        steps = await agent.get_steps()
        actions = [s["action"] for s in steps]

        # Verify every action was persisted in order
        self.assertIn("navigate", actions)
        self.assertIn("select", actions)
        self.assertIn("fill", actions)
        self.assertIn("click", actions)
        self.assertIn("done", actions)

        # Check results
        done_step = next(s for s in steps if s["action"] == "done")
        self.assertIn("Acme Corp created successfully", done_step["result"])

    async def test_09_failed_tool_step_persists_failure_and_attaches_screenshot(self):
        """Verify failed tool execution persists error result and attaches failure screenshot."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": "empty", "size_bytes": 10})
        mock_tools.execute = AsyncMock(return_value={"success": False, "error": "Element '#nonexistent' not found"})

        failure_b64 = "ZmFpbHVyZV9zY3JlZW5zaG90X2Jhc2U2NA=="
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": failure_b64})

        mock_groq = MagicMock(spec=AsyncGroq)
        m = MagicMock()
        tc = MagicMock()
        tc.id = "call_fail_1"
        tc.function.name = "click"
        tc.function.arguments = json.dumps({"selector": "#nonexistent"})
        m.tool_calls = [tc]
        m.content = None

        m_done = MagicMock()
        m_done.tool_calls = None
        m_done.content = "Failed and done"

        mock_groq.chat = MagicMock(completions=MagicMock(create=AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=m)]),
            MagicMock(choices=[MagicMock(message=m_done)]),
        ])))

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=5,
        )

        await agent.run(goal="Try to click nonexistent element")

        steps = await agent.get_steps()
        failed_step = next((s for s in steps if s["action"] == "click"), None)
        self.assertIsNotNone(failed_step)
        self.assertIn("failed", failed_step["result"])
        self.assertIn("Element '#nonexistent' not found", failed_step["result"])
        self.assertTrue(failed_step["has_screenshot"])
        self.assertEqual(failed_step["screenshot_b64"], failure_b64)

    # -----------------------------------------------------------------------
    # 5. FastAPI Endpoints for Step Retrieval
    # -----------------------------------------------------------------------
    async def test_10_fastapi_step_endpoints(self):
        """Verify GET /agent/runs/{run_id}/steps and GET /agent/steps/{step_id} endpoints."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("API Endpoint Step Test")

        s1 = await agent.persist_step(action="navigate", result="navigated to /invoices")
        s2 = await agent.persist_step(
            action="take_screenshot",
            result="screenshot taken",
            screenshot_b64="YXBpX3Rlc3Rfc2NyZWVuc2hvdA==",
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Fetch steps list without screenshots
            res = await client.get(f"/agent/runs/{test_run_id}/steps")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["run_id"], test_run_id)
            self.assertEqual(data["count"], 2)
            self.assertIsNone(data["steps"][1]["screenshot_b64"])
            self.assertTrue(data["steps"][1]["has_screenshot"])

            # 2. Fetch steps list with screenshots
            res_full = await client.get(f"/agent/runs/{test_run_id}/steps?include_screenshots=true")
            self.assertEqual(res_full.status_code, 200)
            data_full = res_full.json()
            self.assertEqual(data_full["steps"][1]["screenshot_b64"], "YXBpX3Rlc3Rfc2NyZWVuc2hvdA==")

            # 3. Fetch single step by step_id
            res_single = await client.get(f"/agent/steps/{s2}")
            self.assertEqual(res_single.status_code, 200)
            data_single = res_single.json()
            self.assertEqual(data_single["step_id"], s2)
            self.assertEqual(data_single["action"], "take_screenshot")
            self.assertEqual(data_single["screenshot_b64"], "YXBpX3Rlc3Rfc2NyZWVuc2hvdA==")

            # 4. 404 for non-existent run and step
            res_404_run = await client.get(f"/agent/runs/{uuid.uuid4()}/steps")
            self.assertEqual(res_404_run.status_code, 404)

            res_404_step = await client.get(f"/agent/steps/{uuid.uuid4()}")
            self.assertEqual(res_404_step.status_code, 404)


if __name__ == "__main__":
    unittest.main()
