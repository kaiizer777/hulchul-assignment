"""
backend/tests/test_issue61_step_identity.py
A durable agent_steps row and the live SSE event that announces it must carry
the same step_id.

backend/main.py delivers a step twice when they disagree: the merged stream
dedupes strictly on step_id (emitted_step_ids over _SSE_PERSISTED_STEP_EVENT_TYPES),
so a live event without a step_id is always delivered and its row is delivered
again by the cursor poll. The frontend then renders two cards, and for a
terminal session_lost the duplicate re-fires the frontend's terminal branch.

These tests drive the real ReActAgent loop and the real persistence layer over
an in-memory agent_steps table, so the id under assertion is the id the insert
actually returned.
"""

import json
import unittest
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from backend.agent import ReActAgent
from backend.tools import PlaywrightTools


class _FakeAcquire:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeConn":
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    """asyncpg-shaped connection over an in-memory agent_steps/agent_runs pair."""

    def __init__(self, pool: "_FakeStepPool") -> None:
        self._pool = pool

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Record an agent_steps insert and hand back the id it was written under."""
        flat = " ".join(query.split())
        if flat.startswith("SELECT count(*) FROM agent_steps"):
            return len(self._pool.rows)
        if flat.startswith("SELECT status FROM agent_runs"):
            return self._pool.runs.get(args[0])
        if flat.startswith("INSERT INTO agent_steps (step_id"):
            # persist_step with an explicit step_id
            step_id, run_id, action, result, screenshot_b64 = args[0], args[1], args[2], args[3], args[4]
        elif flat.startswith("INSERT INTO agent_steps (run_id"):
            # persist_step minting the id server-side
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
        return step_id

    async def execute(self, query: str, *args: Any) -> str:
        """Serve the run-status writes and screenshot attachment updates."""
        flat = " ".join(query.split())
        if "INSERT INTO agent_runs" in flat:
            self._pool.runs.setdefault(args[0], "running")
            return "INSERT 1"
        if flat.startswith("UPDATE agent_runs"):
            self._pool.runs[args[1]] = args[0]
            return "UPDATE 1"
        if flat.startswith("UPDATE agent_steps SET screenshot_b64"):
            row = self._pool.rows[args[1]]
            row["screenshot_b64"] = args[0]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute query: {flat}")


class _FakeStepPool:
    def __init__(self) -> None:
        self.rows: Dict[uuid.UUID, Dict[str, Any]] = {}
        self.runs: Dict[uuid.UUID, str] = {}
        self.insert_counts: Dict[str, int] = {}
        self.conn = _FakeConn(self)

    def acquire(self) -> _FakeAcquire:
        """Return the asyncpg acquire() context manager shape."""
        return _FakeAcquire(self.conn)

    def rows_for(self, action: str) -> List[Dict[str, Any]]:
        """Every persisted row carrying this action."""
        return [row for row in self.rows.values() if row["action"] == action]


def _mock_redis() -> MagicMock:
    """Unconfigured Redis so the run skips pause and resume handling."""
    redis = MagicMock()
    redis.is_configured = False
    redis.set_session_state = AsyncMock(return_value=True)
    redis.get_pause_flag = AsyncMock(return_value=False)
    return redis


def _mock_tools() -> MagicMock:
    """Tool double whose every dispatch succeeds, so the loop reaches the emits."""
    tools = MagicMock(spec=PlaywrightTools)
    tools.page = MagicMock()
    tools.page.is_closed = MagicMock(return_value=False)
    tools.set_run_id = MagicMock()
    tools.set_page = MagicMock()
    tools.read_page = AsyncMock(
        return_value={"success": True, "snapshot": "- button 'Create Invoice'", "url": "http://x", "title": "T"}
    )
    tools.execute = AsyncMock(return_value={"success": True, "url": "http://x", "status": 200})
    tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
    return tools


def _session_lost_snapshot() -> Dict[str, Any]:
    """A read_page result the loop must classify as a lost browser session."""
    return {
        "success": False,
        "error": "Target page, context or browser has been closed",
        "session_lost": True,
    }


def _groq_tool_call(name: str, arguments: Dict[str, Any]) -> MagicMock:
    """One Groq completion carrying a single tool call."""
    message = MagicMock()
    call = MagicMock()
    call.id = f"call_{name}"
    call.function.name = name
    call.function.arguments = json.dumps(arguments)
    message.tool_calls = [call]
    message.content = None
    return MagicMock(choices=[MagicMock(message=message)])


def _groq_done(text: str = "Done.") -> MagicMock:
    """One Groq completion with no tool calls, which ends the loop."""
    message = MagicMock()
    message.tool_calls = None
    message.content = text
    return MagicMock(choices=[MagicMock(message=message)])


def _groq(responses: List[Any]) -> MagicMock:
    """A Groq double replaying `responses` in order."""
    completions = MagicMock()
    completions.create = AsyncMock(side_effect=responses)
    groq = MagicMock()
    groq.chat = MagicMock(completions=completions)
    return groq


def _agent(
    pool: _FakeStepPool,
    tools: Any,
    groq: Any,
    events: List[Dict[str, Any]],
    max_iterations: int = 5,
    reconnect: Any = None,
) -> ReActAgent:
    """A runnable agent whose emitted events are collected into `events`."""

    async def _on_event(event: Dict[str, Any]) -> None:
        """Append one emitted event."""
        events.append(event)

    return ReActAgent(
        run_id=str(uuid.uuid4()),
        tools=tools,
        pool=pool,  # type: ignore[arg-type]
        groq_client=groq,
        redis_client=_mock_redis(),
        max_iterations=max_iterations,
        on_event=_on_event,
        reconnect=reconnect,
    )


class TestStepIdentityOnFailurePaths(unittest.IsolatedAsyncioTestCase):
    """Every emit_event that follows a persist_step must carry that row's step_id."""

    async def test_llm_think_failure_event_carries_the_persisted_step_id(self) -> None:
        """A failed llm_think step_failed must name the row persist_step wrote."""
        pool = _FakeStepPool()
        events: List[Dict[str, Any]] = []
        tools = _mock_tools()

        # A list side_effect is never awaited by AsyncMock, so the 503 has to
        # come from a callable that raises on the first call and lets the second
        # one through, or the run would never take the failure path at all.
        calls = {"n": 0}

        async def _create(*args: Any, **kwargs: Any) -> Any:
            """Fail the first completion with a 503, then let the next one through."""
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("groq upstream 503")
            return _groq_done()

        completions = MagicMock()
        completions.create = AsyncMock(side_effect=_create)
        groq = MagicMock()
        groq.chat = MagicMock(completions=completions)

        agent = _agent(pool, tools, groq, events)
        result = await agent.run(goal="Process all pending invoices")

        self.assertEqual(result["status"], "completed")
        llm_rows = pool.rows_for("llm_think")
        self.assertEqual(len(llm_rows), 1, "the failed llm_think step must leave exactly one row")

        failures = [e for e in events if e["type"] == "step_failed" and e.get("action") == "llm_think"]
        self.assertEqual(len(failures), 1, f"expected one llm_think step_failed, got {failures}")
        emitted_step_id: Optional[str] = failures[0].get("step_id")
        self.assertIsNotNone(
            emitted_step_id,
            "step_failed without a step_id cannot be correlated with its row, so the poll delivers it again",
        )
        self.assertEqual(
            emitted_step_id,
            str(llm_rows[0]["step_id"]),
            "the live event must carry the id of the row it persisted",
        )

    async def test_terminal_session_lost_event_carries_the_persisted_step_id(self) -> None:
        """The terminal session_lost must name the row persist_step wrote.

        The durable row replays as a frame with terminal/reattached set, so a
        second delivery of the same step re-runs the frontend's terminal branch.
        """
        pool = _FakeStepPool()
        events: List[Dict[str, Any]] = []
        tools = _mock_tools()
        tools.read_page = AsyncMock(return_value=_session_lost_snapshot())

        agent = _agent(pool, tools, _groq([]), events, max_iterations=3, reconnect=None)
        result = await agent.run(goal="Process all pending invoices")

        self.assertEqual(result["status"], "session_lost")
        lost_rows = pool.rows_for("session_lost")
        self.assertEqual(len(lost_rows), 1, "the terminal session_lost must leave exactly one row")

        terminal = [e for e in events if e["type"] == "session_lost" and e.get("terminal") is True]
        self.assertEqual(len(terminal), 1, f"expected one terminal session_lost, got {events}")
        self.assertIsNotNone(
            terminal[0].get("step_id"),
            "a terminal session_lost without a step_id is delivered again by the cursor poll",
        )
        self.assertEqual(
            terminal[0]["step_id"],
            str(lost_rows[0]["step_id"]),
            "the live event must carry the id of the row it persisted",
        )

    async def test_reattaching_session_lost_stays_unidentified(self) -> None:
        """The reattach attempt event has no durable row, so it must stay id-free.

        _try_reattach_session persists nothing. A step_id here would name a row
        that does not exist, and the cursor poll has nothing to correlate it
        with, so there is nothing to fix and nothing to assert beyond that.
        """
        pool = _FakeStepPool()
        events: List[Dict[str, Any]] = []
        tools = _mock_tools()
        tools.read_page = AsyncMock(
            side_effect=[
                _session_lost_snapshot(),
                {"success": True, "snapshot": "- button 'A'", "url": "http://x", "title": "T"},
            ]
        )

        async def _reconnect() -> Any:
            """Hand back a fresh open page, as the CDP reconnect callback would."""
            page = MagicMock()
            page.is_closed = MagicMock(return_value=False)
            return page

        agent = _agent(
            pool,
            tools,
            _groq([_groq_tool_call("navigate", {"url": "/invoices"}), _groq_done()]),
            events,
            reconnect=_reconnect,
        )
        await agent.run(goal="Process all pending invoices")

        reattaching = [e for e in events if e["type"] == "session_lost" and e.get("reattaching") is True]
        self.assertEqual(len(reattaching), 1, f"expected one reattaching session_lost, got {events}")
        self.assertIsNone(reattaching[0].get("step_id"))
        self.assertEqual(pool.rows_for("session_lost"), [])
