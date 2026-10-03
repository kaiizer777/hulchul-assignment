"""
backend/tests/test_issue61_screenshot_identity.py
A take_screenshot step must have one identity, not two.

tools.take_screenshot persists the row itself, so the agent loop skips its own
persist_step for that step (agent.py guards on the tool's `persisted` flag) while
the live step_complete still carries the loop's step_id. Before this fix the tool
inserted under a server-generated uuid, so the durable row and the live event
named different steps: the stream delivered the live frame, then the cursor poll
read the other row and delivered a second frame for the same screenshot, and the
frontend rendered a phantom card.

The end-to-end test opens the real SSE endpoint against an in-memory
agent_steps table and drives the real agent loop and tool dispatcher, so both
delivery paths compete for the same step exactly as they do in production.
"""

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import backend.main as main_module
from backend.agent import ReActAgent
from backend.auth import Session
from backend.main import (
    stream_agent_run_endpoint,
    _SSE_STEPS_ALL_SQL,
    _SSE_STEPS_AFTER_CURSOR_SQL,
    _SSE_PERSISTED_STEP_EVENT_TYPES,
)
from backend.tools import PlaywrightTools


class _FakeAcquire:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeConn":
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    """asyncpg-shaped connection over an in-memory agent_steps/agent_runs pair.

    Records every row write by action so a test can assert how many rows one
    step produced, and separately which step ids the screenshot tool wrote,
    because the tool and the agent loop reach the table through different
    statements.
    """

    def __init__(self, pool: "_FakeStepPool") -> None:
        self._pool = pool

    async def fetchval(self, query: str, *args: Any) -> Any:
        flat = " ".join(query.split())
        if flat.startswith("SELECT count(*) FROM agent_steps"):
            return len(self._pool.rows)
        if flat.startswith("SELECT status FROM agent_runs"):
            return self._pool.runs.get(args[0])
        if "ON CONFLICT (step_id) DO UPDATE SET screenshot_b64" in flat:
            # tools.take_screenshot persisting under the caller's step_id
            self._pool.tool_written_step_ids.append(args[0])
            return self._upsert_screenshot_row(
                step_id=args[0], run_id=args[1], action=args[2], result=args[3], screenshot_b64=args[4]
            )
        if flat.startswith("INSERT INTO agent_steps (step_id"):
            # agent.persist_step with an explicit step_id
            step_id, run_id, action, result, screenshot_b64 = args[0], args[1], args[2], args[3], args[4]
        elif flat.startswith("INSERT INTO agent_steps (run_id"):
            # agent.persist_step minting the id server-side
            step_id = uuid.uuid4()
            run_id, action, result, screenshot_b64 = args[0], args[1], args[2], args[3]
        else:
            raise AssertionError(f"unexpected fetchval query: {flat}")
        self._pool.rows[step_id] = {
            "step_id": step_id,
            "run_id": run_id,
            "action": action,
            "result": result,
            "screenshot_b64": screenshot_b64,
            "timestamp": datetime.now(timezone.utc),
        }
        self._pool.insert_counts[action] = self._pool.insert_counts.get(action, 0) + 1
        self._pool.write_counts[action] = self._pool.write_counts.get(action, 0) + 1
        return step_id

    def _upsert_screenshot_row(
        self,
        step_id: uuid.UUID,
        run_id: uuid.UUID,
        action: str,
        result: Optional[str],
        screenshot_b64: str,
    ) -> uuid.UUID:
        """INSERT ... ON CONFLICT (step_id) DO UPDATE, as tools.py writes it.

        An existing row keeps its action, result and timestamp and only gains the
        screenshot, which is what the failure path depends on: the failed result
        is what classifies the row as step_failed when the stream replays it.
        """
        existing = self._pool.rows.get(step_id)
        if existing is None:
            self._pool.rows[step_id] = {
                "step_id": step_id,
                "run_id": run_id,
                "action": action,
                "result": result if result is not None else "screenshot captured",
                "screenshot_b64": screenshot_b64,
                "timestamp": datetime.now(timezone.utc),
            }
        else:
            existing["result"] = result if result is not None else existing["result"]
            existing["screenshot_b64"] = screenshot_b64
        self._pool.write_counts[action] = self._pool.write_counts.get(action, 0) + 1
        return step_id

    async def execute(self, query: str, *args: Any) -> str:
        flat = " ".join(query.split())
        if "INSERT INTO agent_runs" in flat:
            self._pool.runs.setdefault(args[0], "running")
            return "INSERT 1"
        if flat.startswith("UPDATE agent_runs"):
            self._pool.runs[args[1]] = args[0]
            return "UPDATE 1"
        if flat.startswith("UPDATE agent_steps SET screenshot_b64"):
            # Both writers put the screenshot first and match on step_id last,
            # so the step id is the final argument whichever statement this is.
            # A write that matches no row writes nothing, exactly as in Postgres.
            row = self._pool.rows.get(args[-1])
            if row is None:
                return "UPDATE 0"
            row["screenshot_b64"] = args[0]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute query: {flat}")

    async def fetchrow(self, query: str, *args: Any) -> Any:
        flat = " ".join(query.split())
        if flat.startswith("SELECT run_id, goal, status, created_at FROM agent_runs"):
            run_id = args[0]
            if run_id not in self._pool.runs:
                return None
            return {
                "run_id": run_id,
                "goal": "Process all pending invoices",
                "status": self._pool.runs[run_id],
                "created_at": self._pool.created_at,
            }
        raise AssertionError(f"unexpected fetchrow query: {flat}")

    async def fetch(self, query: str, run_uuid: Any, *args: Any) -> List[Dict[str, Any]]:
        """Serve the two SSE step queries over the in-memory table."""
        if query not in (_SSE_STEPS_ALL_SQL, _SSE_STEPS_AFTER_CURSOR_SQL):
            raise AssertionError(f"unexpected agent_steps query: {' '.join(query.split())}")
        self._pool.step_reads += 1
        ordered = sorted(
            (r for r in self._pool.rows.values() if r["run_id"] == run_uuid),
            key=lambda r: (r["timestamp"], r["step_id"]),
        )
        if query == _SSE_STEPS_AFTER_CURSOR_SQL:
            cursor_timestamp, cursor_step_id = args
            return [
                r for r in ordered
                if (r["timestamp"], r["step_id"]) > (cursor_timestamp, cursor_step_id)
            ]
        return list(ordered)


