"""
Regression coverage for issue #33: SSE live events must be delivered durably.

`agent_steps` is the authoritative live-event source for
`GET /agent/runs/{run_id}/stream`. History playback seeds a `(timestamp, step_id)`
cursor and the stream tails that cursor every time the in-process
`RunEventHub` queue goes quiet, so a step committed by a *different* Lambda
execution environment -- which the local hub never saw -- still reaches an
already-open stream.

`RunEventHub` stays as the low-latency warm path; the cursor tail is what makes
delivery correct regardless of which instance produced the step.

Frames are read straight off `EventSourceResponse.body_iterator`. httpx's
ASGITransport awaits the whole application before returning, so an endless SSE
stream never surfaces chunks through the HTTP client; the HTTP-level route is
covered separately in `test_00_...` with the same auth helper the rest of the
suite uses.
"""

import asyncio
import contextlib
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from backend.auth import Session
from backend.main import app, run_event_hub, stream_agent_run_endpoint

T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def _normalize(sql: str) -> str:
    """Collapse SQL whitespace so assertions do not depend on triple-quote layout."""
    return " ".join(sql.split())


def _step_row(
    step_id: str,
    timestamp: datetime,
    action: str = "navigate",
    result: str = "ok",
    screenshot_b64: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one `agent_steps` row in the shape asyncpg returns it."""
    return {
        "step_id": uuid.UUID(step_id),
        "action": action,
        "result": result,
        "screenshot_b64": screenshot_b64,
        "timestamp": timestamp,
    }


def _order_key(row: Dict[str, Any]) -> Tuple[datetime, str]:
    """
    Postgres btree order for `(timestamp, uuid)`.

    Python `uuid.UUID` is not orderable, and Postgres orders `uuid` by its 128-bit
    value -- which is exactly the lexicographic order of the canonical hyphenated
    string form, since every component is fixed-width lowercase hex.
    """
    return (row["timestamp"], str(row["step_id"]))


class _Statement:
    """One SQL statement the SSE generator issued, with its bound parameters."""

    __slots__ = ("sql", "args")

    def __init__(self, sql: str, args: Tuple[Any, ...]) -> None:
        self.sql = sql
        self.args = args

    @property
    def is_step_select(self) -> bool:
        return "FROM agent_steps" in self.sql

    @property
    def is_cursor_tail(self) -> bool:
        return "(timestamp, step_id) > ($2, $3)" in self.sql

    def __repr__(self) -> str:
        return f"_Statement(sql={self.sql!r}, args={self.args!r})"


class _FakeStreamConn:
    """
    Serves the `agent_runs` / `agent_steps` reads the SSE generator performs and
    records every statement it received.

    The cursor predicate and the ordering are enforced, not merely tolerated: a
    generator that dropped `(timestamp, step_id) > ($2, $3)` or the
    `step_id` tiebreak raises here, so a regression in either fails loudly
    instead of silently reading the wrong window.
    """

    def __init__(self, db: "_FakeStreamDB") -> None:
        self._db = db

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self._db.statements.append(_Statement(_normalize(query), args))
        if "FROM agent_runs" in _normalize(query):
            return self._db.run_row
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        normalized = _normalize(query)
        self._db.statements.append(_Statement(normalized, args))

        if "FROM agent_steps" not in normalized:
            raise AssertionError(f"unexpected fetch: {query}")
        if "ORDER BY timestamp ASC, step_id ASC" not in normalized:
            raise AssertionError(
                f"agent_steps read must pin the cursor ordering, got: {normalized}"
            )

        if self._db.step_read_failures > 0:
            self._db.step_read_failures -= 1
            raise RuntimeError("agent_steps read failed")

        rows = list(self._db.rows)
        if self._db.strict_cursor and "(timestamp, step_id) > ($2, $3)" in normalized:
            if len(args) != 3:
                raise AssertionError(
                    "the cursor tail must bind (run_id, timestamp, step_id); "
                    f"got {len(args)} parameters"
                )
            cursor = (args[1], str(args[2]))
            rows = [r for r in rows if _order_key(r) > cursor]
        elif "(timestamp, step_id) > ($2, $3)" in normalized and len(args) != 3:
            raise AssertionError(
                "the cursor tail must bind (run_id, timestamp, step_id); "
                f"got {len(args)} parameters"
            )

        rows.sort(key=_order_key)
        return rows


class _FakeStreamPool:
    def __init__(self, db: "_FakeStreamDB") -> None:
        self._db = db

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _FakeStreamConn(self._db)


class _FakeStreamDB:
    """
    In-memory stand-in for the durable run/step tables.

    `rows` is mutated mid-test to model a step committed *after* the stream
    opened. `strict_cursor=False` disables window filtering on the tail so a
    faulty read can be injected on purpose; it is fault injection, never the
    expected database behaviour.
    """

    def __init__(
        self,
        rows: Optional[Sequence[Dict[str, Any]]] = None,
        run_row: Any = None,
        strict_cursor: bool = True,
    ) -> None:
        self.rows: List[Dict[str, Any]] = list(rows or [])
        self.run_row = run_row
        self.strict_cursor = strict_cursor
        self.step_read_failures = 0
        self.statements: List[_Statement] = []

    async def get_pool(self) -> _FakeStreamPool:
        return _FakeStreamPool(self)

    @property
    def step_statements(self) -> List[_Statement]:
        return [s for s in self.statements if s.is_step_select]

    @property
    def tail_statements(self) -> List[_Statement]:
        return [s for s in self.statements if s.is_cursor_tail]


class _FastQueueTimeoutAsyncio:
    """
    Stands in for the `asyncio` module inside `backend.main` for the duration of
    a test so the SSE loop's 15s `wait_for(queue.get())` expires immediately.

    Patching the module attribute rather than `asyncio.wait_for` itself keeps the
    test's own safety timeouts real: `backend.db` and the test body import
    `asyncio` independently and are unaffected.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    @staticmethod
    async def wait_for(awaitable: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        if timeout is not None:
            return await asyncio.wait_for(awaitable, timeout=0.01)
        return await asyncio.wait_for(awaitable, **kwargs)


@contextlib.contextmanager
def _stream_env(db: _FakeStreamDB):
    """Patch only what this stream test touches; every patch is function-local."""
    with (
        patch("backend.main.get_db_pool", new=db.get_pool),
        patch("backend.db.get_db_pool", new=db.get_pool),
        patch("backend.main.asyncio", new=_FastQueueTimeoutAsyncio()),
    ):
        yield


def _session() -> Session:
    return Session(sub="operator", exp=9999999999)


async def _next_frame(frames: Any) -> Tuple[str, Dict[str, Any]]:
    """Pull one SSE frame, returning its event name and decoded payload."""
    frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
    return frame["event"], json.loads(frame["data"])


class _RawSSEStream:
    """
    Drives the ASGI app directly so an endless SSE response can be inspected and
    then shut down cleanly.

    httpx's ASGITransport awaits the whole application before returning, so it can
    never report `http.response.start` for a stream that does not end. Abandoning
    one on a timeout is worse than useless: Starlette's StreamingResponse does not
    close its body iterator when the surrounding request is cancelled, which leaves
    a hot generator and its hub subscription alive for the rest of the process.
    Sending a real `http.disconnect` lets sse_starlette cancel the task group and
    run the generator's `finally`.
    """

    def __init__(self, path: str, cookie: str) -> None:
        self._path = path
        self._cookie = cookie
        self._disconnect = asyncio.Event()
        self._started = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.status_code: Optional[int] = None
        self.headers: List[Tuple[bytes, bytes]] = []

    async def _receive(self) -> Dict[str, Any]:
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status_code = message["status"]
            self.headers = list(message.get("headers") or [])
            self._started.set()

    async def _run(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self._path,
            "raw_path": self._path.encode("utf-8"),
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"test"),
                (b"cookie", self._cookie.encode("utf-8")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }
        await app(scope, self._receive, self._send)

    async def open(self) -> None:
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._started.wait(), timeout=5.0)

    async def close(self) -> None:
        self._disconnect.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5.0)

    def header(self, name: str) -> str:
        for key, value in self.headers:
            if key.decode("latin-1").lower() == name.lower():
                return value.decode("latin-1")
        return ""


