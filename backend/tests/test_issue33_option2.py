"""Tests for Issue #33 Option 2: Durable state authoritative event streaming.

Verifies that Server-Sent Events (SSE) streaming synthesizes terminal `done`
events from durable database state across serverless/multi-instance setups,
ensuring completion events are delivered reliably even when in-process RunEventHub
subscribers do not exist on the stream instance.
"""

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import backend.main as main_module
from backend.auth import Session
from backend.main import (
    run_event_hub,
    stream_agent_run_endpoint,
)
from backend.tests.test_issue29_nonblocking import (
    _fake_run_row,
    _fake_step_row,
    _fake_step_store,
    _make_mock_pool,
)


async def _open_stream(
    testcase: unittest.IsolatedAsyncioTestCase,
    mock_pool: MagicMock,
    run_id: uuid.UUID,
    poll_interval: float = 0.05,
    ping_interval: float = 0.05,
    reconcile_timeout: float = 0.1,
) -> Any:
    """Open the SSE stream endpoint with patched intervals for fast deterministic testing."""
    async def fake_get_pool() -> MagicMock:
        return mock_pool

    for active in (
        patch("backend.main.get_db_pool", new=fake_get_pool),
        patch("backend.db.get_db_pool", new=fake_get_pool),
        patch.object(main_module, "SSE_DURABLE_POLL_INTERVAL_SECONDS", poll_interval),
        patch.object(main_module, "SSE_PING_INTERVAL_SECONDS", ping_interval),
        patch.object(main_module, "SSE_RECONCILE_TIMEOUT_SECONDS", reconcile_timeout),
    ):
        active.start()
        testcase.addCleanup(active.stop)

    response = await stream_agent_run_endpoint(
        run_id,
        _session=Session(sub="operator", exp=9999999999),
    )
    return response.body_iterator


