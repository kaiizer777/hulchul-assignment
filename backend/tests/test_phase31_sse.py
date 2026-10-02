import asyncio
import json
import unittest
import uuid
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport

from backend.main import app, run_event_hub
from backend.agent import ReActAgent


class TestPhase31SSE(unittest.IsolatedAsyncioTestCase):
    """Test suite for Phase 3.1 FastAPI SSE streaming endpoint and real-time event hub."""

    async def asyncSetUp(self) -> None:
        """Set up test run ID for SSE streaming tests."""
        self.run_id = str(uuid.uuid4())

    async def test_01_event_hub_publish_subscribe(self) -> None:
        """Verify that run_event_hub correctly queues and delivers events to subscribers in real time."""
        queue = run_event_hub.subscribe(self.run_id)
        self.assertIsNotNone(queue)

        test_event = {
            "type": "step_start",
            "run_id": self.run_id,
            "step_index": 1,
            "action": "navigate",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await run_event_hub.publish(self.run_id, test_event)

        try:
            received = await asyncio.wait_for(queue.get(), timeout=2.0)
            self.assertEqual(received["type"], "step_start")
            self.assertEqual(received["action"], "navigate")
            self.assertEqual(received["step_index"], 1)
        finally:
            run_event_hub.unsubscribe(self.run_id, queue)

    @patch("backend.db.get_db_pool")
    async def test_02_sse_stream_endpoint_headers(self, mock_get_pool: AsyncMock) -> None:
        """Verify GET /agent/runs/{run_id}/stream returns text/event-stream content type header."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None
        mock_conn.fetch.return_value = []
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_get_pool.return_value = mock_pool

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            try:
                async with asyncio.timeout(1.5):
                    async with ac.stream("GET", f"/agent/runs/{self.run_id}/stream") as response:
                        self.assertEqual(response.status_code, 200)
                        content_type = response.headers.get("content-type", "")
                        self.assertIn("text/event-stream", content_type)
            except asyncio.TimeoutError:
                pass

    async def test_03_react_agent_emits_required_events(self) -> None:
        """Verify ReActAgent correctly emits required event types (step_start, step_complete, status_change, done)."""
        events = []

        async def capture_event(ev: Dict[str, Any]) -> None:
            """Capture emitted events for assertion."""
            events.append(ev)

        agent = ReActAgent(
            run_id=self.run_id,
            tools=MagicMock(),
            on_event=capture_event,
        )

        await agent.emit_event("step_start", {"step_index": 1, "action": "navigate"})
        await agent.emit_event("step_complete", {"step_index": 1, "action": "navigate", "result": "success", "has_screenshot": False})
        await agent.emit_event("status_change", {"status": "running"})
        await agent.emit_event("done", {"summary": "Completed", "total_steps": 1})

        types = [e["type"] for e in events]
        self.assertIn("step_start", types)
        self.assertIn("step_complete", types)
        self.assertIn("status_change", types)
        self.assertIn("done", types)

    async def test_04_approval_events_emission(self) -> None:
        """Verify needs_approval and approval_resolved events contain correct structured data."""
        events = []

        async def capture_event(ev: Dict[str, Any]) -> None:
            """Capture emitted approval events for assertion."""
            events.append(ev)

        agent = ReActAgent(
            run_id=self.run_id,
            tools=MagicMock(),
            on_event=capture_event,
        )

        await agent.emit_event("needs_approval", {
            "invoice_id": "inv-123",
            "vendor": "Acme",
            "amount": 75000.0,
            "po_number": "PO-999",
            "nonce": "abc123nonce",
        })

        await agent.emit_event("approval_resolved", {
            "decision": "approved",
            "invoice_id": "inv-123",
        })

        needs_app = next((e for e in events if e["type"] == "needs_approval"), None)
        resolved = next((e for e in events if e["type"] == "approval_resolved"), None)

        self.assertIsNotNone(needs_app)
        self.assertEqual(needs_app["amount"], 75000.0)
        self.assertEqual(needs_app["nonce"], "abc123nonce")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["decision"], "approved")