class TestIssue33StreamCursor(unittest.IsolatedAsyncioTestCase):
    """
    Issue #33: live SSE events must not depend on an in-process hub that publishes
    before any subscriber exists.

    These are `unittest.IsolatedAsyncioTestCase` methods on purpose: the repo has
    no pytest config and no asyncio auto-mode, so a bare `async def test_` would be
    collected and silently skipped instead of run.
    """

    async def asyncSetUp(self) -> None:
        run_event_hub._subscribers.clear()
        self.run_id = str(uuid.uuid4())

    async def asyncTearDown(self) -> None:
        run_event_hub._subscribers.clear()

    async def test_00_stream_route_rejects_an_unauthenticated_caller(self) -> None:
        """
        The stream route is behind `require_session`.

        Observable through httpx only because the rejection short-circuits before
        any streaming begins, so the ASGI application completes and returns.
        """
        db = _FakeStreamDB()

        with _stream_env(db):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.get(f"/agent/runs/{self.run_id}/stream")

        self.assertEqual(response.status_code, 401)

    async def test_00_stream_route_negotiates_event_stream_for_a_real_session(self) -> None:
        """
        With a minted session the route answers 200 / text/event-stream and, on a
        genuine client disconnect, releases its hub subscription.
        """
        from conftest import issue_test_session

        db = _FakeStreamDB()
        stream = _RawSSEStream(f"/agent/runs/{self.run_id}/stream", issue_test_session())

        with _stream_env(db):
            await stream.open()
            try:
                self.assertEqual(stream.status_code, 200)
                self.assertIn("text/event-stream", stream.header("content-type"))
                # The route subscribed before it ever yielded a frame.
                self.assertIn(self.run_id, run_event_hub._subscribers)
            finally:
                await stream.close()

        self.assertNotIn(
            self.run_id,
            run_event_hub._subscribers,
            "a real client disconnect must release the hub subscription",
        )

    async def test_live_step_committed_after_stream_opened_is_delivered(self) -> None:
        """A step persisted after the stream opened reaches the open stream."""
        db = _FakeStreamDB()
        late_step = str(uuid.uuid4())

        with _stream_env(db):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                # Nothing to replay, so the first frame is the keep-alive ping.
                name, payload = await _next_frame(frames)
                self.assertEqual(name, "ping")
                self.assertEqual(payload["run_id"], self.run_id)

                db.rows.append(_step_row(late_step, T0))

                name, payload = await _next_frame(frames)
                self.assertEqual(name, "step_complete")
                self.assertEqual(payload["step_id"], late_step)
                self.assertEqual(payload["step_index"], 1)
                self.assertEqual(payload["run_id"], self.run_id)
                self.assertEqual(payload["timestamp"], T0.isoformat())
            finally:
                await frames.aclose()

    async def test_history_to_tail_handoff_emits_each_step_id_exactly_once(self) -> None:
        """
        History, the hub, and the tail can each legitimately carry the same step.

        The agent commits a step before emitting its live event, so a step can be
        committed before the history read (and therefore replayed) *and* published
        to the hub afterwards. Reverting the per-connection dedupe makes the hub
        frame below reach the client a second time and fails this test.
        """
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        third = str(uuid.uuid4())
        db = _FakeStreamDB(
            rows=[
                _step_row(first, T0),
                _step_row(second, T0 + timedelta(seconds=1)),
            ],
            # Fault injection: the tail ignores its window and replays the run, so
            # the dedupe is exercised by construction rather than by luck.
            strict_cursor=False,
        )

        with _stream_env(db):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                delivered: List[str] = []
                replayed_indices: List[int] = []
                for _ in range(2):
                    name, payload = await _next_frame(frames)
                    self.assertIn(name, ("step_complete", "step_failed"))
                    delivered.append(payload["step_id"])
                    replayed_indices.append(payload["step_index"])
                self.assertEqual(delivered, [first, second])
                self.assertEqual(replayed_indices, [1, 2])

                # Force the generator past history into the live loop. This frame is
                # the keep-alive the cursor tail produces when it has nothing new,
                # and the subscription assertion is the barrier that makes the
                # publish below deterministic: the hub is subscribed only after
                # history replay, so publishing earlier would be a silent drop and
                # this test would prove nothing.
                name, _ = await _next_frame(frames)
                self.assertEqual(name, "ping")
                self.assertIn(self.run_id, run_event_hub._subscribers)

                db.rows.append(_step_row(third, T0 + timedelta(seconds=2)))
                # The agent's own publish for a step the history read already
                # replayed: the third delivery path.
                await run_event_hub.publish(
                    self.run_id,
                    {
                        "type": "step_complete",
                        "run_id": self.run_id,
                        "step_id": first,
                        "step_index": 1,
                        "action": "navigate",
                        "result": "ok",
                        "timestamp": T0.isoformat(),
                    },
                )

                after_handoff: List[str] = []
                indices: List[int] = []
                for _ in range(3):
                    name, payload = await _next_frame(frames)
                    if name == "ping":
                        continue
                    after_handoff.append(payload["step_id"])
                    indices.append(payload["step_index"])
            finally:
                await frames.aclose()

            self.assertEqual(
                after_handoff,
                [third],
                "only the genuinely new step may cross the history -> tail handoff",
            )
            # step_index keeps counting across the handoff instead of restarting.
            self.assertEqual(indices, [3])

        tally = delivered + after_handoff
        for step_id in (first, second, third):
            self.assertEqual(
                tally.count(step_id),
                1,
                f"step {step_id} was delivered {tally.count(step_id)} times",
            )
        self.assertEqual(len(set(tally)), len(tally))

    async def test_tail_query_pins_the_tuple_cursor_predicate_and_ordering(self) -> None:
        """The tail reads past `(timestamp, step_id)`, never past `timestamp` alone."""
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        third = str(uuid.uuid4())
        db = _FakeStreamDB(rows=[_step_row(first, T0), _step_row(second, T0 + timedelta(seconds=1))])

        with _stream_env(db):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                for _ in range(2):
                    await _next_frame(frames)

                db.rows.append(_step_row(third, T0 + timedelta(seconds=9)))
                name, payload = await _next_frame(frames)
                self.assertEqual(payload["step_id"], third)
            finally:
                await frames.aclose()

        history = db.step_statements[0]
        self.assertEqual(history.args, (uuid.UUID(self.run_id),))
        self.assertIn("ORDER BY timestamp ASC, step_id ASC", history.sql)
        self.assertNotIn("timestamp, step_id) >", history.sql)

        self.assertEqual(len(db.tail_statements), 1)
        tail = db.tail_statements[0]
        self.assertIn("(timestamp, step_id) > ($2, $3)", tail.sql)
        self.assertIn("ORDER BY timestamp ASC, step_id ASC", tail.sql)
        self.assertEqual(
            tail.args,
            (uuid.UUID(self.run_id), T0 + timedelta(seconds=1), uuid.UUID(second)),
            "the tail cursor must be the last row history replayed",
        )

    async def test_rows_sharing_a_timestamp_emit_once_each_in_step_id_order(self) -> None:
        """
        `timestamp` alone is not a total order: steps committed inside the same
        clock tick tie. `step_id` breaks the tie, so no step is replayed and a step
        landing in the tied window at the cursor boundary is still delivered.
        """
        # Derived, not drawn at random: the cursor after history is the step_id
        # Postgres orders last, and only a step_id above that one is inside the
        # tail's window. Random uuids land on either side of it, which would make
        # this test a coin flip rather than a regression test. The low two bits are
        # cleared so base + 2 cannot overflow a 128-bit uuid.
        base = uuid.uuid4().int >> 2 << 2
        low = str(uuid.UUID(int=base))
        high = str(uuid.UUID(int=base + 1))
        # Same timestamp as the cursor row, step_id just past it: the boundary tie
        # the ORDER BY tiebreak exists for.
        tied = str(uuid.UUID(int=base + 2))
        # Appended out of order on purpose: the read ordering, not the physical
        # insertion order, is what the client must observe.
        db = _FakeStreamDB(rows=[_step_row(high, T0), _step_row(low, T0)])

        with _stream_env(db):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                replayed: List[Tuple[str, int]] = []
                for _ in range(2):
                    _, payload = await _next_frame(frames)
                    replayed.append((payload["step_id"], payload["step_index"]))
                self.assertEqual(replayed, [(low, 1), (high, 2)])

                # A third step at the *same* timestamp as the cursor's row: it sits
                # inside the tied window and must still be delivered, exactly once.
                db.rows.append(_step_row(tied, T0))
                seen_tail: List[Tuple[str, int]] = []
                for _ in range(3):
                    name, payload = await _next_frame(frames)
                    if name == "ping":
                        continue
                    seen_tail.append((payload["step_id"], payload["step_index"]))
            finally:
                await frames.aclose()

        self.assertEqual(seen_tail, [(tied, 3)])

        all_ids = [s for s, _ in replayed] + [s for s, _ in seen_tail]
        self.assertEqual(len(all_ids), len(set(all_ids)), "no tied step may replay")

    async def test_durable_tail_delivers_when_the_hub_publish_is_a_noop(self) -> None:
        """
        Cross-instance repro: the step was committed on another Lambda execution
        environment, so `RunEventHub.publish` on this instance is never called.

        On current main this times out -- the queue is the only live source, so the
        stream sits on keep-alive pings forever and the step is lost.
        """
        db = _FakeStreamDB()
        cross_instance_step = str(uuid.uuid4())

        with _stream_env(db), patch(
            "backend.main.run_event_hub.publish", new=AsyncMock()
        ) as mock_publish:
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                name, _ = await _next_frame(frames)
                self.assertEqual(name, "ping")

                db.rows.append(_step_row(cross_instance_step, T0))

                name, payload = await _next_frame(frames)
                self.assertEqual(name, "step_complete")
                self.assertEqual(payload["step_id"], cross_instance_step)
                self.assertEqual(mock_publish.await_count, 0)
            finally:
                await frames.aclose()

    async def test_empty_run_emits_ping_and_attempts_the_tail_first(self) -> None:
        """
        The keep-alive must not mask a stalled cursor: the tail is polled *before*
        the ping is emitted, so an idle stream still makes progress on the table.
        """
        db = _FakeStreamDB()

        with _stream_env(db):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                name, payload = await _next_frame(frames)
            finally:
                await frames.aclose()

        self.assertEqual(name, "ping")
        self.assertEqual(payload["type"], "ping")
        self.assertEqual(payload["run_id"], self.run_id)
        self.assertGreaterEqual(
            len(db.step_statements),
            2,
            "history replay and the cursor tail must both run before the ping is sent",
        )
        # With no history rows there is no cursor, so the tail must read the run
        # from the start rather than invent a timestamp.
        self.assertEqual(db.step_statements[0].args, (uuid.UUID(self.run_id),))
        self.assertEqual(db.step_statements[1].args, (uuid.UUID(self.run_id),))

    async def test_history_read_failure_falls_through_to_the_tail(self) -> None:
        """A failing history read must not strand the run; the tail re-reads it all."""
        step_id = str(uuid.uuid4())
        db = _FakeStreamDB(rows=[_step_row(step_id, T0)])
        db.step_read_failures = 1  # only the history read fails

        with _stream_env(db):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            try:
                name, payload = await _next_frame(frames)
            finally:
                await frames.aclose()

        self.assertEqual(name, "step_complete")
        self.assertEqual(payload["step_id"], step_id)
        # No cursor could be seeded, so the step is the first of the run.
        self.assertEqual(payload["step_index"], 1)
        self.assertEqual(len(db.step_statements), 2)

    async def test_unsubscribe_runs_exactly_once_on_client_disconnect(self) -> None:
        """The hub subscription is per-connection and must be released once."""
        db = _FakeStreamDB()
        released: List[str] = []
        real_unsubscribe = run_event_hub.unsubscribe

        def _tracking_unsubscribe(run_id_arg: str, queue: asyncio.Queue) -> None:
            released.append(run_id_arg)
            real_unsubscribe(run_id_arg, queue)

        with _stream_env(db), patch.object(
            run_event_hub, "unsubscribe", new=_tracking_unsubscribe
        ):
            response = await stream_agent_run_endpoint(
                uuid.UUID(self.run_id), _session=_session()
            )
            frames = response.body_iterator
            await _next_frame(frames)
            self.assertIn(self.run_id, run_event_hub._subscribers)
            await frames.aclose()

        self.assertEqual(released, [self.run_id])
        self.assertNotIn(
            self.run_id,
            run_event_hub._subscribers,
            "the subscription must not survive the client disconnect",
        )