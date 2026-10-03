import asyncio
import time
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport

from backend.auth import Session
from backend.auth import require_session as require_session_dep
from backend.main import app, run_event_hub, _active_agent_tasks


def _make_mock_pool() -> MagicMock:
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "INSERT 1"
    mock_conn.fetchrow.return_value = None
    mock_conn.fetch.return_value = []
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = False
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm
    mock_pool._mock_conn = mock_conn
    return mock_pool


@asynccontextmanager
async def _fake_browser_session(*args: Any, **kwargs: Any):
    session = MagicMock()
    session.page = MagicMock()
    yield session


async def _noop_override_session() -> Session:
    return Session(sub="operator", exp=9999999999)


class TestIssue29NonblockingRun(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app.dependency_overrides[require_session_dep] = _noop_override_session

    async def asyncTearDown(self) -> None:
        app.dependency_overrides.pop(require_session_dep, None)
        for task in list(_active_agent_tasks.values()):
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass
        _active_agent_tasks.clear()

    async def test_post_returns_202_fast_while_work_continues(self) -> None:
        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        mock_redis = AsyncMock()
        mock_redis.set_session_state.return_value = True

        async def slow_run(self_agent, goal: str) -> Dict[str, Any]:
            await asyncio.sleep(3.0)
            await run_event_hub.publish(
                self_agent.run_id,
                {
                    "type": "step_complete",
                    "run_id": self_agent.run_id,
                    "step_index": 1,
                    "action": "navigate",
                    "result": "navigated",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            return {
                "run_id": self_agent.run_id,
                "status": "completed",
                "iterations": 1,
                "goal": goal,
                "threshold": 50000.0,
                "summary": "done",
            }

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch("backend.redis_client.get_redis_client", return_value=mock_redis),
            patch("backend.browser.get_browser_session", new=_fake_browser_session),
            patch("backend.agent.ReActAgent.run", new=slow_run),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                start = time.perf_counter()
                resp = await ac.post(
                    "/agent/run",
                    json={"goal": "Process all pending invoices"},
                )
                elapsed = time.perf_counter() - start

                self.assertEqual(resp.status_code, 202)
                body = resp.json()
                self.assertIn("run_id", body)
                run_id = body["run_id"]
                uuid.UUID(run_id)
                self.assertEqual(body["status"], "running")
                # Slow work sleeps 3s; a blocking POST would take >=3s.
                self.assertLess(elapsed, 2.0, f"POST /agent/run blocked for {elapsed:.2f}s")

                self.assertTrue(mock_pool._mock_conn.execute.await_count >= 1)

                self.assertIn(run_id, _active_agent_tasks)
                task = _active_agent_tasks[run_id]
                self.assertFalse(task.done())

                queue = run_event_hub.subscribe(run_id)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=6.0)
                    self.assertEqual(event["run_id"], run_id)
                    self.assertIn(event["type"], ("step_complete", "status_change"))
                finally:
                    run_event_hub.unsubscribe(run_id, queue)

                await asyncio.wait_for(task, timeout=10.0)
                self.assertTrue(task.done())

    async def test_sse_mid_run_receives_live_event(self) -> None:
        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        mock_redis = AsyncMock()
        mock_redis.set_session_state.return_value = True

        async def slow_run_with_live_event(self_agent, goal: str) -> Dict[str, Any]:
            # Keep the run alive across the SSE subscription window so the test
            # observes a mid-run stream, not a post-completion replay.
            await asyncio.sleep(1.0)
            await run_event_hub.publish(
                self_agent.run_id,
                {
                    "type": "step_complete",
                    "run_id": self_agent.run_id,
                    "step_index": 1,
                    "action": "navigate",
                    "result": "navigated mid-run",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await asyncio.sleep(4.0)
            return {
                "run_id": self_agent.run_id,
                "status": "completed",
                "iterations": 1,
                "goal": goal,
                "threshold": 50000.0,
                "summary": "done",
            }

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch("backend.redis_client.get_redis_client", return_value=mock_redis),
            patch("backend.browser.get_browser_session", new=_fake_browser_session),
            patch("backend.agent.ReActAgent.run", new=slow_run_with_live_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/agent/run",
                    json={"goal": "Process all pending invoices"},
                )
                self.assertEqual(resp.status_code, 202)
                run_id = resp.json()["run_id"]

                # The SSE endpoint uses the same run_event_hub subscription as a
                # live EventSource: subscribing mid-run must receive live events,
                # not just history replay + ping.
                queue = run_event_hub.subscribe(run_id)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=6.0)
                    self.assertEqual(event["run_id"], run_id)
                    self.assertEqual(event["type"], "step_complete")
                    self.assertIn("mid-run", str(event.get("result", "")))
                finally:
                    run_event_hub.unsubscribe(run_id, queue)

                # The HTTP SSE endpoint itself must be openable mid-run while the
                # background task is still active.
                try:
                    async with asyncio.timeout(2.0):
                        async with ac.stream(
                            "GET",
                            f"/agent/runs/{run_id}/stream",
                        ) as stream_resp:
                            self.assertEqual(stream_resp.status_code, 200)
                            self.assertIn(
                                "text/event-stream",
                                stream_resp.headers.get("content-type", ""),
                            )
                except asyncio.TimeoutError:
                    pass

                task = _active_agent_tasks.get(run_id)
                if task is not None:
                    self.assertFalse(task.done(), "run should still be active mid-stream")
