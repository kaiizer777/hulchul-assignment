"""Issue #33 residual gaps left after PR #54.

Two defects survive the durable-state reconciliation merged in PR #54:

1. A durable hub frame that carries no ``step_id`` cannot be deduped. The live
   path's dedupe gate is keyed on ``step_id``, so a ``step_failed`` or terminal
   ``session_lost`` published without one is yielded while ``emitted_step_ids``
   stays unaware of the row, and the durable poll re-emits the same row a few
   seconds later. The two agent emit sites that persist a row but never thread
   the returned id into the payload are covered here.

2. The reconcile read is unbounded. ``queue.get()`` is bounded by
   ``asyncio.wait_for`` but the reconcile coroutine is awaited bare, so a slow or
   hung Neon query blocks the generator indefinitely and stops the keep-alive
   ping. Covered here along with the cursor and status invariants a bound has to
   preserve.
"""

import asyncio
import json
import unittest
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import backend.main as main_module
from backend.agent import ReActAgent
from backend.auth import Session
from backend.main import (
    _SSE_STEPS_AFTER_CURSOR_SQL,
    run_event_hub,
)
from backend.tools import PlaywrightTools
from backend.tests.test_issue29_nonblocking import (
    _cursor_poll_count,
    _fake_run_row,
    _fake_step_row,
    _fake_step_store,
    _make_mock_pool,
)

# Every persist_step call the agent made, as (action, returned step_id).
PersistedStep = Tuple[str, str]


def _mock_redis() -> MagicMock:
    """Build an unconfigured mock Redis client so an agent run skips pause handling."""
    redis = MagicMock()
    redis.is_configured = False
    redis.set_session_state = AsyncMock(return_value=True)
    redis.get_pause_flag = AsyncMock(return_value=False)
    return redis


def _fake_step_store_hanging_first_polls(
    rows: List[Dict[str, Any]],
    hangs: int = 1,
    entered: List[str] | None = None,
):
    """Wrap the proven _fake_step_store so the first `hangs` cursor polls never return.

    A poll that never returns is what a stalled Neon connection looks like from
    inside the stream: the awaited read can only be bounded from outside, so the
    fake suspends on an Event that is deliberately never set and lets the caller's
    ``asyncio.wait_for`` cancel it. Every later poll delegates to _fake_step_store
    unchanged, so the (timestamp, step_id) cursor comparison is honoured exactly as
    in the unsuspended case and a test cannot pass against a fake that ignores the
    cursor.

    Only a cursor query is ever hung, so a caller that wants the stream to reach
    the hang must seed at least one replayed row: with no rows the stream's
    ``step_cursor`` stays None and the poll reads _SSE_STEPS_ALL_SQL instead, so
    the hang would never be entered. ``entered`` records each hang that was
    actually reached, so a test can assert it stalled instead of passing without
    ever doing so.
    """
    store = _fake_step_store(rows)
    never_set = asyncio.Event()
    state = {"hangs": hangs}

    async def fetch_steps(query: str, run_uuid: Any, *args: Any) -> List[Any]:
        if query == _SSE_STEPS_AFTER_CURSOR_SQL and state["hangs"] > 0:
            state["hangs"] -= 1
            if entered is not None:
                entered.append(query)
            await never_set.wait()
        return await store(query, run_uuid, *args)

    return fetch_steps


def _cursor_call_args(mock_pool: MagicMock) -> List[tuple]:
    """Every cursor-query call, in order, as (cursor_timestamp, cursor_step_id)."""
    return [
        c.args[2:]
        for c in mock_pool._mock_conn.fetch.call_args_list
        if c.args and c.args[0] == _SSE_STEPS_AFTER_CURSOR_SQL
    ]