class _FakeStepPool:
    def __init__(self) -> None:
        self.rows: Dict[uuid.UUID, Dict[str, Any]] = {}
        self.runs: Dict[uuid.UUID, str] = {}
        # agent.persist_step inserts, keyed by action: the loop's own rows.
        self.insert_counts: Dict[str, int] = {}
        # Every write of an agent_steps row, whoever made it.
        self.write_counts: Dict[str, int] = {}
        # Step ids tools.take_screenshot persisted under the caller's id.
        self.tool_written_step_ids: List[uuid.UUID] = []
        # Times the stream read agent_steps, over either SSE query.
        self.step_reads = 0
        self.created_at = datetime.now(timezone.utc)
        self.conn = _FakeConn(self)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)

    def rows_for(self, action: str) -> List[Dict[str, Any]]:
        return [row for row in self.rows.values() if row["action"] == action]


def _mock_redis() -> MagicMock:
    redis = MagicMock()
    redis.is_configured = False
    redis.set_session_state = AsyncMock(return_value=True)
    redis.get_pause_flag = AsyncMock(return_value=False)
    return redis


def _fake_page(screenshot_b64: str = "c2NyZWVuc2hvdA==") -> MagicMock:
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.screenshot = AsyncMock(return_value=b"png-bytes")
    return page


def _real_tools(pool: _FakeStepPool, run_id: str) -> PlaywrightTools:
    """The real tool dispatcher over a stubbed Playwright page and the fake pool.

    read_page is stubbed because the accessibility snapshot is Playwright
    surface; take_screenshot and the dispatcher are the code under test.
    """
    tools = PlaywrightTools(page=_fake_page(), run_id=run_id, pool=pool)  # type: ignore[arg-type]
    tools.read_page = AsyncMock(  # type: ignore[method-assign]
        return_value={"success": True, "snapshot": "- button 'Create Invoice'", "url": "http://x", "title": "T"}
    )
    return tools


