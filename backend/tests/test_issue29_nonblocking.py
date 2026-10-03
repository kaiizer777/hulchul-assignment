import asyncio
import json
import time
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport

from backend.auth import Session
from backend.auth import require_session as require_session_dep
from backend.main import (
    app,
    run_event_hub,
    _active_agent_tasks,
    _reserved_agent_runs,
    _execute_agent_run_background,
)


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
        _reserved_agent_runs.clear()

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
        from backend.main import stream_agent_run_endpoint

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

                # Subscribe through the real SSE endpoint *before* the run
                # publishes, and assert the step_complete frame actually arrives on
                # the stream. A timeout here is a delivery regression, not a pass.
                # Frames are read from the response body iterator directly: httpx's
                # ASGITransport buffers a whole response body, so an endless SSE
                # stream never surfaces chunks through the HTTP client.
                stream_resp = await stream_agent_run_endpoint(
                    uuid.UUID(run_id),
                    _session=Session(sub="operator", exp=9999999999),
                )
                frames = stream_resp.body_iterator
                delivered: Dict[str, Any] = {}
                try:
                    while not delivered:
                        frame = await asyncio.wait_for(frames.__anext__(), timeout=10.0)
                        payload = json.loads(frame["data"])
                        if payload.get("type") == "step_complete":
                            delivered = payload
                finally:
                    await frames.aclose()

                self.assertTrue(
                    delivered,
                    "SSE stream must deliver the mid-run step_complete frame",
                )
                self.assertEqual(delivered["run_id"], run_id)
                self.assertIn("mid-run", str(delivered.get("result", "")))

                task = _active_agent_tasks.get(run_id)
                if task is not None:
                    self.assertFalse(task.done(), "run should still be active mid-stream")

    async def test_concurrent_same_run_id_single_execution(self) -> None:
        mock_pool = _make_mock_pool()

        async def slow_execute(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return "INSERT 1"

        mock_pool._mock_conn.execute.side_effect = slow_execute

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        mock_redis = AsyncMock()

        async def slow_set_state(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return True

        mock_redis.set_session_state.side_effect = slow_set_state

        run_count = 0

        async def counting_run(self_agent, goal: str) -> Dict[str, Any]:
            nonlocal run_count
            run_count += 1
            await asyncio.sleep(1.0)
            return {
                "run_id": self_agent.run_id,
                "status": "completed",
                "iterations": 1,
                "goal": goal,
                "threshold": 50000.0,
                "summary": "done",
            }

        run_id = str(uuid.uuid4())
        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch("backend.redis_client.get_redis_client", return_value=mock_redis),
            patch("backend.browser.get_browser_session", new=_fake_browser_session),
            patch("backend.agent.ReActAgent.run", new=counting_run),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:

                async def _post_once() -> Any:
                    return await ac.post(
                        "/agent/run",
                        json={"goal": "Process all pending invoices", "run_id": run_id},
                    )

                resp_a, resp_b = await asyncio.gather(_post_once(), _post_once())

                self.assertEqual(resp_a.status_code, 202)
                self.assertEqual(resp_b.status_code, 202)
                self.assertEqual(resp_a.json()["run_id"], run_id)
                self.assertEqual(resp_b.json()["run_id"], run_id)
                self.assertEqual(resp_a.json()["status"], "running")
                self.assertEqual(resp_b.json()["status"], "running")

                task = _active_agent_tasks.get(run_id)
                self.assertIsNotNone(task)
                if task is not None:
                    await asyncio.wait_for(task, timeout=10.0)

                self.assertEqual(run_count, 1)
                self.assertEqual(mock_pool._mock_conn.execute.await_count, 1)
                self.assertNotIn(run_id, _reserved_agent_runs)

    async def test_background_failure_marks_redis_state_failed(self) -> None:
        from backend.main import _execute_agent_run_background

        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        run_id = "22222222-2222-4222-8222-222222222222"
        stored = {
            "run_id": run_id,
            "goal": "Process invoices",
            "current_step": 2,
            "last_action": "navigate",
            "status": "running",
        }
        mock_redis = AsyncMock()
        mock_redis.get_session_state.return_value = dict(stored)
        mock_redis.set_session_state.return_value = True

        @asynccontextmanager
        async def failing_browser(*args: Any, **kwargs: Any):
            raise RuntimeError("CDP endpoint gone")
            yield

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch("backend.redis_client.get_redis_client", return_value=mock_redis),
            patch("backend.browser.get_browser_session", new=failing_browser),
            patch.object(run_event_hub, "publish", new=AsyncMock()) as mock_publish,
        ):
            await _execute_agent_run_background(run_id, "Process invoices")

        # Redis snapshot preserved with status flipped to failed.
        mock_redis.get_session_state.assert_awaited_once_with(run_id)
        failed_writes = [
            c.args[1]
            for c in mock_redis.set_session_state.call_args_list
            if c.args[1].get("status") == "failed"
        ]
        self.assertTrue(failed_writes)
        self.assertEqual(failed_writes[-1]["goal"], "Process invoices")
        self.assertEqual(failed_writes[-1]["current_step"], 2)
        self.assertEqual(failed_writes[-1]["last_action"], "navigate")
        # DB marked failed + SSE status_change failed still published.
        self.assertTrue(mock_pool._mock_conn.execute.await_count >= 1)
        statuses = [c.args[1].get("status") for c in mock_publish.call_args_list]
        self.assertIn("failed", statuses)

    async def test_agent_event_published_to_hub_exactly_once(self) -> None:
        """ReActAgent.emit_event owns hub delivery; main must not forward a second time."""
        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        mock_redis = AsyncMock()
        mock_redis.set_session_state.return_value = True

        run_id = "33333333-3333-4333-8333-333333333333"
        observed_on_event: List[Any] = []

        async def emitting_run(self_agent, goal: str) -> Dict[str, Any]:
            observed_on_event.append(self_agent.on_event)
            for idx in (1, 2, 3):
                await self_agent.emit_event(
                    "step_complete",
                    {
                        "step_id": str(uuid.uuid4()),
                        "step_index": idx,
                        "action": "navigate",
                        "result": "ok",
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
            patch("backend.agent.ReActAgent.run", new=emitting_run),
            patch.object(run_event_hub, "publish", new=AsyncMock()) as mock_publish,
        ):
            await _execute_agent_run_background(run_id, "Process invoices")

        # A hub-publishing on_event callback would double every delivery.
        self.assertEqual(
            observed_on_event,
            [None],
            "run setup must not install a hub-forwarding on_event callback",
        )
        # Three emitted events => exactly three hub publishes.
        self.assertEqual(mock_publish.await_count, 3)
        published_run_ids = [c.args[0] for c in mock_publish.call_args_list]
        self.assertEqual(published_run_ids, [run_id, run_id, run_id])

    async def test_cancelled_background_run_reaches_terminal_state(self) -> None:
        """Cancelling the task must mark DB + Redis terminal and re-raise CancelledError."""
        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        run_id = "44444444-4444-4444-8444-444444444444"
        stored = {
            "run_id": run_id,
            "goal": "Process invoices",
            "current_step": 2,
            "last_action": "navigate",
            "status": "running",
        }
        mock_redis = AsyncMock()
        mock_redis.get_session_state.return_value = dict(stored)
        mock_redis.set_session_state.return_value = True

        started = asyncio.Event()

        async def blocking_run(self_agent, goal: str) -> Dict[str, Any]:
            started.set()
            await asyncio.sleep(30.0)
            raise AssertionError("run should have been cancelled")

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch("backend.redis_client.get_redis_client", return_value=mock_redis),
            patch("backend.browser.get_browser_session", new=_fake_browser_session),
            patch("backend.agent.ReActAgent.run", new=blocking_run),
            patch.object(run_event_hub, "publish", new=AsyncMock()) as mock_publish,
        ):
            task = asyncio.create_task(_execute_agent_run_background(run_id, "Process invoices"))
            await asyncio.wait_for(started.wait(), timeout=5.0)

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(task.cancelled())

        db_statuses = [
            c.args[1]
            for c in mock_pool._mock_conn.execute.call_args_list
            if c.args and c.args[0].startswith("UPDATE agent_runs")
        ]
        self.assertTrue(db_statuses, "cancellation must mark the run failed in the database")
        self.assertEqual(db_statuses[-1], "failed")

        failed_writes = [
            c.args[1]
            for c in mock_redis.set_session_state.call_args_list
            if c.args[1].get("status") == "failed"
        ]
        self.assertTrue(failed_writes, "cancellation must mark the run failed in Redis")
        self.assertEqual(failed_writes[-1]["goal"], "Process invoices")
        self.assertEqual(failed_writes[-1]["current_step"], 2)
        self.assertEqual(failed_writes[-1]["last_action"], "navigate")

        statuses = [c.args[1].get("status") for c in mock_publish.call_args_list]
        self.assertIn("failed", statuses)

    async def test_cancelled_run_still_releases_reservation(self) -> None:
        """A cancelled background task must not leave the run_id reserved."""
        run_id = "55555555-5555-4555-8555-555555555555"

        @asynccontextmanager
        async def hanging_browser(*args: Any, **kwargs: Any):
            started.set()
            await asyncio.Event().wait()
            yield

        started = asyncio.Event()

        with patch("backend.browser.get_browser_session", new=hanging_browser):
            task = asyncio.create_task(_execute_agent_run_background(run_id, "Process invoices"))
            await asyncio.wait_for(started.wait(), timeout=5.0)
            _reserved_agent_runs.add(run_id)
            _active_agent_tasks[run_id] = task

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertNotIn(run_id, _reserved_agent_runs)
        self.assertNotIn(run_id, _active_agent_tasks)

    async def test_sse_stream_has_no_history_to_live_gap(self) -> None:
        """An event published during the history fetch must still reach the subscriber."""
        from backend.main import stream_agent_run_endpoint

        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        run_id = str(uuid.uuid4())
        in_flight_step_id = str(uuid.uuid4())
        history_step_id = str(uuid.uuid4())

        history_row = {
            "step_id": uuid.UUID(history_step_id),
            "action": "navigate",
            "result": "history step",
            "screenshot_b64": None,
            "timestamp": datetime.now(timezone.utc),
        }

        async def fetch_with_inflight_publish(*args: Any, **kwargs: Any) -> List[Any]:
            # Simulates a step persisted + published after the subscription but
            # while history is still being read: it is in neither the history rows
            # nor a post-subscription live read.
            await run_event_hub.publish(
                run_id,
                {
                    "type": "step_complete",
                    "run_id": run_id,
                    "step_id": in_flight_step_id,
                    "step_index": 2,
                    "action": "click",
                    "result": "in-flight step",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            return [history_row]

        mock_pool._mock_conn.fetch.side_effect = fetch_with_inflight_publish

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
        ):
            response = await stream_agent_run_endpoint(
                uuid.UUID(run_id),
                _session=Session(sub="operator", exp=9999999999),
            )
            frames = response.body_iterator

            received: List[str] = []
            try:
                for _ in range(2):
                    frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                    received.append(frame["data"])
            finally:
                await frames.aclose()

        self.assertEqual(len(received), 2)
        payloads = [json.loads(d) for d in received]
        step_ids = [p.get("step_id") for p in payloads]
        self.assertIn(history_step_id, step_ids, "history replay must still be delivered")
        self.assertIn(
            in_flight_step_id,
            step_ids,
            "event published during the history fetch must not be lost",
        )

    async def test_sse_stream_unsubscribes_when_history_fetch_fails(self) -> None:
        """A failing history read must not leak the subscription or strand buffered events."""
        from backend.main import stream_agent_run_endpoint

        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        run_id = str(uuid.uuid4())

        async def failing_fetch(*args: Any, **kwargs: Any) -> List[Any]:
            # Publish, then fail: the event is buffered in the already-created
            # subscription and must still be forwarded by the live loop.
            await run_event_hub.publish(
                run_id,
                {
                    "type": "step_complete",
                    "run_id": run_id,
                    "step_index": 1,
                    "action": "navigate",
                    "result": "ok",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            raise RuntimeError("history read failed")

        mock_pool._mock_conn.fetch.side_effect = failing_fetch

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
        ):
            response = await stream_agent_run_endpoint(
                uuid.UUID(run_id),
                _session=Session(sub="operator", exp=9999999999),
            )
            frames = response.body_iterator

            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            self.assertEqual(json.loads(frame["data"])["action"], "navigate")
            self.assertIn(run_id, run_event_hub._subscribers)
            await frames.aclose()

        self.assertNotIn(
            run_id,
            run_event_hub._subscribers,
            "subscription must be released exactly once on exit",
        )

    async def test_cancelled_run_terminal_write_survives_second_cancel(self) -> None:
        """A second cancellation must not abort the in-flight terminal writes."""
        mock_pool = _make_mock_pool()

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        run_id = "66666666-6666-4666-8666-666666666666"
        mock_redis = AsyncMock()
        mock_redis.get_session_state.return_value = None

        db_write_started = asyncio.Event()
        release_db_write = asyncio.Event()

        async def slow_execute(*args: Any, **kwargs: Any) -> Any:
            db_write_started.set()
            await release_db_write.wait()
            return "UPDATE 1"

        mock_pool._mock_conn.execute.side_effect = slow_execute

        started = asyncio.Event()

        async def blocking_run(self_agent, goal: str) -> Dict[str, Any]:
            started.set()
            await asyncio.sleep(30.0)
            raise AssertionError("run should have been cancelled")

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch("backend.redis_client.get_redis_client", return_value=mock_redis),
            patch("backend.browser.get_browser_session", new=_fake_browser_session),
            patch("backend.agent.ReActAgent.run", new=blocking_run),
            patch.object(run_event_hub, "publish", new=AsyncMock()) as mock_publish,
        ):
            task = asyncio.create_task(_execute_agent_run_background(run_id, "Process invoices"))
            await asyncio.wait_for(started.wait(), timeout=5.0)

            # First cancellation lands while the run is in flight.
            task.cancel()
            # The terminal DB write is now blocked mid-flight.
            await asyncio.wait_for(db_write_started.wait(), timeout=5.0)
            # Second cancellation arrives while the terminal write is suspended.
            task.cancel()

            # Release the blocked DB write; the shielded task must still finish it.
            release_db_write.set()
            for _ in range(50):
                if task.done():
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)

        self.assertTrue(task.cancelled(), "cancellation must still propagate")

        db_statuses = [
            c.args[1]
            for c in mock_pool._mock_conn.execute.call_args_list
            if c.args and c.args[0].startswith("UPDATE agent_runs")
        ]
        self.assertEqual(
            db_statuses[-1:],
            ["failed"],
            "terminal DB write must complete despite the second cancellation",
        )
        statuses = [c.args[1].get("status") for c in mock_publish.call_args_list]
        self.assertIn("failed", statuses)