async def _open_stream(testcase: unittest.IsolatedAsyncioTestCase, mock_pool: MagicMock, run_id: uuid.UUID) -> Any:
    """Open the stream endpoint with its pool and cadence constants patched.

    The patches stay active for the whole test because the generator body does not
    run until the first frame is pulled, and every later poll reads through them.
    Every deadline comes from a patched module constant, so a test never waits on
    the production 3s poll, 15s ping or reconcile bound.
    """
    async def fake_get_pool() -> MagicMock:
        return mock_pool

    from backend.main import stream_agent_run_endpoint

    for active in (
        patch("backend.main.get_db_pool", new=fake_get_pool),
        patch("backend.db.get_db_pool", new=fake_get_pool),
        patch.object(main_module, "SSE_DURABLE_POLL_INTERVAL_SECONDS", 0.05),
        patch.object(main_module, "SSE_PING_INTERVAL_SECONDS", 0.05),
        patch.object(main_module, "SSE_RECONCILE_TIMEOUT_SECONDS", 0.1),
    ):
        active.start()
        testcase.addCleanup(active.stop)

    response = await stream_agent_run_endpoint(
        run_id,
        _session=Session(sub="operator", exp=9999999999),
    )
    return response.body_iterator


def _step_id_for(persisted: List[PersistedStep], action: str) -> str:
    """Return the id persist_step handed back for the row with this action."""
    for persisted_action, step_id in persisted:
        if persisted_action == action:
            return step_id
    raise AssertionError(f"no persist_step call for action={action!r} in {persisted}")


class TestAgentDurableFrameCorrelation(unittest.IsolatedAsyncioTestCase):
    """Every agent_steps row the agent persists must reach the hub carrying its step_id.

    The stream can only correlate a live frame with the durable row behind it by
    step_id. persist_step already returns that id and the act-phase emit sites
    already thread it into the payload; these two tests pin the two emit sites
    that persisted a row and published without it.
    """

    async def _make_agent(
        self,
        mock_groq: MagicMock,
        events: List[Dict[str, Any]],
        persisted: List[PersistedStep],
        mock_tools: MagicMock | None = None,
        reconnect: AsyncMock | None = None,
        max_reattaches: int = 2,
        max_iterations: int = 5,
    ) -> ReActAgent:
        """Build a ReActAgent whose persistence and Redis side effects are stubbed out.

        persist_step hands back a distinct id per call and records (action, id) in
        call order, so a test asserts the emitted payload carries the id of the row
        that call actually wrote instead of a hardcoded constant.
        """

        async def _on_event(evt: Dict[str, Any]) -> None:
            events.append(evt)

        if mock_tools is None:
            # read_page must be an explicit AsyncMock: a bare spec'd MagicMock
            # child is not awaitable, so the observe step would raise instead of
            # returning a snapshot and the run would take a different path.
            mock_tools = MagicMock(spec=PlaywrightTools)
            mock_tools.page = MagicMock()
            mock_tools.page.is_closed = MagicMock(return_value=False)
            mock_tools.read_page = AsyncMock(
                return_value={
                    "success": True,
                    "snapshot": "x",
                    "url": "http://x",
                    "title": "T",
                }
            )

        async def fake_persist_step(**kwargs: Any) -> str:
            step_id = str(uuid.uuid4())
            persisted.append((kwargs.get("action"), step_id))
            return step_id

        agent = ReActAgent(
            run_id=str(uuid.uuid4()),
            tools=mock_tools,
            groq_client=mock_groq,
            redis_client=_mock_redis(),
            max_iterations=max_iterations,
            on_event=_on_event,
            reconnect=reconnect,
            max_reattaches=max_reattaches,
        )
        agent.ensure_run_record = AsyncMock(return_value=None)
        agent.get_last_successful_step_index = AsyncMock(return_value=0)
        agent.persist_step = AsyncMock(side_effect=fake_persist_step)
        agent.update_run_status = AsyncMock(return_value=None)
        return agent

    async def test_llm_think_failure_event_carries_persisted_step_id(self) -> None:
        """A failed LLM call publishes step_failed with the id of the row it persisted."""
        events: List[Dict[str, Any]] = []
        persisted: List[PersistedStep] = []
        mock_groq = MagicMock()
        mock_groq.chat = MagicMock(
            completions=MagicMock(
                create=AsyncMock(side_effect=Exception("groq upstream 503"))
            )
        )
        agent = await self._make_agent(mock_groq, events, persisted, max_iterations=1)

        await agent.run(goal="Process all pending invoices")

        failed = [e for e in events if e["type"] == "step_failed"]
        self.assertEqual(len(failed), 1, f"expected one step_failed, got {failed}")
        self.assertEqual(failed[0].get("action"), "llm_think")
        self.assertEqual(
            failed[0].get("step_id"),
            _step_id_for(persisted, "llm_think"),
            "step_failed must carry the step_id of the row persist_step wrote, "
            "otherwise the stream cannot correlate it with the durable poll",
        )

    async def test_terminal_session_lost_event_carries_persisted_step_id(self) -> None:
        """Exhausting the reattach cap publishes session_lost with the row's step_id."""
        events: List[Dict[str, Any]] = []
        persisted: List[PersistedStep] = []
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.page = MagicMock()
        mock_tools.page.is_closed = MagicMock(return_value=False)
        mock_tools.set_run_id = MagicMock()
        mock_tools.set_page = MagicMock()
        mock_tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "x", "url": "http://x", "title": "T"}
        )
        mock_tools.execute = AsyncMock(
            return_value={
                "success": False,
                "error": "Target page, context or browser has been closed",
                "session_lost": True,
            }
        )
        mock_tools.take_screenshot = AsyncMock(return_value={"success": False})
        reconnect = AsyncMock(return_value=MagicMock())
        msg = MagicMock()
        tc = MagicMock()
        tc.id = "loop"
        tc.function.name = "navigate"
        tc.function.arguments = json.dumps({"url": "/invoices"})
        msg.tool_calls = [tc]
        msg.content = None
        mock_groq = MagicMock()
        mock_groq.chat = MagicMock(
            completions=MagicMock(
                create=AsyncMock(return_value=MagicMock(choices=[MagicMock(message=msg)]))
            )
        )
        agent = await self._make_agent(
            mock_groq,
            events,
            persisted,
            mock_tools=mock_tools,
            reconnect=reconnect,
            max_reattaches=2,
            max_iterations=5,
        )

        result = await agent.run(goal="Process all pending invoices")

        self.assertEqual(result["status"], "session_lost")
        terminal = [e for e in events if e["type"] == "session_lost" and e.get("terminal") is True]
        self.assertEqual(len(terminal), 1, f"expected one terminal session_lost, got {terminal}")
        self.assertEqual(
            terminal[0].get("step_id"),
            _step_id_for(persisted, "session_lost"),
            "terminal session_lost must carry the step_id of the row persist_step "
            "wrote, otherwise the durable poll re-emits it and the frontend "
            "terminal-status branch fires twice",
        )