def _groq(responses: List[Any]) -> MagicMock:
    completions = MagicMock()
    completions.create = AsyncMock(side_effect=responses)
    groq = MagicMock()
    groq.chat = MagicMock(completions=completions)
    return groq


def _groq_tool_call(name: str, arguments: Dict[str, Any]) -> MagicMock:
    message = MagicMock()
    call = MagicMock()
    call.id = f"call_{name}"
    call.function.name = name
    call.function.arguments = json.dumps(arguments)
    message.tool_calls = [call]
    message.content = None
    return MagicMock(choices=[MagicMock(message=message)])


def _groq_done(text: str = "All invoices processed.") -> MagicMock:
    message = MagicMock()
    message.tool_calls = None
    message.content = text
    return MagicMock(choices=[MagicMock(message=message)])


def _screenshot_run_groq() -> MagicMock:
    return _groq(
        [
            _groq_tool_call("take_screenshot", {}),
            _groq_done(),
        ]
    )


def _collect(events: List[Dict[str, Any]]) -> Any:
    async def _on_event(event: Dict[str, Any]) -> None:
        events.append(event)

    return _on_event


class TestTakeScreenshotStepIdentity(unittest.IsolatedAsyncioTestCase):
    async def _agent(self, pool: _FakeStepPool, run_id: str, tools: Any, groq: Any, max_iterations: int = 5) -> ReActAgent:
        agent = ReActAgent(
            run_id=run_id,
            tools=tools,
            pool=pool,  # type: ignore[arg-type]
            groq_client=groq,
            redis_client=_mock_redis(),
            max_iterations=max_iterations,
        )
        await agent.ensure_run_record("Process all pending invoices")
        return agent

    async def _open_stream(self, pool: _FakeStepPool, run_id: str) -> Any:
        """Open the real SSE endpoint against the in-memory table, poll interval collapsed."""

        async def fake_get_pool() -> _FakeStepPool:
            return pool

        for active in (
            patch("backend.main.get_db_pool", new=fake_get_pool),
            patch("backend.db.get_db_pool", new=fake_get_pool),
            patch.object(main_module, "SSE_DURABLE_POLL_INTERVAL_SECONDS", 0.05),
        ):
            active.start()
            self.addCleanup(active.stop)

        response = await stream_agent_run_endpoint(
            uuid.UUID(run_id),
            _session=Session(sub="operator", exp=9999999999),
        )
        return response.body_iterator

    async def test_screenshot_step_is_delivered_once_by_the_live_and_poll_paths(self) -> None:
        """One take_screenshot row and one live event, one frame on the stream.

        Both delivery paths see the step: the row is visible to the cursor poll
        and the live event to the hub queue, at the same time. Dedupe is keyed
        on step_id, so this only holds while the row and the event agree.
        """
        pool = _FakeStepPool()
        run_id = str(uuid.uuid4())
        tools = _real_tools(pool, run_id)
        agent = await self._agent(pool, run_id, tools, _screenshot_run_groq())

        frames = await self._open_stream(pool, run_id)
        payloads: List[Dict[str, Any]] = []
        try:
            # The replay delivers the run's status before the run starts.
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            payloads.append(json.loads(frame["data"]))
            self.assertEqual(payloads[0]["type"], "status_change")

            await agent.run(goal="Process all pending invoices")

            # Drain every queued live frame and every poll cycle over the rows
            # the run committed, so a duplicate has to show up before the read
            # times out.
            while True:
                try:
                    frame = await asyncio.wait_for(frames.__anext__(), timeout=0.4)
                except asyncio.TimeoutError:
                    break
                payloads.append(json.loads(frame["data"]))
        finally:
            await frames.aclose()

        # step_start is excluded on purpose: main.py keeps it out of the dedupe
        # and the frontend does not render it as a card, so only the persisted
        # step event types can duplicate each other.
        screenshot_frames = [
            p for p in payloads
            if p.get("action") == "take_screenshot"
            and p.get("type") in _SSE_PERSISTED_STEP_EVENT_TYPES
            and p.get("step_id")
        ]
        self.assertEqual(
            len(screenshot_frames),
            1,
            f"the screenshot step was delivered {len(screenshot_frames)} times: "
            f"{[(p['type'], p.get('step_id')) for p in screenshot_frames]}",
        )
        self.assertEqual(screenshot_frames[0]["type"], "step_complete")
        self.assertEqual(
            len(pool.tool_written_step_ids),
            1,
            "take_screenshot must persist the row itself, under the step id the loop minted",
        )
        self.assertEqual(
            screenshot_frames[0]["step_id"],
            str(pool.tool_written_step_ids[0]),
            "the live event must name the row the tool persisted",
        )
        self.assertEqual(
            [row["step_id"] for row in pool.rows_for("take_screenshot")],
            pool.tool_written_step_ids,
            "the durable screenshot row must be the one the live event names",
        )
        self.assertGreaterEqual(
            pool.step_reads,
            1,
            "the poll must have run over the row, or the duplicate could not have surfaced",
        )

    async def test_screenshot_step_inserts_exactly_one_row(self) -> None:
        """The loop's avoid-duplicate-row guard must keep holding.

        The tool writes the row and reports persisted: true, so the loop skips
        its own persist_step. Threading the step id through must not turn that
        into a second write, and the row that survives must be the one the
        step_complete names.
        """
        pool = _FakeStepPool()
        run_id = str(uuid.uuid4())
        tools = _real_tools(pool, run_id)
        events: List[Dict[str, Any]] = []
        agent = await self._agent(pool, run_id, tools, _screenshot_run_groq())
        agent.on_event = _collect(events)

        await agent.run(goal="Process all pending invoices")

        rows = pool.rows_for("take_screenshot")
        self.assertEqual(len(rows), 1, f"one screenshot step must leave one row, got {rows}")
        self.assertEqual(
            pool.write_counts.get("take_screenshot"),
            1,
            "the tool writes the row and the loop must not write a second one",
        )
        completions = [e for e in events if e["type"] == "step_complete" and e.get("action") == "take_screenshot"]
        self.assertEqual(len(completions), 1, f"expected one step_complete, got {events}")
        self.assertEqual(
            rows[0]["step_id"],
            uuid.UUID(completions[0]["step_id"]),
            "the surviving row must be the one the live event names",
        )
        self.assertIsNotNone(rows[0]["screenshot_b64"])

    async def test_failure_screenshot_attaches_to_the_failed_row_instead_of_inserting(self) -> None:
        """The direct take_screenshot call must still update the loop's failed row.

        The failure path persists the failed step first and then calls
        take_screenshot with that row's id. The upsert must attach the
        screenshot there and leave the failed result alone, since the durable
        row's result is what classifies it as step_failed on replay.
        """
        pool = _FakeStepPool()
        run_id = str(uuid.uuid4())
        tools = _real_tools(pool, run_id)
        # Only the Playwright click is stubbed; the dispatcher and the failure
        # path's own take_screenshot call are the real ones.
        tools.click = AsyncMock(  # type: ignore[method-assign]
            return_value={"success": False, "error": "Element '#nonexistent' not found"}
        )
        groq = _groq(
            [
                _groq_tool_call("click", {"selector": "#nonexistent"}),
                _groq_done(),
            ]
        )
        agent = await self._agent(pool, run_id, tools, groq)

        await agent.run(goal="Process all pending invoices")

        click_rows = pool.rows_for("click")
        self.assertEqual(len(click_rows), 1, f"one failed click must leave one row, got {click_rows}")
        self.assertIn("failed", click_rows[0]["result"] or "")
        self.assertIsNotNone(
            click_rows[0]["screenshot_b64"],
            "the diagnostic screenshot must be attached to the failed row",
        )
        self.assertIn(
            "failed",
            click_rows[0]["result"] or "",
            "attaching the screenshot must not overwrite the result that classifies the row",
        )
        self.assertEqual(pool.insert_counts.get("click"), 1)
