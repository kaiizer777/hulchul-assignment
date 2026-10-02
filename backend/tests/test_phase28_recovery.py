import asyncio
import unittest
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, get_db_pool
from backend.agent import ReActAgent
from backend.tools import PlaywrightTools
from backend.redis_client import UpstashRedisClient


class TestPhase28RecoveryUnit(unittest.TestCase):
    """Unit tests for Phase 2.8 recovery logic and error handling."""

    def test_01_get_last_successful_step_index_mock(self):
        """Verify calculation of last successful step index using fetchval."""
        agent = ReActAgent(run_id=str(uuid.uuid4()), tools=MagicMock(spec=PlaywrightTools))
        agent.get_db = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = 2
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        agent.get_db.return_value = mock_pool

        count = asyncio.run(agent.get_last_successful_step_index())
        self.assertEqual(count, 2)


@unittest.skipUnless(
    bool(settings.DATABASE_URL and settings.UPSTASH_REDIS_REST_URL),
    "Live Neon DB or Upstash Redis not configured",
)
class TestPhase28RecoveryIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test suite for Phase 2.8 & 2.9 Recovery, Error Handling, and Redis Session State."""

    async def asyncSetUp(self):
        await init_db_pool()
        self.pool = await get_db_pool()
        self.redis = UpstashRedisClient()
        self.test_run_ids: List[str] = []

    async def asyncTearDown(self):
        if self.test_run_ids:
            try:
                valid_uuids = [uuid.UUID(str(r)) for r in self.test_run_ids]
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                    await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_uuids)
            except Exception:
                pass
        for r_id in self.test_run_ids:
            try:
                await self.redis.execute_command("DEL", f"hulchul:session:{r_id}")
                await self.redis.execute_command("DEL", f"hulchul:run:{r_id}")
            except Exception:
                pass
        await self.redis.close()
        await close_db_pool()

    async def test_02_error_handling_and_step_failure_logging(self):
        """Test tool failure logging to Neon with result 'failed', screenshot, and Redis session state at hulchul:session:{run_id}."""
        run_id = str(uuid.uuid4())
        self.test_run_ids.append(run_id)

        events = []
        async def mock_on_event(ev):
            events.append(ev)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.execute = AsyncMock(side_effect=Exception("Simulated ERP 500 Error"))
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": "ZmFrZV9zY3JlZW5zaG90"})

        agent = ReActAgent(
            run_id=run_id,
            tools=mock_tools,
            pool=self.pool,
            on_event=mock_on_event,
            max_iterations=1,
        )

        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": "- button \"Submit\""})
        
        mock_groq = MagicMock()
        mock_completion = MagicMock()
        mock_message = MagicMock()
        mock_message.content = None
        mock_call = MagicMock()
        mock_call.id = "call_123"
        mock_call.function.name = "fill"
        mock_call.function.arguments = '{"selector": "Amount", "value": "50000"}'
        mock_message.tool_calls = [mock_call]
        mock_completion.choices = [MagicMock(message=mock_message)]
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_completion)
        agent.get_groq_client = AsyncMock(return_value=mock_groq)

        res = await agent.run(goal="Process invoice with error")
        self.assertIn(res["status"], ("running", "stalled", "completed"))

        async with self.pool.acquire() as conn:
            steps = await conn.fetch("SELECT action, result FROM agent_steps WHERE run_id = $1;", uuid.UUID(run_id))
            failure_logged = any("failed" in str(s["result"]).lower() for s in steps)
            self.assertTrue(failure_logged, "Failure should be logged in agent_steps table")

        session_state = await self.redis.get_session_state(run_id)
        self.assertIsNotNone(session_state)
        self.assertEqual(session_state.get("run_id"), run_id)
        self.assertIn("current_step", session_state)