class TestSseDedupeOfCorrelatedFrames(unittest.IsolatedAsyncioTestCase):
    """Once a durable frame carries its step_id, the stream delivers it exactly once.

    These tests pair with the agent tests above: they pin that the stream's
    existing dedupe set suppresses the poll's re-read of a row whose live frame
    already went out. They hold regardless of which agent code path produced the
    frame, so they are the stream-side half of the same guarantee.
    """

    async def _deliver_live_frame_over_durable_row(
        self, live_event: Dict[str, Any], durable_row: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Publish a live frame for a row that also exists durably; return every payload.

        The live frame goes out first, then the row becomes visible to the cursor
        poll, so the poll is guaranteed to read a row the live path already
        delivered. Every step_id in the drained frames must appear exactly once.
        """
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        history_step_id = uuid.uuid4()
        rows: List[Dict[str, Any]] = [
            _fake_step_row(history_step_id, "navigate", "loaded", created_at)
        ]

        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(run_id, "running", created_at)
        mock_pool._mock_conn.fetchval.return_value = "running"
        store = _fake_step_store(rows)
        offered: List[List[uuid.UUID]] = []

        async def recording_fetch(query: str, run_uuid: Any, *args: Any) -> List[Any]:
            result = await store(query, run_uuid, *args)
            if query == _SSE_STEPS_AFTER_CURSOR_SQL:
                offered.append([r["step_id"] for r in result])
            return result

        mock_pool._mock_conn.fetch.side_effect = recording_fetch

        frames = await _open_stream(self, mock_pool, run_id)
        payloads: List[Dict[str, Any]] = []
        try:
            # Drain the replay first so the row below is committed strictly after the
            # stream read it and only the poll can deliver it.
            for _ in range(2):
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                payloads.append(json.loads(frame["data"]))
            await run_event_hub.publish(run_id_str, live_event)
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            payloads.append(json.loads(frame["data"]))
            rows.append(durable_row)
            # Let several poll cycles run over the row the live path already
            # delivered. Frames are collected rather than the next read being
            # expected to time out, because the patched 0.05s ping cadence puts
            # keep-alives into the same window and would mask a duplicate.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.4
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(frames.__anext__(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                payloads.append(json.loads(frame["data"]))
        finally:
            await frames.aclose()

        self.assertGreaterEqual(
            _cursor_poll_count(mock_pool),
            2,
            "the poll must have run over the row the live path already delivered",
        )
        live_step_id = live_event["step_id"]
        self.assertTrue(
            any(uuid.UUID(live_step_id) in batch for batch in offered),
            "the durable poll must actually have offered this row; otherwise the "
            "dedupe is never exercised and the assertions below pass vacuously",
        )
        delivered = [p for p in payloads if p.get("step_id") == live_step_id]
        self.assertEqual(
            len(delivered),
            1,
            f"live frame delivered {len(delivered)}x: "
            f"{[p.get('step_id') for p in payloads if p.get('step_id')]}",
        )
        self.assertEqual(
            delivered[0]["type"],
            live_event["type"],
            "the single delivery must be the live frame, not the poll's re-read",
        )
        step_ids = [p.get("step_id") for p in payloads if p.get("step_id")]
        self.assertEqual(len(step_ids), 2, f"unexpected duplicate deliveries: {step_ids}")
        return payloads

    async def test_step_failed_hub_frame_is_delivered_once(self) -> None:
        """A step_failed frame carrying its step_id is not re-emitted by the poll."""
        step_id = str(uuid.uuid4())
        # Strictly after the replayed row: a row sharing the replay's timestamp
        # would sit before or after the (timestamp, step_id) cursor on UUID
        # ordering alone, so the poll would sometimes never offer it and the test
        # would pass without the dedupe doing anything.
        committed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        payloads = await self._deliver_live_frame_over_durable_row(
            {
                "type": "step_failed",
                "run_id": str(uuid.uuid4()),
                "step_id": step_id,
                "action": "llm_think",
                "error": "groq upstream 503",
                "timestamp": committed_at.isoformat(),
            },
            _fake_step_row(
                uuid.UUID(step_id), "llm_think", "failed: groq upstream 503", committed_at
            ),
        )
        failed = [p for p in payloads if p["type"] == "step_failed"]
        self.assertEqual(len(failed), 1, f"step_failed delivered {len(failed)}x: {failed}")

    async def test_session_lost_hub_frame_is_delivered_once_with_terminal(self) -> None:
        """A terminal session_lost frame reaches the client once, terminal flag intact."""
        step_id = str(uuid.uuid4())
        committed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        payloads = await self._deliver_live_frame_over_durable_row(
            {
                "type": "session_lost",
                "run_id": str(uuid.uuid4()),
                "step_id": step_id,
                "step": 4,
                "error": "Target page, context or browser has been closed",
                "terminal": True,
                "reattached": False,
                "attempts": 2,
                "max_attempts": 2,
                "timestamp": committed_at.isoformat(),
            },
            _fake_step_row(
                uuid.UUID(step_id),
                "session_lost",
                "session_lost: Target page, context or browser has been closed after 2 reattach attempt(s)",
                committed_at,
            ),
        )
        session_lost = [p for p in payloads if p["type"] == "session_lost"]
        self.assertEqual(len(session_lost), 1, f"session_lost delivered {len(session_lost)}x")
        self.assertTrue(
            session_lost[0].get("terminal"),
            "terminal must reach the client: the frontend fires its terminal-status "
            "branch off this flag",
        )
        self.assertFalse(session_lost[0].get("reattached"))


class TestSseReconcileBound(unittest.IsolatedAsyncioTestCase):
    """A stalled reconcile read must not stall the stream or lose durable state.

    ``queue.get()`` is already bounded by asyncio.wait_for. These tests pin the
    same bound on the reconcile coroutine and the two invariants a bound has to
    preserve: the cursor is only advanced once the rows are actually in hand, and
    a status read is not consumed by a reconcile that never completed.
    """

    def _hanging_pool(
        self,
        run_id: uuid.UUID,
        rows: List[Dict[str, Any]],
        hangs: int,
        run_status: Dict[str, str] | None = None,
        entered: List[str] | None = None,
    ) -> MagicMock:
        mock_pool = _make_mock_pool()
        mock_pool._mock_conn.fetchrow.return_value = _fake_run_row(
            run_id, (run_status or {}).get("value", "running"), datetime.now(timezone.utc)
        )
        if run_status is None:
            mock_pool._mock_conn.fetchval.return_value = "running"
        else:
            mock_pool._mock_conn.fetchval.side_effect = lambda *a, **kw: run_status["value"]
        mock_pool._mock_conn.fetch.side_effect = _fake_step_store_hanging_first_polls(
            rows, hangs=hangs, entered=entered
        )
        return mock_pool

    async def test_hung_reconcile_does_not_stop_ping_and_does_not_skip_row(self) -> None:
        """A reconcile that never returns must still yield a ping, then the row."""
        run_id = uuid.uuid4()
        created_at = datetime.now(timezone.utc)
        history_step_id = uuid.uuid4()
        late_step_id = uuid.uuid4()
        rows: List[Dict[str, Any]] = [
            _fake_step_row(history_step_id, "navigate", "loaded", created_at)
        ]
        mock_pool = self._hanging_pool(run_id, rows, hangs=1)

        frames = await _open_stream(self, mock_pool, run_id)
        payloads: List[Dict[str, Any]] = []
        try:
            for _ in range(2):
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                payloads.append(json.loads(frame["data"]))
            rows.append(
                _fake_step_row(
                    late_step_id, "click", "clicked approve", created_at + timedelta(seconds=1)
                )
            )
            while True:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                payload = json.loads(frame["data"])
                payloads.append(payload)
                if payload.get("step_id") == str(late_step_id):
                    break
        finally:
            await frames.aclose()

        pings = [p for p in payloads if p["type"] == "ping"]
        self.assertTrue(
            pings,
            "the keep-alive ping must still be emitted while a reconcile read hangs; "
            "an unbounded reconcile read silently stalls the stream",
        )
        ping_at = payloads.index(pings[0])
        row_at = next(
            i for i, p in enumerate(payloads) if p.get("step_id") == str(late_step_id)
        )
        self.assertLess(
            ping_at,
            row_at,
            "the ping must arrive before the row, i.e. the hung poll was abandoned "
            "and the stream kept running",
        )
        step_ids = [p.get("step_id") for p in payloads if p.get("step_id")]
        self.assertEqual(
            step_ids.count(str(late_step_id)),
            1,
            f"the row behind the timed-out poll must not be skipped or repeated: {step_ids}",
        )

    async def test_reconcile_timeout_leaves_cursor_so_gap_is_reread(self) -> None:
        """A timed-out poll must not advance the cursor, so the gap is re-read."""
        run_id = uuid.uuid4()
        created_at = datetime.now(timezone.utc)
        history_step_id = uuid.uuid4()
        late_step_id = uuid.uuid4()
        rows: List[Dict[str, Any]] = [
            _fake_step_row(history_step_id, "navigate", "loaded", created_at)
        ]
        mock_pool = self._hanging_pool(run_id, rows, hangs=1)

        frames = await _open_stream(self, mock_pool, run_id)
        try:
            for _ in range(2):
                await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            rows.append(
                _fake_step_row(
                    late_step_id, "click", "clicked approve", created_at + timedelta(seconds=1)
                )
            )
            while True:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                if json.loads(frame["data"]).get("step_id") == str(late_step_id):
                    break
        finally:
            await frames.aclose()

        cursor_calls = _cursor_call_args(mock_pool)
        self.assertGreaterEqual(
            len(cursor_calls),
            2,
            "the poll must have run again after the timeout, or the gap was never re-read",
        )
        replay_cursor = (created_at, history_step_id)
        self.assertEqual(
            cursor_calls[0],
            replay_cursor,
            "the first poll must resume from the replay's cursor",
        )
        self.assertEqual(
            cursor_calls[1],
            replay_cursor,
            "the poll after a timeout must re-read from the same cursor: a timeout "
            "must not advance it, or the row committed during the stall is skipped",
        )

    async def test_status_change_survives_a_reconcile_timeout(self) -> None:
        """A status read must not be consumed by a reconcile that then times out."""
        run_id = uuid.uuid4()
        created_at = datetime.now(timezone.utc)
        history_step_id = uuid.uuid4()
        late_step_id = uuid.uuid4()
        rows: List[Dict[str, Any]] = [
            _fake_step_row(history_step_id, "navigate", "loaded", created_at)
        ]
        run_status = {"value": "running"}
        mock_pool = self._hanging_pool(run_id, rows, hangs=1, run_status=run_status)

        frames = await _open_stream(self, mock_pool, run_id)
        payloads: List[Dict[str, Any]] = []
        try:
            for _ in range(2):
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                payloads.append(json.loads(frame["data"]))
            # The status flips and a step lands, then the next poll's step read hangs.
            run_status["value"] = "failed"
            rows.append(
                _fake_step_row(
                    late_step_id, "click", "clicked approve", created_at + timedelta(seconds=1)
                )
            )
            while True:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                payload = json.loads(frame["data"])
                payloads.append(payload)
                if payload.get("step_id") == str(late_step_id):
                    break
        finally:
            await frames.aclose()

        statuses = [p["status"] for p in payloads if p["type"] == "status_change"]
        self.assertEqual(
            statuses,
            ["running", "failed"],
            "the status read before the hung step query must still be delivered: "
            "advancing last_status inside the timed-out reconcile drops the "
            "transition on the floor with the discarded frames",
        )

    async def test_cancelled_reconcile_terminates_the_pooled_connection(self) -> None:
        """A read cancelled mid-query must terminate, not hand back, its connection.

        Returning a connection that was cancelled mid-query makes pool release
        block on asyncpg's cancellation wait and reset, which is unbounded here
        because the pool sets no command_timeout -- so wait_for would wait on that
        cleanup instead of returning.
        """
        run_id = uuid.uuid4()
        created_at = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = [
            _fake_step_row(uuid.uuid4(), "navigate", "loaded", created_at)
        ]
        mock_pool = self._hanging_pool(run_id, rows, hangs=1)
        conn = mock_pool._mock_conn
        # asyncpg's Connection.terminate is synchronous, but an AsyncMock would
        # auto-create it as a coroutine function and never run the call.
        conn.terminate = MagicMock()

        frames = await _open_stream(self, mock_pool, run_id)
        try:
            for _ in range(2):
                await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            # Let the bound cancel the hanging read.
            deadline = asyncio.get_running_loop().time() + 5.0
            while not conn.terminate.call_count:
                self.assertLess(
                    asyncio.get_running_loop().time(),
                    deadline,
                    "the cancelled reconcile never terminated its connection",
                )
                await asyncio.wait_for(frames.__anext__(), timeout=1.0)
        finally:
            await frames.aclose()

        self.assertGreaterEqual(
            conn.terminate.call_count,
            1,
            "a connection cancelled mid-query must not be returned to the pool",
        )

    async def test_reconcile_timeout_is_logged_apart_from_a_read_failure(self) -> None:
        """A timeout must be distinguishable in logs from a query that errored."""
        run_id = uuid.uuid4()
        created_at = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = [
            _fake_step_row(uuid.uuid4(), "navigate", "loaded", created_at)
        ]
        mock_pool = self._hanging_pool(run_id, rows, hangs=1)

        with self.assertLogs(level="WARNING") as logs:
            frames = await _open_stream(self, mock_pool, run_id)
            try:
                for _ in range(2):
                    await asyncio.wait_for(frames.__anext__(), timeout=5.0)
                # The generator only advances as frames are pulled, so keep pulling
                # until the bound has abandoned the hanging poll.
                deadline = asyncio.get_running_loop().time() + 5.0
                while not any("Timed out reconciling durable state" in m for m in logs.output):
                    self.assertLess(
                        asyncio.get_running_loop().time(),
                        deadline,
                        f"the reconcile timeout was never logged: {logs.output}",
                    )
                    await asyncio.wait_for(frames.__anext__(), timeout=1.0)
            finally:
                await frames.aclose()

        self.assertFalse(
            [m for m in logs.output if "Error reconciling durable state" in m],
            f"a timeout must not be reported as a read failure: {logs.output}",
        )
        self.assertTrue(
            any(str(run_id) in m for m in logs.output if "Timed out reconciling" in m),
            f"the timeout log must identify the stream: {logs.output}",
        )

    async def test_client_disconnect_tears_down_a_stream_with_a_hung_reconcile(self) -> None:
        """A disconnect must still close the stream while a reconcile read hangs."""
        run_id = uuid.uuid4()
        run_id_str = str(run_id)
        created_at = datetime.now(timezone.utc)
        # One replayed row is required: it is what leaves step_cursor non-None, so
        # the poll reads the cursor query the fake hangs on. With an empty step
        # list the stream would poll _SSE_STEPS_ALL_SQL and never stall.
        rows: List[Dict[str, Any]] = [
            _fake_step_row(uuid.uuid4(), "navigate", "loaded", created_at)
        ]
        entered: List[str] = []
        # Every cursor poll hangs, so the stream is wedged in the reconcile at the
        # moment the client goes away.
        mock_pool = self._hanging_pool(run_id, rows, hangs=10_000, entered=entered)

        async def fake_get_pool() -> MagicMock:
            return mock_pool

        disconnect_now = asyncio.Event()
        request_sent = asyncio.Event()
        body: List[bytes] = []

        async def receive() -> Dict[str, Any]:
            if not request_sent.is_set():
                request_sent.set()
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect_now.wait()
            return {"type": "http.disconnect"}

        async def send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                body.append(message.get("body", b""))

        path = f"/agent/runs/{run_id_str}/stream"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        from backend.auth import require_session as require_session_dep
        from backend.main import app

        async def _noop_override_session() -> Session:
            return Session(sub="operator", exp=9999999999)

        app.dependency_overrides[require_session_dep] = _noop_override_session
        self.addCleanup(app.dependency_overrides.pop, require_session_dep, None)

        with (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch.object(main_module, "SSE_DURABLE_POLL_INTERVAL_SECONDS", 0.05),
            patch.object(main_module, "SSE_PING_INTERVAL_SECONDS", 0.05),
            patch.object(main_module, "SSE_RECONCILE_TIMEOUT_SECONDS", 0.1),
        ):
            serving = asyncio.create_task(app(scope, receive, send))
            try:
                # Wait until the stream has subscribed and is inside the hung poll.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5.0
                while run_id_str not in run_event_hub._subscribers:
                    self.assertLess(loop.time(), deadline, "the stream never subscribed")
                    await asyncio.sleep(0.01)
                # The poll deadline has to have fired and reached the hanging read
                # before the client leaves, otherwise this asserts nothing about a
                # stalled reconcile.
                deadline = loop.time() + 5.0
                while not entered:
                    self.assertLess(
                        loop.time(),
                        deadline,
                        "the stream never entered the hanging cursor read",
                    )
                    await asyncio.sleep(0.01)
                disconnect_now.set()
                await asyncio.wait_for(serving, timeout=5.0)
            finally:
                if not serving.done():
                    serving.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await serving

        self.assertEqual(
            entered,
            [_SSE_STEPS_AFTER_CURSOR_SQL],
            "the disconnect must have interrupted the bounded cursor read",
        )
        self.assertTrue(
            any(b"status_change" in chunk for chunk in body),
            f"expected the replayed status_change in the response body, got {body!r}",
        )
        self.assertNotIn(
            run_id_str,
            run_event_hub._subscribers,
            "the subscription must be released on disconnect even with a reconcile in flight",
        )