class TestIssue33Option2DurableTerminalStream(unittest.IsolatedAsyncioTestCase):
    """Test suite verifying durable state authoritative terminal SSE streaming."""

    async def test_cross_instance_stream_synthesizes_done_event_on_completion(self) -> None:
        """Simulate cross-instance execution where RunEventHub has no publisher on this node.

        When the run status flips to 'completed' in the database, the stream
        reconciles durable state, yields pending step rows, yields status_change: completed,
        and synthesizes an event: 'done' frame without in-process hub events.
        """
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        step_id = uuid.uuid4()
        rows: List[Dict[str, Any]] = []
        status = {"value": "running"}

        async def fetchval_status(query: str, run_uuid: Any) -> Any:
            return status["value"]

        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(run_id, "running", created_at)
        mock_pool._mock_conn.fetchval.side_effect = fetchval_status
        mock_pool._mock_conn.fetch.side_effect = _fake_step_store(rows)

        frames = await _open_stream(self, mock_pool, run_id)
        raw_frames: List[Dict[str, Any]] = []
        payloads: List[Dict[str, Any]] = []

        try:
            # 1. Connect and receive initial status_change
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)
            payloads.append(json.loads(frame["data"]))

            # 2. Simulate cross-instance completion in DB:
            # - New step persisted
            # - Status flipped to 'completed'
            # - NO events published to RunEventHub
            rows.append(
                _fake_step_row(
                    step_id,
                    "verify_invoice",
                    "Invoice verified successfully",
                    created_at + timedelta(seconds=1),
                )
            )
            status["value"] = "completed"

            # 3. Read subsequent frames until 'done' is received
            while True:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                raw_frames.append(frame)
                payload = json.loads(frame["data"])
                payloads.append(payload)
                if frame.get("event") == "done":
                    break
        finally:
            await frames.aclose()

        # Assert initial status change
        self.assertEqual(payloads[0]["type"], "status_change")
        self.assertEqual(payloads[0]["status"], "running")

        # Assert step row yielded
        step_payloads = [p for p in payloads if p.get("step_id") == str(step_id)]
        self.assertEqual(len(step_payloads), 1)
        self.assertEqual(step_payloads[0]["action"], "verify_invoice")
        self.assertEqual(step_payloads[0]["type"], "step_complete")

        # Assert status_change: completed yielded
        status_changes = [p for p in payloads if p.get("type") == "status_change" and p.get("status") == "completed"]
        self.assertEqual(len(status_changes), 1)

        # Assert synthesized done frame
        done_raw_frames = [f for f in raw_frames if f.get("event") == "done"]
        self.assertEqual(len(done_raw_frames), 1)
        done_payload = json.loads(done_raw_frames[0]["data"])
        self.assertEqual(done_payload["type"], "done")
        self.assertEqual(done_payload["run_id"], run_id_str)
        self.assertEqual(done_payload["status"], "completed")
        self.assertIn("timestamp", done_payload)

    async def test_connect_to_already_completed_run_replays_history_and_emits_done(self) -> None:
        """Connect to a run with initial status 'completed'.

        The stream must yield initial status_change, all historical steps, and
        the synthesized terminal done frame during history playback.
        """
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        step_1_id = uuid.uuid4()
        step_2_id = uuid.uuid4()
        rows: List[Dict[str, Any]] = [
            _fake_step_row(step_1_id, "navigate", "navigated to portal", created_at),
            _fake_step_row(step_2_id, "extract_po", "extracted PO-9901", created_at + timedelta(seconds=1)),
        ]

        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(run_id, "completed", created_at)
        mock_pool._mock_conn.fetchval.return_value = "completed"
        mock_pool._mock_conn.fetch.side_effect = _fake_step_store(rows)

        frames = await _open_stream(self, mock_pool, run_id)
        raw_frames: List[Dict[str, Any]] = []
        payloads: List[Dict[str, Any]] = []

        try:
            # Exactly 4 historical frames expected:
            # 1. status_change: completed
            # 2. step 1
            # 3. step 2
            # 4. done frame
            for _ in range(4):
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                raw_frames.append(frame)
                payloads.append(json.loads(frame["data"]))

            # Let several poll cycles run over the completed state to ensure no duplicate done
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.25
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(frames.__anext__(), timeout=remaining)
                    raw_frames.append(frame)
                    payloads.append(json.loads(frame["data"]))
                except asyncio.TimeoutError:
                    break
        finally:
            await frames.aclose()

        # Check sequence
        self.assertEqual(raw_frames[0]["event"], "status_change")
        self.assertEqual(payloads[0]["status"], "completed")

        self.assertEqual(raw_frames[1]["event"], "step_complete")
        self.assertEqual(payloads[1]["step_id"], str(step_1_id))

        self.assertEqual(raw_frames[2]["event"], "step_complete")
        self.assertEqual(payloads[2]["step_id"], str(step_2_id))

        self.assertEqual(raw_frames[3]["event"], "done")
        self.assertEqual(payloads[3]["type"], "done")
        self.assertEqual(payloads[3]["run_id"], run_id_str)
        self.assertEqual(payloads[3]["status"], "completed")

        # Verify no duplicate done events were emitted
        done_events = [f for f in raw_frames if f.get("event") == "done"]
        self.assertEqual(len(done_events), 1, f"Expected 1 done event, got {len(done_events)}")

    async def test_live_done_event_is_not_duplicated_by_durable_reconcile(self) -> None:
        """When a live 'done' event arrives from RunEventHub, subsequent durable reconcile does not duplicate it."""
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = []
        status = {"value": "running"}

        async def fetchval_status(query: str, run_uuid: Any) -> Any:
            return status["value"]

        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(run_id, "running", created_at)
        mock_pool._mock_conn.fetchval.side_effect = fetchval_status
        mock_pool._mock_conn.fetch.side_effect = _fake_step_store(rows)

        frames = await _open_stream(self, mock_pool, run_id)
        raw_frames: List[Dict[str, Any]] = []
        payloads: List[Dict[str, Any]] = []

        try:
            # Drain initial status_change
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)
            payloads.append(json.loads(frame["data"]))

            # Publish live 'done' event via RunEventHub
            await run_event_hub.publish(
                run_id_str,
                {
                    "type": "done",
                    "run_id": run_id_str,
                    "status": "completed",
                    "summary": "Task complete",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

            # Receive live done frame
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)
            payloads.append(json.loads(frame["data"]))
            self.assertEqual(frame.get("event"), "done")

            # Update DB status to 'completed'
            status["value"] = "completed"

            # Let multiple reconcile poll cycles run over the completed DB status
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.3
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(frames.__anext__(), timeout=remaining)
                    raw_frames.append(frame)
                    payloads.append(json.loads(frame["data"]))
                except asyncio.TimeoutError:
                    break
        finally:
            await frames.aclose()

        # Count total done events received
        done_events = [f for f in raw_frames if f.get("event") == "done"]
        self.assertEqual(
            len(done_events),
            1,
            f"Expected exactly 1 done event, but got {len(done_events)}: {raw_frames}",
        )

    async def test_completed_run_continues_pinging_without_closing(self) -> None:
        """Verify stream stays open and emits keep-alive pings after terminal 'completed' status."""
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = []

        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(run_id, "completed", created_at)
        mock_pool._mock_conn.fetchval.return_value = "completed"
        mock_pool._mock_conn.fetch.side_effect = _fake_step_store(rows)

        # Patched ping interval = 0.05s
        frames = await _open_stream(self, mock_pool, run_id, ping_interval=0.05, poll_interval=0.05)
        raw_frames: List[Dict[str, Any]] = []

        try:
            # 1. Read initial status_change
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)

            # 2. Read synthesized done frame
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)
            self.assertEqual(frame.get("event"), "done")

            # 3. Collect multiple ping frames while stream remains open
            ping_count = 0
            deadline = asyncio.get_running_loop().time() + 0.3
            while ping_count < 3 and asyncio.get_running_loop().time() < deadline:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                raw_frames.append(frame)
                if frame.get("event") == "ping":
                    ping_count += 1
                    data = json.loads(frame["data"])
                    self.assertEqual(data["type"], "ping")
                    self.assertEqual(data["run_id"], run_id_str)

            self.assertGreaterEqual(
                ping_count,
                3,
                f"Expected at least 3 keep-alive ping frames after completion, got {ping_count}",
            )
        finally:
            await frames.aclose()

    async def test_buffered_stale_status_change_is_not_emitted_after_done(self) -> None:
        """A stale status_change: running buffered before completion must not be emitted after done or regress status."""
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = []

        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(run_id, "completed", created_at)
        mock_pool._mock_conn.fetchval.return_value = "completed"
        mock_pool._mock_conn.fetch.side_effect = _fake_step_store(rows)

        # Buffer a stale 'running' status event into the hub queue during history fetch
        async def fake_fetchrow(*args: Any, **kwargs: Any) -> Any:
            await run_event_hub.publish(
                run_id_str,
                {
                    "type": "status_change",
                    "run_id": run_id_str,
                    "status": "running",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            return _fake_run_row(run_id, "completed", created_at)

        mock_pool._mock_conn.fetchrow.side_effect = fake_fetchrow

        frames = await _open_stream(self, mock_pool, run_id, poll_interval=0.05, ping_interval=0.05)
        raw_frames: List[Dict[str, Any]] = []
        payloads: List[Dict[str, Any]] = []

        try:
            # 1. Read initial status_change: completed
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)
            payloads.append(json.loads(frame["data"]))

            # 2. Read synthesized done frame
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            raw_frames.append(frame)
            payloads.append(json.loads(frame["data"]))

            # 3. Read subsequent frames to verify the stale 'running' status was discarded
            deadline = asyncio.get_running_loop().time() + 0.25
            while asyncio.get_running_loop().time() < deadline:
                try:
                    frame = await asyncio.wait_for(frames.__anext__(), timeout=0.1)
                    raw_frames.append(frame)
                    payloads.append(json.loads(frame["data"]))
                except asyncio.TimeoutError:
                    break
        finally:
            await frames.aclose()

        statuses = [p.get("status") for p in payloads if p.get("type") == "status_change"]
        self.assertEqual(
            statuses,
            ["completed"],
            f"Stale buffered 'running' status must be discarded, but got statuses: {statuses}",
        )


if __name__ == "__main__":
    unittest.main()
