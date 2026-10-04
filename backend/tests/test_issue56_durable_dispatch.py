"""
Test suite for issue #56: durable dispatch of accepted agent runs.

Covers the run-lease primitives in ``backend.run_lease``, the lease claim inside
``ReActAgent.ensure_run_record``, the startup reconciliation hook in the FastAPI
lifespan, and the load-time invariant tying RUN_LEASE_SECONDS to
PAUSE_TIMEOUT_SECONDS.

No network, no AWS, no real Postgres: the pool is a stateful fake that models the
``agent_runs`` conditional-UPDATE guards (``status = 'running'``, lease expiry,
``owner_id`` scoping) so the tests assert the same thing Postgres would decide,
rather than asserting on mock call counts alone.
"""

import asyncio
import contextlib
import json
import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agent import ReActAgent
from backend.auth import Session
from backend.auth import require_session as _require_session_dep
from backend.config import settings, validate_run_lease_settings
from httpx import ASGITransport, AsyncClient
from backend.run_lease import (
    ACTIVE_RUN_STATUSES,
    ORPHANED_RUN_STATUS,
    ORPHANED_STEP_ACTION,
    TERMINAL_RUN_STATUSES,
    reconcile_orphaned_agent_runs,
    release_run_lease,
    renew_run_lease,
    run_lease_heartbeat,
)
from backend.tools import PlaywrightTools

# Guards that appear in the conditional statements this suite exercises. The fake
# connection below evaluates them, so a run that must not be touched cannot be
# touched even if a caller offered it as a candidate.
STATUS_GUARD = "status = 'running'"
LEASE_FREE_GUARD = "lease_expires_at IS NULL OR lease_expires_at < now()"
# Anchored on AND so it identifies the WHERE clause rather than any bare
# "owner_id = $2" appearing in a statement body.
OWNER_SCOPED_GUARD = "AND owner_id = $2"
STATUS_FENCE_GUARD = "AND owner_id = $3"
ACTIVE_STATUS_GUARD = "status = ANY($4::text[])"
UPSERT_LEASE_GUARD = "agent_runs.lease_expires_at IS NULL"
UPSERT_EXPIRED_GUARD = "agent_runs.lease_expires_at <= now()"
UPSERT_SAME_OWNER_GUARD = "agent_runs.owner_id = EXCLUDED.owner_id"
# The sweep's rollout guard: a row this lease scheme has never owned is not a candidate.
ATTEMPTED_GUARD = "AND attempt > 0"
OWNER_RECHECK_QUERY = "SELECT owner_id FROM agent_runs"
# The fallback terminal write in backend/main.py. Qualified with the table name so it
# cannot be confused with the unqualified owner fences above.
TERMINAL_OWNER_FENCE = "agent_runs.owner_id = $3"
TERMINAL_UNCLAIMED_FENCE = (
    "agent_runs.owner_id IS NULL OR agent_runs.lease_expires_at <= now()"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expired() -> datetime:
    return _now() - timedelta(seconds=60)


def _live() -> datetime:
    return _now() + timedelta(seconds=600)


class _FakeConn:
    """Connection stand-in that applies the real conditional-UPDATE semantics."""

    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    async def fetchrow(self, query: str, *args: Any) -> Optional[Dict[str, Any]]:
        self._pool.record(query, args)
        self._pool.maybe_fail(query)

        # The row-lock probe is gone: `pool.acquire()` is not a transaction, so such a
        # SELECT autocommits and drops its lock before the UPDATE could use it. Kept
        # here only so a reintroduced probe shows up as an unexpected extra statement.
        if "FOR UPDATE" in query:
            for row in self._pool.rows:
                if str(row["run_id"]) == str(args[0]):
                    return dict(row)
            return None

        # INSERT ... ON CONFLICT (run_id) DO UPDATE ... RETURNING: ensure_run_record.
        # Args are (run_id, goal, owner_id, lease_seconds) -- a different layout from
        # the claim. Checked before the plain UPDATE branch because this statement
        # also contains both "UPDATE" and "RETURNING".
        if "ON CONFLICT (run_id) DO UPDATE" in query:
            owner_id = args[2]
            lease_seconds = float(args[3])
            existing = [r for r in self._pool.rows if str(r["run_id"]) == str(args[0])]
            for row in existing:
                if not self._pool.upsert_guards_pass(row, query, owner_id):
                    # The conflict target exists but its WHERE clause refused the
                    # update. Postgres performs no insert here and RETURNING yields
                    # no row -- which is exactly the refusal signal ensure_run_record
                    # turns into "another live owner holds this run".
                    return None
                row["status"] = "running"
                row["owner_id"] = owner_id
                row["lease_expires_at"] = _now() + timedelta(seconds=lease_seconds)
                row["attempt"] = row.get("attempt", 0) + 1
                return dict(row)
            inserted = _row(status="running", run_id=str(args[0]))
            inserted["owner_id"] = owner_id
            inserted["lease_expires_at"] = _now() + timedelta(seconds=lease_seconds)
            inserted["attempt"] = 1
            self._pool.rows.append(inserted)
            return dict(inserted)

        # UPDATE ... RETURNING: the conditional claim.
        if "RETURNING" in query and "UPDATE" in query:
            lease_seconds = float(args[2]) if len(args) > 2 else self._pool.default_lease
            for row in self._pool.rows:
                if str(row["run_id"]) != str(args[0]):
                    continue
                if not self._pool.guards_pass(row, query, args):
                    continue
                row["owner_id"] = args[1]
                row["lease_expires_at"] = _now() + timedelta(seconds=lease_seconds)
                row["attempt"] = row.get("attempt", 0) + 1
                return dict(row)
            return None

        return None

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        self._pool.record(query, args)
        self._pool.maybe_fail(query)
        if "SELECT" in query and "agent_runs" in query:
            # Deliberately over-broad on the filter: returns every seeded row
            # regardless of the WHERE clause. That is what makes the CAS in
            # reconcile_orphaned_agent_runs the load-bearing protection -- if the
            # sweep ever lost its status guard, a paused or awaiting_approval run
            # would be failed by these rows.
            rows = [dict(r) for r in self._pool.rows]
            if "LIMIT $1" in query and args:
                # LIMIT is honoured because it is real database behaviour the batch
                # drain in reconcile_orphaned_agent_runs depends on, unlike the WHERE
                # clause which is deliberately not modelled so the CAS stays the thing
                # under test. Status is ordered ahead of the rest for the same reason:
                # Postgres filters before it limits, so without this a second batch
                # would keep re-reading the rows the first batch already reclaimed.
                # Non-running rows are still delivered once the running ones run out,
                # which is what keeps the CAS load-bearing.
                if STATUS_GUARD in query:
                    running = [r for r in rows if r["status"] == "running"]
                    others = [r for r in rows if r["status"] != "running"]
                    rows = (running + others)[: int(args[0])]
                else:
                    rows = rows[: int(args[0])]
            return rows
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Post-sweep ownership re-read that gates the Redis / hub mirrors."""
        self._pool.record(query, args)
        self._pool.maybe_fail(query)
        if OWNER_RECHECK_QUERY in query:
            for row in self._pool.rows:
                if str(row["run_id"]) == str(args[0]):
                    return row.get("owner_id")
            return None
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self._pool.record(query, args)
        self._pool.maybe_fail(query)

        if "INSERT INTO agent_steps" in query:
            self._pool.steps.append(
                {"run_id": str(args[0]), "action": args[1], "result": args[2]}
            )
            return "INSERT 0 1"

        if "UPDATE agent_runs" in query:
            # Three arg layouts reach here:
            #   update_run_status : SET status = $1 WHERE run_id = $2  -> (status, run_id)
            #   reconcile sweep   : SET status = $2 WHERE run_id = $1  -> (run_id, status)
            #   renew / release   : WHERE run_id = $1                  -> (run_id, ...)
            if "SET status = $1" in query:
                run_id_arg, status_value = args[1], args[0]
            elif "SET status = $2" in query:
                run_id_arg, status_value = args[0], args[1]
            else:
                run_id_arg, status_value = args[0], None

            affected = 0
            for row in self._pool.rows:
                if str(row["run_id"]) != str(run_id_arg):
                    continue
                if not self._pool.guards_pass(row, query, args):
                    continue
                affected += 1
                if status_value is not None:
                    row["status"] = status_value
                if "owner_id = NULL" in query:
                    row["owner_id"] = None
                    row["lease_expires_at"] = None
                if "lease_expires_at = now()" in query and len(args) > 2:
                    row["lease_expires_at"] = _now() + timedelta(seconds=float(args[2]))
            return f"UPDATE {affected}"

        return "UPDATE 0"


class _FakeAcquire:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._pool)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    """Stateful agent_runs/agent_steps fake: row lookups plus guard evaluation."""

    def __init__(
        self,
        rows: Optional[List[Dict[str, Any]]] = None,
        fail_on: Optional[List[str]] = None,
        default_lease: float = 900.0,
    ) -> None:
        self.rows: List[Dict[str, Any]] = rows or []
        self.steps: List[Dict[str, Any]] = []
        self.executed: List[Tuple[str, Tuple[Any, ...]]] = []
        self.fail_on: List[str] = fail_on or []
        self.default_lease = default_lease

    def acquire(self, timeout: Optional[float] = None) -> _FakeAcquire:
        # timeout is accepted because the real call site passes one:
        # pool.acquire(timeout=TERMINAL_WRITE_IO_TIMEOUT_SECONDS).
        return _FakeAcquire(self)

    def record(self, query: str, args: Tuple[Any, ...]) -> None:
        self.executed.append((query, args))

    def maybe_fail(self, query: str) -> None:
        for marker in self.fail_on:
            if marker in query:
                raise RuntimeError(f"injected failure for {marker!r}")

    def guards_pass(self, row: Dict[str, Any], query: str, args: Tuple[Any, ...]) -> bool:
        """Apply the conditional-UPDATE guards the query actually declares."""
        if STATUS_GUARD in query and row["status"] != "running":
            return False
        if LEASE_FREE_GUARD in query:
            lease = row.get("lease_expires_at")
            if lease is not None and lease >= _now():
                return False
        if ACTIVE_STATUS_GUARD in query:
            allowed = [str(s) for s in args[3]] if len(args) > 3 else []
            if row["status"] not in allowed:
                return False
        if STATUS_FENCE_GUARD in query and len(args) > 2:
            if row.get("owner_id") != args[2]:
                return False
        if OWNER_SCOPED_GUARD in query and len(args) > 1:
            if row.get("owner_id") != args[1]:
                return False
        if ATTEMPTED_GUARD in query and int(row.get("attempt") or 0) < 1:
            return False
        # Fallback terminal write, owner supplied: writable when the row is unowned
        # (nobody claimed it, so a cancellation before the claim still records
        # failed) or already ours. A different owner is refused.
        if TERMINAL_OWNER_FENCE in query and len(args) > 2:
            if row.get("owner_id") is not None and row.get("owner_id") != args[2]:
                return False
        # Fallback terminal write, no owner supplied: this request never claimed a
        # lease, so a row held under a live lease by anyone is refused. A NULL expiry
        # counts as refused rather than free: `lease_expires_at <= now()` against NULL
        # evaluates to NULL, which is not true, so Postgres takes neither branch.
        if TERMINAL_UNCLAIMED_FENCE in query:
            lease = row.get("lease_expires_at")
            if row.get("owner_id") is not None and (lease is None or lease >= _now()):
                return False
        return True

    def upsert_guards_pass(self, row: Dict[str, Any], query: str, owner_id: Any) -> bool:
        """Apply the ON CONFLICT DO UPDATE ... WHERE guards from ensure_run_record.

        Passing means the row may be taken over: either its lease is absent or
        already lapsed, or it is already ours.
        """
        lease = row.get("lease_expires_at")
        lease_free = lease is None
        if not lease_free and UPSERT_EXPIRED_GUARD in query:
            lease_free = lease < _now()
        if lease_free and UPSERT_LEASE_GUARD in query:
            return True
        if UPSERT_SAME_OWNER_GUARD in query and row.get("owner_id") == owner_id:
            return True
        return False

    def queries(self) -> List[str]:
        return [q for q, _ in self.executed]

    def find(self, marker: str) -> List[Tuple[str, Tuple[Any, ...]]]:
        return [(q, a) for q, a in self.executed if marker in q]

    def row(self, run_id: Any) -> Dict[str, Any]:
        for r in self.rows:
            if str(r["run_id"]) == str(run_id):
                return r
        raise AssertionError(f"no seeded row for run_id {run_id!r}")


def _row(
    status: str = "running",
    owner_id: Optional[str] = None,
    lease_expires_at: Optional[datetime] = None,
    attempt: int = 0,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "goal": "test",
        "status": status,
        "owner_id": owner_id,
        "lease_expires_at": lease_expires_at,
        "attempt": attempt,
    }


class _FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.states: Dict[str, Dict[str, Any]] = {}

    async def set_session_state(self, run_id: str, state: Dict[str, Any], ttl_seconds: int = 86400) -> bool:
        if self.fail:
            raise RuntimeError("redis down")
        self.states[run_id] = state
        return True


class _FakeHub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: List[Tuple[str, Dict[str, Any]]] = []

    async def publish(self, run_id: str, event: Dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("hub down")
        self.published.append((run_id, event))


def _agent(pool: Any, run_id: str) -> ReActAgent:
    return ReActAgent(run_id=run_id, tools=MagicMock(spec=PlaywrightTools), pool=pool)


def _unconfigured_redis() -> MagicMock:
    """Redis stub with no Upstash configuration, so run() skips the pause/resume branch."""
    redis = MagicMock()
    redis.is_configured = False
    redis.set_session_state = AsyncMock(return_value=True)
    redis.get_pause_flag = AsyncMock(return_value=False)
    return redis


def _groq_returning(messages: List[Any]) -> MagicMock:
    """Groq stub replaying one assistant message per planned iteration."""
    responses = [MagicMock(choices=[MagicMock(message=m)]) for m in messages]
    mock_completions = MagicMock()
    mock_completions.create = AsyncMock(side_effect=responses)
    mock_groq = MagicMock()
    mock_groq.chat = MagicMock(completions=mock_completions)
    return mock_groq


def _tool_call_message(call_id: str, name: str, args: Dict[str, Any]) -> MagicMock:
    """Assistant message carrying a single tool call."""
    msg = MagicMock()
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg.tool_calls = [tc]
    msg.content = None
    return msg


def _text_message(text: str) -> MagicMock:
    """Assistant message with prose only and no tool call."""
    msg = MagicMock()
    msg.tool_calls = None
    msg.content = text
    return msg


_RENEW_QUERY_MARKER = "lease_expires_at = now()"


async def _await_renewals(pool: "_FakePool", minimum: int) -> bool:
    """Yield to the loop until the heartbeat has issued at least `minimum` renewals.

    Returns False if it stops early, which is how a heartbeat that gave up on a paused
    run is distinguished from one that is merely slow. Counting observed statements
    instead of sleeping a fixed interval keeps the assertion on the behaviour rather
    than on the machine's timer resolution.
    """
    while True:
        if len(pool.find(_RENEW_QUERY_MARKER)) >= minimum:
            return True
        await asyncio.sleep(0.005)


async def _await_call_count(state: Dict[str, int], minimum: int) -> bool:
    """Yield until a counter reaches `minimum`.

    Bounded by the caller's wait_for: a heartbeat that stops early freezes the counter,
    and that has to surface as a failure rather than as a hung test.
    """
    while state["n"] < minimum:
        await asyncio.sleep(0.005)
    return True


class TestRunLeaseRenewRelease(unittest.IsolatedAsyncioTestCase):
    async def test_01_renew_extends_only_for_the_current_owner(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])

        self.assertTrue(await renew_run_lease(pool, run_id, "owner-a", 900.0))
        self.assertFalse(await renew_run_lease(pool, run_id, "owner-b", 900.0))
        self.assertIn(OWNER_SCOPED_GUARD, pool.find("UPDATE agent_runs")[0][0])

    async def test_02_renew_fails_once_the_run_is_terminal(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="done", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])

        self.assertFalse(await renew_run_lease(pool, run_id, "owner-a", 900.0))

    async def test_05_renew_keeps_the_lease_alive_while_paused_or_awaiting_approval(self) -> None:
        """Regression: the heartbeat must survive a human wait.

        Scoping renewal to status = 'running' made the first heartbeat tick during a
        pause or approval gate renew zero rows, which the heartbeat read as "lease
        lost" and stopped for good. The resumed run then executed with a dead lease,
        so a duplicate request could claim it and double-run it.
        """
        for status in ("running", "paused", "awaiting_approval"):
            with self.subTest(status=status):
                run_id = str(uuid.uuid4())
                pool = _FakePool(
                    [_row(status=status, owner_id="owner-a", lease_expires_at=_expired(), run_id=run_id)]
                )

                self.assertTrue(
                    await renew_run_lease(pool, run_id, "owner-a", 900.0),
                    f"a run in {status!r} is still executing and must keep its lease",
                )
                self.assertEqual(pool.row(run_id)["owner_id"], "owner-a")

    async def test_06_active_statuses_exclude_terminal_ones(self) -> None:
        """A terminal run must never hold a live lease, or it stops being resumable."""
        self.assertFalse(ACTIVE_RUN_STATUSES & TERMINAL_RUN_STATUSES)
        self.assertIn("running", ACTIVE_RUN_STATUSES)
        self.assertIn("paused", ACTIVE_RUN_STATUSES)
        self.assertIn("awaiting_approval", ACTIVE_RUN_STATUSES)

    async def test_07_heartbeat_survives_a_pause(self) -> None:
        """End-to-end version of test_05: the heartbeat task itself keeps renewing."""
        for status in ("paused", "awaiting_approval"):
            with self.subTest(status=status):
                run_id = str(uuid.uuid4())
                pool = _FakePool(
                    [_row(status=status, owner_id="owner-a", lease_expires_at=_expired(), run_id=run_id)]
                )

                task = asyncio.create_task(run_lease_heartbeat(pool, run_id, "owner-a", 0.01, 900.0))
                try:
                    # Waiting on the observed renewal rather than on a wall-clock
                    # sleep: an `await asyncio.sleep(0.08)` assertion passed or failed
                    # depending on how long the interpreter took to get through its
                    # first iterations, which made this test fail on a cold run while
                    # the heartbeat was behaving correctly. The bounded loop keeps the
                    # failure mode honest -- a heartbeat that stops on a pause never
                    # reaches the timeout.
                    renewed = await asyncio.wait_for(
                        _await_renewals(pool, minimum=2), timeout=5.0
                    )
                    self.assertTrue(renewed)
                    self.assertFalse(
                        task.done(), f"the heartbeat must not treat {status} as a lost lease"
                    )
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                self.assertGreater(pool.row(run_id)["lease_expires_at"], _now())

    async def test_08_two_concurrent_claims_yield_exactly_one_winner(self) -> None:
        """The dispatch-level race: two agents racing the same run_id.

        This is the statement run() actually depends on, and it asserts that exactly
        one execution is ACCEPTED rather than merely that one claim statement won.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", run_id=run_id)])
        first = _agent(pool, run_id)
        second = _agent(pool, run_id)

        accepted = await asyncio.gather(
            first.ensure_run_record("goal"), second.ensure_run_record("goal")
        )

        self.assertEqual(
            sorted(accepted), [False, True], "two dispatches of one run_id must not both execute"
        )
        self.assertEqual(pool.row(run_id)["attempt"], 1)
        self.assertIn(pool.row(run_id)["owner_id"], {first._run_owner_id, second._run_owner_id})

    async def test_03_release_is_owner_scoped(self) -> None:
        """A stale releaser must not clear the lease a newer owner is holding."""
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="owner-new", lease_expires_at=_live(), run_id=run_id)])

        await release_run_lease(pool, run_id, "owner-stale")

        row = pool.row(run_id)
        self.assertEqual(row["owner_id"], "owner-new")
        self.assertIsNotNone(row["lease_expires_at"])
        self.assertIn(OWNER_SCOPED_GUARD, pool.find("UPDATE agent_runs")[0][0])

    async def test_04_release_clears_the_caller_own_lease(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])

        await release_run_lease(pool, run_id, "owner-a")

        row = pool.row(run_id)
        self.assertIsNone(row["owner_id"])
        self.assertIsNone(row["lease_expires_at"])


class TestReconcileOrphanedAgentRuns(unittest.IsolatedAsyncioTestCase):
    """The startup sweep. The paused / awaiting_approval guarantee is the point."""

    async def _reconcile(
        self,
        pool: _FakePool,
        redis: Optional[_FakeRedis] = None,
        hub: Optional[_FakeHub] = None,
        limit: int = 50,
        max_batches: int = 10,
    ) -> List[str]:
        redis = redis or _FakeRedis()
        hub = hub or _FakeHub()
        with patch("backend.run_lease.get_redis_client", return_value=redis), patch(
            "backend.main.run_event_hub", hub
        ):
            return await reconcile_orphaned_agent_runs(
                pool, limit=limit, max_batches=max_batches
            )

    async def test_01_sweeps_expired_running_row_and_records_the_reason(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="dead", lease_expires_at=_expired(), attempt=1, run_id=run_id)])

        reclaimed = await self._reconcile(pool)

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertIsNone(pool.row(run_id)["owner_id"], "a reclaimed run must not keep a lease")

    async def test_02_never_sweeps_paused_or_awaiting_approval(self) -> None:
        """A run parked on a human is not orphaned. Failing one destroys live work.

        The fake connection returns EVERY seeded row from the candidate SELECT, so
        this proves the conditional UPDATE is what protects these runs rather than
        the SELECT's WHERE clause alone.
        """
        expired_running = _row(status="running", owner_id="dead", lease_expires_at=_expired(), attempt=1)
        paused = _row(status="paused", owner_id=None, lease_expires_at=None)
        awaiting = _row(status="awaiting_approval", owner_id=None, lease_expires_at=None)
        stalled = _row(status="stalled", owner_id=None, lease_expires_at=None)
        pool = _FakePool([expired_running, paused, awaiting, stalled])

        reclaimed = await self._reconcile(pool)

        self.assertEqual(reclaimed, [expired_running["run_id"]])
        for row in (paused, awaiting, stalled):
            self.assertNotEqual(pool.row(row["run_id"])["status"], ORPHANED_RUN_STATUS)
        self.assertEqual(pool.row(paused["run_id"])["status"], "paused")
        self.assertEqual(pool.row(awaiting["run_id"])["status"], "awaiting_approval")
        self.assertEqual([s["run_id"] for s in pool.steps], [expired_running["run_id"]])

    async def test_03_leaves_a_live_leased_running_row_alone(self) -> None:
        live_run = _row(status="running", owner_id="owner-a", lease_expires_at=_live())
        pool = _FakePool([live_run])

        self.assertEqual(await self._reconcile(pool), [])
        self.assertEqual(pool.row(live_run["run_id"])["status"], "running")

    async def test_04_candidate_select_is_limited(self) -> None:
        pool = _FakePool([])
        await self._reconcile(pool, )
        selects = pool.find("SELECT run_id")
        self.assertEqual(len(selects), 1)
        self.assertIn("LIMIT $1", selects[0][0])
        self.assertIn(STATUS_GUARD, selects[0][0])

    async def test_05_hits_db_redis_and_hub_for_each_orphan(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), attempt=1, run_id=run_id)])
        redis, hub = _FakeRedis(), _FakeHub()

        await self._reconcile(pool, redis, hub)

        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertEqual(len(pool.steps), 1)
        self.assertEqual(pool.steps[0]["action"], ORPHANED_STEP_ACTION)
        self.assertIn(redis.states[run_id]["status"], (ORPHANED_RUN_STATUS, "failed"))
        self.assertTrue(redis.states[run_id]["reconciled"])
        self.assertEqual(len(hub.published), 1)
        published_run, event = hub.published[0]
        self.assertEqual(published_run, run_id)
        self.assertEqual(event["type"], "status_change")
        self.assertTrue(event["terminal"])

    async def test_06_survives_redis_failure_and_still_terminalises(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), attempt=1, run_id=run_id)])
        hub = _FakeHub()

        reclaimed = await self._reconcile(pool, _FakeRedis(fail=True), hub)

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertEqual(len(hub.published), 1, "a Redis outage must not stop the SSE publish")

    async def test_07_survives_hub_failure_and_still_mirrors_to_redis(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), attempt=1, run_id=run_id)])
        redis = _FakeRedis()

        reclaimed = await self._reconcile(pool, redis, _FakeHub(fail=True))

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertIn(run_id, redis.states)

    async def test_08_survives_step_insert_failure(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", lease_expires_at=_expired(), attempt=1, run_id=run_id)],
            fail_on=["INSERT INTO agent_steps"],
        )
        redis, hub = _FakeRedis(), _FakeHub()

        reclaimed = await self._reconcile(pool, redis, hub)

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertEqual(len(hub.published), 1)

    async def test_09_primary_db_write_failure_reclaims_nothing(self) -> None:
        pool = _FakePool(
            [_row(status="running", lease_expires_at=_expired(), attempt=1)],
            fail_on=["SET status = $2"],
        )
        with self.assertLogs("backend.run_lease", level="ERROR"):
            self.assertEqual(await self._reconcile(pool), [])

    async def test_10_candidate_enumeration_failure_reclaims_nothing(self) -> None:
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), attempt=1)], fail_on=["SELECT run_id"])
        with self.assertLogs("backend.run_lease", level="ERROR"):
            self.assertEqual(await self._reconcile(pool), [])

    async def test_11_uses_existing_terminal_status(self) -> None:
        """A novel status would leave the frontend's terminal set hanging."""
        self.assertIn(ORPHANED_RUN_STATUS, TERMINAL_RUN_STATUSES)
        self.assertEqual(ORPHANED_RUN_STATUS, "failed")

    async def test_12_never_sweeps_a_row_the_lease_scheme_never_owned(self) -> None:
        """Rollout safety: a pre-lease execution is live, not orphaned.

        An execution started by the pre-lease code writes no owner_id and no
        lease_expires_at, so a sweep in a freshly deployed environment sees a NULL lease
        on a run that is still driving a browser. Failing it declares the run dead
        while it keeps clicking in the ERP. attempt = 0 means "no lease-aware
        execution has ever owned this row", which is exactly the set the sweep must
        decline.
        """
        legacy = _row(status="running", owner_id=None, lease_expires_at=None, attempt=0)
        leased_once = _row(
            status="running", owner_id=None, lease_expires_at=None, attempt=1
        )
        pool = _FakePool([legacy, leased_once])
        hub = _FakeHub()

        reclaimed = await self._reconcile(pool, _FakeRedis(), hub)

        self.assertEqual(reclaimed, [leased_once["run_id"]])
        self.assertEqual(pool.row(legacy["run_id"])["status"], "running")
        self.assertIn(ATTEMPTED_GUARD, pool.find("SET status = $2")[0][0])
        self.assertNotIn(legacy["run_id"], [r for r, _ in hub.published])

    async def test_13_drains_more_than_one_batch(self) -> None:
        """A backlog larger than one batch must not wait for the next cold start."""
        pool = _FakePool(
            [
                _row(status="running", lease_expires_at=_expired(), attempt=1)
                for _ in range(5)
            ]
        )

        reclaimed = await self._reconcile(pool, limit=2)

        self.assertEqual(len(reclaimed), 5)
        self.assertEqual(len(pool.find("SELECT run_id")), 3, "2 + 2 + 1 batches")

    async def test_14_batch_count_is_bounded(self) -> None:
        """The drain must not become an unbounded write burst on a cold start."""
        pool = _FakePool(
            [
                _row(status="running", lease_expires_at=_expired(), attempt=1)
                for _ in range(10)
            ]
        )

        with self.assertLogs("backend.run_lease", level="ERROR"):
            reclaimed = await self._reconcile(pool, limit=1, max_batches=3)

        self.assertEqual(len(reclaimed), 3)
        self.assertEqual(len(pool.find("SELECT run_id")), 3)

    async def test_15_new_owner_keeps_the_read_models(self) -> None:
        """The mirrors are not the source of truth and must not clobber a live owner.

        Between the sweep's UPDATE and the Redis write, a resume or duplicate dispatch
        can legitimately claim the row (its upsert takes over a terminal row with a
        NULL lease). set_session_state replaces the whole hash, so writing here would
        overwrite the new owner's running snapshot and broadcast a terminal status for a
        run being executed right now.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", lease_expires_at=_expired(), attempt=1, run_id=run_id)]
        )
        redis, hub = _FakeRedis(), _FakeHub()
        agent = _agent(pool, run_id)

        async def _reclaim(*args: Any, **kwargs: Any) -> Any:
            # A new owner claims the run the instant the sweep has written its status.
            taken = await agent.ensure_run_record("goal")
            self.assertTrue(taken)
            return "INSERT 0 1"

        with patch.object(_FakeConn, "execute", side_effect=_reclaim):
            reclaimed = await self._reconcile(pool, redis, hub)

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["owner_id"], agent._run_owner_id)
        self.assertEqual(hub.published, [], "a terminal status was broadcast over a live owner")
        self.assertNotIn(run_id, redis.states)
        self.assertTrue(
            any(OWNER_RECHECK_QUERY in q for q in pool.queries()),
            "the mirrors must be gated on a fresh ownership read",
        )


class TestEnsureRunRecordLease(unittest.IsolatedAsyncioTestCase):
    async def test_01_refused_when_another_owner_holds_a_live_lease(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])
        agent = _agent(pool, run_id)

        accepted = await agent.ensure_run_record("goal")

        self.assertFalse(accepted, "a duplicate request must not start the same run twice")
        self.assertEqual(pool.row(run_id)["owner_id"], "owner-a")
        self.assertEqual(pool.row(run_id)["attempt"], 0)

    async def test_02_refused_for_terminal_row_with_a_live_lease(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="done", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])
        agent = _agent(pool, run_id)

        self.assertFalse(await agent.ensure_run_record("goal"))
        self.assertEqual(pool.row(run_id)["status"], "done")

    async def test_03_resurrects_a_terminal_row_with_no_live_lease(self) -> None:
        """The resume/recovery path: preserved deliberately, existing tests rely on it."""
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="failed", owner_id=None, lease_expires_at=None, attempt=2, run_id=run_id)])
        agent = _agent(pool, run_id)

        self.assertTrue(await agent.ensure_run_record("goal"))
        row = pool.row(run_id)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["attempt"], 3)
        self.assertEqual(row["owner_id"], agent._run_owner_id)

    async def test_04_claim_sql_keeps_the_live_lease_guard(self) -> None:
        """ensure_run_record's upsert must stay gated on the lease, not unconditional.

        It used to be a bare `ON CONFLICT (run_id) DO UPDATE SET status = 'running'`,
        which resurrected a terminal run and voided every lease.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])
        agent = _agent(pool, run_id)

        await agent.ensure_run_record("goal")

        upsert = pool.find("ON CONFLICT (run_id) DO UPDATE")[0][0]
        self.assertIn("agent_runs.lease_expires_at IS NULL", upsert)
        self.assertIn("agent_runs.lease_expires_at <= now()", upsert)
        self.assertIn("agent_runs.owner_id = EXCLUDED.owner_id", upsert)
        self.assertIn("owner_id = EXCLUDED.owner_id", upsert)

    async def test_05_owner_ids_are_unique_per_execution(self) -> None:
        a = _agent(_FakePool([]), str(uuid.uuid4()))
        b = _agent(_FakePool([]), str(uuid.uuid4()))
        self.assertNotEqual(a._run_owner_id, b._run_owner_id)

    async def test_06_heartbeat_is_not_started_by_persist_step_recovery(self) -> None:
        """ensure_run_record defaults to no heartbeat; only run() may spawn one."""
        pool = _FakePool([_row(status="failed", lease_expires_at=None)])
        agent = _agent(pool, str(uuid.uuid4()))

        self.assertTrue(await agent.ensure_run_record("goal"))
        self.assertIsNone(agent._run_heartbeat_task)

    async def test_07_terminal_status_releases_the_lease(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", run_id=run_id)])
        agent = _agent(pool, run_id)
        agent._run_owner_id = "owner-me"
        await agent.ensure_run_record("goal")

        await agent.update_run_status("completed")

        row = pool.row(run_id)
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["owner_id"], "a terminal run must hand its lease back")

    async def test_08_non_terminal_status_keeps_the_lease(self) -> None:
        for status in ("running", "paused", "awaiting_approval"):
            with self.subTest(status=status):
                run_id = str(uuid.uuid4())
                pool = _FakePool([_row(status="running", run_id=run_id)])
                agent = _agent(pool, run_id)
                agent._run_owner_id = "owner-me"
                await agent.ensure_run_record("goal")

                await agent.update_run_status(status)

                self.assertEqual(pool.row(run_id)["owner_id"], "owner-me")

    async def test_09_every_terminal_status_used_by_agent_is_covered(self) -> None:
        """If a new terminal status is added to agent.py without updating this set,
        its lease would never be released and the run could never be reclaimed."""
        with open(os.path.join(os.path.dirname(__file__), "..", "agent.py"), "r", encoding="utf-8") as f:
            source = f.read()

        used = set()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("await self.update_run_status("):
                used.add(stripped[len("await self.update_run_status("):].split(")")[0].strip('"\''))

        self.assertTrue(used, "no update_run_status call sites were found")
        for status in used:
            with self.subTest(status=status):
                if status in {"running", "paused", "awaiting_approval"}:
                    continue
                self.assertIn(status, TERMINAL_RUN_STATUSES)

    async def test_10_run_bails_out_when_the_lease_is_refused(self) -> None:
        """A refused claim must stop the run before it executes anything.

        Pins the contract that ReActAgent.run() branches on ensure_run_record's
        boolean. Stubbing it with a falsy value (it used to return None) silently
        turned every such test into a "refused" run, so this asserts the branch
        directly rather than relying on it.
        """
        agent = _agent(_FakePool([]), str(uuid.uuid4()))
        agent.ensure_run_record = AsyncMock(return_value=False)
        agent.get_last_successful_step_index = AsyncMock(return_value=0)
        agent.persist_step = AsyncMock(return_value=None)
        agent.update_run_status = AsyncMock(return_value=None)

        result = await asyncio.wait_for(agent.run("create invoices"), timeout=5.0)

        self.assertEqual(result["status"], "failed")
        self.assertIn("already leased", result["summary"])
        agent.get_last_successful_step_index.assert_not_called()
        self.assertIsNone(agent._run_heartbeat_task, "a refused run must not start a heartbeat")

    async def test_11_heartbeat_stops_when_it_loses_ownership(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="done", owner_id="someone-else", lease_expires_at=_live(), run_id=run_id)])

        task = asyncio.create_task(
            run_lease_heartbeat(pool, run_id, "owner-a", 0.01, 900.0)
        )
        await asyncio.wait_for(task, timeout=2.0)

        self.assertEqual(pool.row(run_id)["owner_id"], "someone-else")


class TestRunStatusFencing(unittest.IsolatedAsyncioTestCase):
    """A status write must be fenced on the lease owner.

    An expired lease does not stop the old holder: a frozen-then-thawed execution
    wakes up mid-run and would otherwise write `completed` straight over a newer
    owner's state, including a run reconciliation already failed.
    """

    def _leased_agent(
        self, row_owner: Optional[str], agent_owner: str
    ) -> Tuple[_FakePool, ReActAgent]:
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id=row_owner, lease_expires_at=_live(), run_id=run_id)]
        )
        agent = _agent(pool, run_id)
        agent._run_owner_id = agent_owner
        agent.emit_event = AsyncMock()
        return pool, agent

    async def test_01_owner_write_is_applied_and_broadcast(self) -> None:
        pool, agent = self._leased_agent("owner-me", "owner-me")

        await agent.update_run_status("completed")

        self.assertEqual(pool.row(agent.run_id)["status"], "completed")
        agent.emit_event.assert_awaited_once_with(
            "status_change", {"status": "completed"}
        )

    async def test_02_superseded_owner_write_is_dropped(self) -> None:
        # The row is now owned by a newer execution; this agent is the stale one.
        pool, agent = self._leased_agent("owner-new", "owner-old")

        await agent.update_run_status("completed")

        self.assertEqual(
            pool.row(agent.run_id)["status"],
            "running",
            "a superseded execution overwrote newer state",
        )
        agent.emit_event.assert_not_awaited()

    async def test_03_superseded_owner_cannot_overwrite_a_reconciled_run(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="failed", owner_id=None, lease_expires_at=None, run_id=run_id)]
        )
        agent = _agent(pool, run_id)
        agent._run_owner_id = "ghost-owner"
        agent.emit_event = AsyncMock()

        await agent.update_run_status("completed")

        self.assertEqual(pool.row(run_id)["status"], "failed")

    async def test_04_unowned_row_rejects_a_status_write(self) -> None:
        """NULL owner means the lease was released or the run was reclaimed.

        Either way this execution is not the owner, so it must not write. An earlier
        draft allowed `owner_id IS NULL` here to avoid stranding a run whose row was
        created by tools.py's ON CONFLICT DO NOTHING insert (which sets no owner) --
        but that allowance let a ghost owner resurrect a run reconciliation had
        already failed, which is the exact bug the fence exists to stop. run() always
        claims the lease via ensure_run_record before any status write, so the
        legitimate writer is always fenced in.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id=None, lease_expires_at=None, run_id=run_id)]
        )
        agent = _agent(pool, run_id)
        agent._run_owner_id = "whoever"
        agent.emit_event = AsyncMock()

        await agent.update_run_status("stalled")

        self.assertEqual(pool.row(run_id)["status"], "running")
        agent.emit_event.assert_not_awaited()

    async def test_05_status_sql_carries_the_fence(self) -> None:
        pool, agent = self._leased_agent("owner-me", "owner-me")

        await agent.update_run_status("running")

        self.assertIn(STATUS_FENCE_GUARD, pool.find("SET status = $1")[0][0])

    async def test_06_fenced_non_terminal_write_is_dropped_and_not_announced(self) -> None:
        """The fence is not terminal-only.

        A superseded execution can also try to write 'running' or 'paused' on its way
        out. Broadcasting a status_change for a write that never landed would leave the
        client showing a transition the durable store does not have, and the SSE poll
        would then walk it back on the next pass.
        """
        for status in ("running", "paused", "awaiting_approval"):
            with self.subTest(status=status):
                pool, agent = self._leased_agent("owner-new", "owner-old")

                await agent.update_run_status(status)

                self.assertEqual(pool.row(agent.run_id)["status"], "running")
                self.assertEqual(pool.row(agent.run_id)["owner_id"], "owner-new")
                agent.emit_event.assert_not_awaited()

    async def test_07_fenced_write_does_not_disturb_the_new_owner_s_lease(self) -> None:
        """release_run_lease was already owner-scoped; the status fence must match."""
        pool, agent = self._leased_agent("owner-new", "owner-old")

        await agent.update_run_status("completed")

        row = pool.row(agent.run_id)
        self.assertEqual(row["owner_id"], "owner-new")
        self.assertEqual(row["status"], "running")

    async def test_08_a_failed_status_write_keeps_the_lease(self) -> None:
        """Releasing on the failure path would hand a live run to the sweep.

        The write raises, so agent_runs.status stays 'running' -- and clearing
        owner_id / lease_expires_at then makes the row match the orphan predicate
        exactly. The next startup sweep, or a duplicate request, can claim a run this
        execution is still driving, and nothing renews or fences it any more. Leaving
        the lease in place is safe: it lapses on its own.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-me", lease_expires_at=_live(), run_id=run_id)]
        )
        agent = _agent(pool, run_id)
        agent._run_owner_id = "owner-me"
        agent.emit_event = AsyncMock()

        with patch.object(_FakeConn, "execute", side_effect=RuntimeError("db blip")):
            await agent.update_run_status("completed")

        row = pool.row(run_id)
        self.assertEqual(row["status"], "running", "the write must not have landed")
        self.assertEqual(row["owner_id"], "owner-me", "the lease was released anyway")
        self.assertIsNotNone(row["lease_expires_at"])
        self.assertIsNone(agent._run_heartbeat_task)
        agent.emit_event.assert_not_awaited()

    async def test_09_a_successful_terminal_write_still_releases(self) -> None:
        """The landed case must keep releasing, or finished runs hold a live lease."""
        pool, agent = self._leased_agent("owner-me", "owner-me")

        await agent.update_run_status("completed")

        row = pool.row(agent.run_id)
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["owner_id"])
        self.assertIsNone(row["lease_expires_at"])

    async def test_10_a_fenced_out_terminal_write_does_not_release(self) -> None:
        """A superseded owner must not touch the new owner's lease at all.

        The renew the heartbeat depends on is owner-scoped, so a superseded execution
        cannot keep a lease alive either way -- but clearing state it does not own is
        needless, and `stop_run_lease` is exactly the call that would do it.
        """
        pool, agent = self._leased_agent("owner-new", "owner-old")

        await agent.update_run_status("completed")

        row = pool.row(agent.run_id)
        self.assertEqual(row["owner_id"], "owner-new")
        self.assertIsNotNone(row["lease_expires_at"])
        self.assertEqual(pool.find("owner_id = NULL"), [])


class TestHeartbeatReportsOwnershipLoss(unittest.IsolatedAsyncioTestCase):
    """Losing the lease has to reach the loop, not just the log."""

    async def test_01_reports_the_loss_once_when_renewal_matches_no_rows(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-new", lease_expires_at=_live(), run_id=run_id)]
        )
        reported: List[str] = []

        await asyncio.wait_for(
            run_lease_heartbeat(
                pool, run_id, "owner-old", 0.01, 900.0, on_ownership_lost=lambda: reported.append("lost")
            ),
            timeout=2.0,
        )

        self.assertEqual(reported, ["lost"], "the heartbeat must report the loss exactly once")
        self.assertEqual(pool.row(run_id)["owner_id"], "owner-new")

    async def test_02_survives_a_raising_callback(self) -> None:
        """The heartbeat is already returning; a bad callback must not raise out of it."""
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-new", lease_expires_at=_live(), run_id=run_id)]
        )

        def _boom() -> None:
            raise RuntimeError("callback exploded")

        with self.assertLogs("backend.run_lease", level="WARNING"):
            await asyncio.wait_for(
                run_lease_heartbeat(pool, run_id, "owner-old", 0.01, 900.0, on_ownership_lost=_boom),
                timeout=2.0,
            )

    async def test_03_not_reported_while_the_lease_is_held(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)]
        )
        reported: List[str] = []

        task = asyncio.create_task(
            run_lease_heartbeat(
                pool, run_id, "owner-a", 0.01, 900.0, on_ownership_lost=lambda: reported.append("lost")
            )
        )
        try:
            await asyncio.wait_for(_await_renewals(pool, minimum=2), timeout=5.0)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.assertEqual(reported, [], "a held lease is not a lost lease")

    async def test_04_agent_flag_is_set_end_to_end(self) -> None:
        """The agent's own marker is what run() reads, so wire the real two together."""
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-new", lease_expires_at=_live(), run_id=run_id)]
        )
        agent = _agent(pool, run_id)
        agent._run_owner_id = "owner-old"
        self.assertFalse(agent._lease_ownership_lost)

        await asyncio.wait_for(
            run_lease_heartbeat(
                pool, run_id, agent._run_owner_id, 0.01, 900.0,
                on_ownership_lost=agent._mark_run_lease_lost,
            ),
            timeout=2.0,
        )

        self.assertTrue(agent._lease_ownership_lost)

    async def test_05_marker_is_idempotent(self) -> None:
        agent = _agent(_FakePool([]), str(uuid.uuid4()))
        agent._mark_run_lease_lost()
        agent._mark_run_lease_lost()
        self.assertTrue(agent._lease_ownership_lost)

    async def test_06_sustained_renewal_failure_reports_ownership_loss(self) -> None:
        """A renewal that keeps RAISING never returns zero rows, so the "lease lost"
        branch is unreachable and an unbounded retry would let the lease lapse
        silently while the run keeps acting on it.

        The heartbeat must give up once ``lease_seconds`` has passed with no renewal
        actually landing, which is the point at which another instance's reconciliation
        or a duplicate dispatch can take the run.

        The window is 0.5s against a 0.01s interval, not something tighter, because the
        retry assertion below is only meaningful if a retry is structurally guaranteed.
        The loop sleeps ``min(interval, remaining)``, so one scheduling stall longer than
        the window ends the heartbeat before it can retry -- correctly, but it makes
        "the retry must not be a no-op" a coin flip. Measured on a loaded box, a 0.05s
        window yielded 0 or 1 renewal attempts and this test failed around a third of the
        time; at 0.5s it yielded 2 to 21 under the same load and never failed. The
        sibling cases already use 0.15s and 0.2s for the same reason. Nothing is relaxed:
        the heartbeat still has to report the loss, and still has to have retried.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)],
            fail_on=[_RENEW_QUERY_MARKER],
        )
        reported: List[str] = []

        with self.assertLogs("backend.run_lease", level="ERROR"):
            await asyncio.wait_for(
                run_lease_heartbeat(
                    pool, run_id, "owner-a", 0.01, 0.5,
                    on_ownership_lost=lambda: reported.append("lost"),
                ),
                timeout=5.0,
            )

        self.assertEqual(reported, ["lost"])
        self.assertGreater(len(pool.find(_RENEW_QUERY_MARKER)), 1, "the retry must not be a no-op")

    async def test_07_transient_renewal_failure_is_retried_without_reporting(self) -> None:
        """A blip shorter than the window must not be mistaken for a lost lease.

        validate_run_lease_settings sizes the window with margin precisely so a short
        outage does not stop a live run; giving up on the first failure would
        reintroduce the bug this callback was added to fix.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)]
        )
        reported: List[str] = []
        real_renew = renew_run_lease
        calls = {"n": 0}

        async def _flaky(*args: Any, **kwargs: Any) -> bool:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("pool acquire timeout")
            return await real_renew(*args, **kwargs)

        with patch("backend.run_lease.renew_run_lease", new=_flaky):
            task = asyncio.create_task(
                run_lease_heartbeat(
                    pool, run_id, "owner-a", 0.01, 900.0,
                    on_ownership_lost=lambda: reported.append("lost"),
                )
            )
            try:
                await asyncio.wait_for(_await_renewals(pool, minimum=1), timeout=5.0)
                self.assertFalse(task.done(), "a short outage must not stop the heartbeat")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self.assertEqual(reported, [])
        self.assertGreaterEqual(calls["n"], 3)

    async def test_08_long_healthy_run_still_tolerates_a_later_failure(self) -> None:
        """The window is measured from the last renewal, not from the heartbeat's start.

        Seeding the clock once and never resetting it would stop any run that has been
        executing longer than one lease window the first time a single renewal blipped
        -- which is the heartbeat-during-a-pause bug all over again, and it would fire
        on long healthy runs where it matters most.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)]
        )
        reported: List[str] = []
        real_renew = renew_run_lease
        state = {"n": 0, "failing": False}
        # The healthy phase has to outlast a whole window on its own, or this test
        # cannot tell a per-renewal clock reset from a single seed at startup: 8
        # renewals at a 10ms interval is 80ms, well inside the 200ms window, so the
        # failures below would still land before the deadline even if the clock were
        # never reset. 40 renewals is ~400ms, twice the window, which fails if the
        # reset is removed and still finishes far inside the 5s bound.
        healthy_needed = 40
        failures_needed = 3

        async def _fail_after_healthy(*args: Any, **kwargs: Any) -> bool:
            state["n"] += 1
            if state["failing"]:
                raise RuntimeError("pool acquire timeout")
            renewed = await real_renew(*args, **kwargs)
            if state["n"] >= healthy_needed:
                state["failing"] = True
            return renewed

        with patch("backend.run_lease.renew_run_lease", new=_fail_after_healthy):
            task = asyncio.create_task(
                run_lease_heartbeat(
                    pool, run_id, "owner-a", 0.01, 0.2,
                    on_ownership_lost=lambda: reported.append("lost"),
                )
            )
            try:
                # Bounded: if the heartbeat gives up early, `state["n"]` stops
                # advancing and this must fail rather than spin forever.
                await asyncio.wait_for(
                    _await_call_count(state, minimum=healthy_needed + failures_needed),
                    timeout=5.0,
                )
                await asyncio.sleep(0.05)
                self.assertFalse(
                    task.done(),
                    "a healthy run was stopped by a failure well inside its own window",
                )
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self.assertEqual(reported, [])

    async def test_09_a_stalled_renewal_is_bounded_and_reports_the_loss(self) -> None:
        """A renewal parked on a connection that never answers must still time out.

        The pool is built without an asyncpg command_timeout and acquire() is called
        without one, so ``renew_run_lease`` can hang indefinitely. A deadline checked
        only after the await returns is never reached in that case: the heartbeat
        cannot report the loss, and the superseded run keeps driving the browser while
        its lease sits expired and claimable by a duplicate request.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)]
        )
        reported: List[str] = []
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def _stalls_forever(*args: Any, **kwargs: Any) -> bool:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return True  # pragma: no cover - unreachable

        with patch("backend.run_lease.renew_run_lease", new=_stalls_forever):
            with self.assertLogs("backend.run_lease", level="ERROR"):
                # lease_seconds is the whole bound: the heartbeat must be gone well
                # inside the 5s ceiling, and gone because the attempt was cancelled
                # rather than because the stub returned.
                await asyncio.wait_for(
                    run_lease_heartbeat(
                        pool, run_id, "owner-a", 0.01, 0.15,
                        on_ownership_lost=lambda: reported.append("lost"),
                    ),
                    timeout=5.0,
                )

        self.assertTrue(entered.is_set(), "the stalled attempt was never reached")
        self.assertTrue(cancelled.is_set(), "the parked attempt must be cancelled, not abandoned")
        self.assertEqual(reported, ["lost"])

    async def test_10_the_sleep_never_outlasts_the_renewable_window(self) -> None:
        """The interval is capped by the window, so no sleep sits past the expiry.

        With a plain ``asyncio.sleep(interval_seconds)`` and an interval longer than
        the lease, the first sleep alone would carry the heartbeat well past its own
        expiry before it ever attempted a renewal.

        validate_run_lease_settings rejects this configuration outright, so what is
        pinned here is the defensive behaviour rather than a supported setting: the
        capped sleep consumes the window, no time is left to renew in, and the
        heartbeat reports the loss promptly instead of sleeping out the interval.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)]
        )
        reported: List[str] = []
        calls = {"n": 0}

        async def _always_refused(*args: Any, **kwargs: Any) -> bool:
            calls["n"] += 1
            return False

        interval_seconds = 10.0
        lease_seconds = 0.15
        started = asyncio.get_event_loop().time()
        with patch("backend.run_lease.renew_run_lease", new=_always_refused):
            with self.assertLogs("backend.run_lease", level="ERROR"):
                await asyncio.wait_for(
                    run_lease_heartbeat(
                        pool, run_id, "owner-a", interval_seconds, lease_seconds,
                        on_ownership_lost=lambda: reported.append("lost"),
                    ),
                    timeout=5.0,
                )
        elapsed = asyncio.get_event_loop().time() - started

        self.assertEqual(calls["n"], 0, "the capped sleep leaves no window to renew in")
        self.assertEqual(reported, ["lost"])
        self.assertLess(
            elapsed,
            interval_seconds / 2,
            "the heartbeat slept the full interval instead of the remaining window",
        )


class _LoopHarness:
    """A ReActAgent wired for run() with every durable side effect stubbed out."""

    def __init__(
        self,
        groq_messages: List[Any],
        max_iterations: int = 5,
    ) -> None:
        self.events: List[Dict[str, Any]] = []
        self.tools = MagicMock(spec=PlaywrightTools)
        self.tools.page = MagicMock()
        self.tools.page.is_closed = MagicMock(return_value=False)
        self.tools.set_run_id = MagicMock()
        self.tools.set_page = MagicMock()
        self.tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
        self.redis = _unconfigured_redis()

        async def _on_event(evt: Dict[str, Any]) -> None:
            self.events.append(evt)

        self.agent = ReActAgent(
            run_id=str(uuid.uuid4()),
            tools=self.tools,
            groq_client=_groq_returning(groq_messages),
            redis_client=self.redis,
            max_iterations=max_iterations,
            on_event=_on_event,
        )
        # Accepted by this execution: these cases are about what happens AFTER the
        # lease is granted, so the claim itself is stubbed.
        self.agent.ensure_run_record = AsyncMock(return_value=True)
        self.agent.get_last_successful_step_index = AsyncMock(return_value=0)
        self.agent.persist_step = AsyncMock(return_value=str(uuid.uuid4()))
        self.agent.update_run_status = AsyncMock(return_value=None)

    def event_types(self) -> List[str]:
        return [str(e.get("type")) for e in self.events]

    def read_page_ok(self, on_read: Optional[Any] = None) -> None:
        """Successful OBSERVE, optionally flipping a callback (e.g. lease loss) first."""

        async def _read_page() -> Dict[str, Any]:
            if on_read is not None:
                on_read()
            return {
                "success": True,
                "snapshot": "- button 'Submit'",
                "url": "http://x",
                "title": "T",
            }

        self.tools.read_page = AsyncMock(side_effect=_read_page)


class TestOwnershipLossStopsTheRun(unittest.IsolatedAsyncioTestCase):
    """A superseded execution must stop before it acts or writes anything.

    The owner-scoped fence on update_run_status only drops the agent_runs.status
    write. Without these guards the lost owner keeps calling tools.execute (the
    side-effecting click/fill/select that creates an invoice), keeps persisting steps,
    keeps writing Redis state and still emits `done`.
    """

    async def _run(self, harness: _LoopHarness) -> Dict[str, Any]:
        return await asyncio.wait_for(harness.agent.run("create invoices"), timeout=10.0)

    def _assert_no_state_writes(self, harness: _LoopHarness) -> None:
        harness.agent.persist_step.assert_not_awaited()
        harness.agent.update_run_status.assert_not_awaited()
        self.assertNotIn("done", harness.event_types())
        self.assertNotIn("step_complete", harness.event_types())
        self.assertNotIn("step", harness.event_types())

    async def test_01_stops_before_tools_execute_when_the_lease_goes_mid_iteration(self) -> None:
        harness = _LoopHarness([_tool_call_message("c1", "click", {"selector": "Create Invoice"})])
        agent = harness.agent
        # Ownership is lost while OBSERVE is running, i.e. after the iteration-boundary
        # guard has already passed and before the action is taken.
        harness.read_page_ok(on_read=agent._mark_run_lease_lost)
        harness.tools.execute = AsyncMock(return_value={"success": True})

        result = await self._run(harness)

        harness.tools.execute.assert_not_awaited()
        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self.assertNotIn("step_start", harness.event_types(), "a step was announced but not taken")
        self._assert_no_state_writes(harness)

    async def test_02_iteration_boundary_stops_before_any_tool_runs(self) -> None:
        harness = _LoopHarness([_tool_call_message("c1", "click", {"selector": "Create Invoice"})])
        harness.tools.execute = AsyncMock(return_value={"success": True})
        harness.tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "", "url": "http://x", "title": "T"}
        )
        harness.agent._lease_ownership_lost = True

        result = await self._run(harness)

        harness.tools.read_page.assert_not_awaited()
        harness.tools.execute.assert_not_awaited()
        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self._assert_no_state_writes(harness)

    async def test_03_done_is_not_emitted_after_ownership_loss(self) -> None:
        harness = _LoopHarness([_text_message("Task completed successfully.")])
        agent = harness.agent
        harness.read_page_ok(on_read=agent._mark_run_lease_lost)
        harness.tools.execute = AsyncMock(return_value={"success": True})

        result = await self._run(harness)

        self.assertEqual(result["status"], "failed", "a lost lease must not report completion")
        self.assertNotIn("done", harness.event_types())
        agent.update_run_status.assert_not_awaited()
        for call in agent.persist_step.await_args_list:
            self.assertNotEqual(call.kwargs.get("action"), "done")

    async def test_04_session_lost_abort_is_suppressed(self) -> None:
        """_abort_session_lost is reached from inside an iteration, past the boundary."""
        harness = _LoopHarness([_text_message("still working")])
        agent = harness.agent
        harness.tools.read_page = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed")
        )
        agent._mark_run_lease_lost()

        result = await self._run(harness)

        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self.assertNotEqual(result["status"], "session_lost")
        agent.update_run_status.assert_not_awaited()
        self.assertNotIn("session_lost", harness.event_types())

    async def test_05_hard_cap_stall_is_suppressed(self) -> None:
        """The hard-cap block runs after the loop, so no boundary guard covers it."""
        harness = _LoopHarness(
            [_tool_call_message("c1", "navigate", {"url": "/invoices"})],
            max_iterations=1,
        )
        agent = harness.agent
        harness.read_page_ok()
        harness.tools.execute = AsyncMock(return_value={"success": True, "url": "http://x"})

        # Ownership is lost on the last thing the loop body does, so the hard-cap
        # check is the first code to run after the loop exits.
        async def _persist(*args: Any, **kwargs: Any) -> str:
            agent._mark_run_lease_lost()
            return str(uuid.uuid4())

        agent.persist_step = AsyncMock(side_effect=_persist)

        result = await self._run(harness)

        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self.assertNotIn("stalled", harness.event_types())
        agent.update_run_status.assert_not_awaited()

    async def test_06_approval_gate_does_not_open_once_ownership_is_lost(self) -> None:
        harness = _LoopHarness([_text_message("still working")])
        agent = harness.agent
        agent.get_redis = AsyncMock(return_value=harness.redis)
        agent._lease_ownership_lost = True

        outcome = await agent.handle_approval_gate(
            vendor="Acme", amount=60000.0, invoice_id="inv-1", po_number="PO-1"
        )

        self.assertEqual(outcome, "lease_lost")
        agent.get_redis.assert_not_awaited()
        harness.redis.set_session_state.assert_not_awaited()
        harness.redis.set_approval_pending.assert_not_called()
        agent.persist_step.assert_not_awaited()
        agent.update_run_status.assert_not_awaited()

    async def test_07_approval_gate_abandons_a_wait_it_no_longer_owns(self) -> None:
        """The rejection path marks the invoice skipped in Neon -- a durable ERP write.

        Checking only at the gate's entry would not help: the gate blocks for up to
        APPROVAL_TIMEOUT_SECONDS, so the lease can be taken over while it polls.
        """
        harness = _LoopHarness([_text_message("still working")])
        agent = harness.agent
        redis = _unconfigured_redis()
        redis.is_configured = True
        redis.execute_command = AsyncMock(return_value=True)
        redis.set_approval_pending = AsyncMock(return_value=True)
        redis.get_approval_decision_record = AsyncMock(return_value=None)
        redis.clear_approval = AsyncMock(return_value=True)

        async def _set_state(*args: Any, **kwargs: Any) -> bool:
            # Reported once the gate has announced itself and is about to poll.
            agent._mark_run_lease_lost()
            return True

        redis.set_session_state = AsyncMock(side_effect=_set_state)
        agent.get_redis = AsyncMock(return_value=redis)

        outcome = await asyncio.wait_for(
            agent.handle_approval_gate(
                vendor="Acme", amount=60000.0, invoice_id="inv-1", po_number="PO-1"
            ),
            timeout=20.0,
        )

        self.assertEqual(outcome, "lease_lost")
        redis.clear_approval.assert_not_awaited(), "a lost owner must not clear the approval keys"
        redis.get_approval_decision_record.assert_not_awaited()
        # Only the writes the gate legitimately made while it still held the lease.
        self.assertEqual(agent.update_run_status.await_count, 1)
        self.assertEqual(agent.update_run_status.await_args.args[0], "awaiting_approval")
        self.assertEqual(agent.persist_step.await_count, 1)

    async def test_08_gate_outcome_lease_lost_is_not_treated_as_a_rejection(self) -> None:
        """A 'lease_lost' gate outcome must not fall into the rejection branch.

        Anything that is not 'approved' drives the rejection path, which clears the
        tracked form state and tells the model the human rejected the invoice.
        """
        harness = _LoopHarness(
            [_text_message("Invoice PO-9 for Acme requires approval of ₹60,000.")]
        )
        agent = harness.agent
        harness.read_page_ok()
        harness.tools.execute = AsyncMock(return_value={"success": True})
        agent._active_form_state.update({"amount": "60000", "po_number": "PO-9", "vendor": "Acme"})

        async def _gate(**kwargs: Any) -> str:
            agent._mark_run_lease_lost()
            return "lease_lost"

        agent.handle_approval_gate = AsyncMock(side_effect=_gate)

        result = await self._run(harness)

        agent.handle_approval_gate.assert_awaited_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self.assertIn(
            "po_number",
            agent._active_form_state,
            "the rejection branch cleared the tracked form state",
        )
        harness.tools.execute.assert_not_awaited()

    async def test_09_pause_wait_stops_when_ownership_is_lost(self) -> None:
        """The pause poll blocks up to PAUSE_TIMEOUT_SECONDS on a human.

        Its guard is separate from the iteration-boundary one because the boundary has
        already passed by the time the run parks itself, and staying parked would hold
        the pause open against whichever owner actually holds the run.
        """
        harness = _LoopHarness([_tool_call_message("c1", "navigate", {"url": "/invoices"})])
        agent = harness.agent
        redis = harness.redis
        redis.is_configured = True
        redis.get_pause_flag = AsyncMock(return_value=True)
        harness.read_page_ok()
        harness.tools.execute = AsyncMock(return_value={"success": True, "url": "http://x"})

        async def _paused(status: str) -> None:
            agent._mark_run_lease_lost()

        agent.update_run_status = AsyncMock(side_effect=_paused)

        result = await self._run(harness)

        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        agent.update_run_status.assert_awaited_once_with("paused")
        self.assertNotIn("resumed", harness.event_types())
        self.assertNotIn("stalled", harness.event_types())
        harness.tools.execute.assert_not_awaited()

    async def test_10_session_lost_abort_does_not_announce_a_second_terminal(self) -> None:
        """_abort_session_lost is entered from inside the iteration, flag set mid-OBSERVE."""
        harness = _LoopHarness([_text_message("still working")])
        agent = harness.agent

        async def _read_page() -> Dict[str, Any]:
            agent._mark_run_lease_lost()
            raise Exception("Target page, context or browser has been closed")

        harness.tools.read_page = AsyncMock(side_effect=_read_page)

        result = await self._run(harness)

        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self.assertNotIn("session_lost", harness.event_types())
        agent.update_run_status.assert_not_awaited()
        agent.persist_step.assert_not_awaited()

    async def test_11_submit_gate_lease_lost_skips_the_invoice_skipped_write(self) -> None:
        """The submit gate's rejection branch writes invoices.status = 'skipped'.

        Reaching it needs the amount to clear the approval threshold, so this is the
        path where a lost lease would otherwise mark a live owner's invoice skipped.
        """
        harness = _LoopHarness(
            [_tool_call_message("c1", "click", {"selector": "Create Invoice"})]
        )
        agent = harness.agent
        harness.read_page_ok()
        harness.tools.execute = AsyncMock(return_value={"success": True})
        agent.check_idempotency = AsyncMock(return_value={"exists": False, "error": None})
        agent._active_form_state.update(
            {"amount": "60000", "po_number": "PO-9", "vendor": "Acme"}
        )

        async def _gate(**kwargs: Any) -> str:
            agent._mark_run_lease_lost()
            return "lease_lost"

        agent.handle_approval_gate = AsyncMock(side_effect=_gate)

        result = await self._run(harness)

        agent.handle_approval_gate.assert_awaited_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("lease", result["summary"])
        self.assertIn(
            "po_number",
            agent._active_form_state,
            "the rejection branch cleared the tracked form state",
        )
        harness.tools.execute.assert_not_awaited()

    async def test_12_ownership_lost_while_emitting_step_start_blocks_the_action(self) -> None:
            """The pre-action guard does not span the step_start emit.

            `emit_event` awaits the on_event callback and the hub publish, so ownership can
            be reported lost while it is in flight. One check cannot cover a window it does
            not span: the click that creates an invoice must not begin on a lease this
            execution has already lost.
            """
            harness = _LoopHarness(
                [_tool_call_message("c1", "click", {"selector": "Create Invoice"})]
            )
            agent = harness.agent
            harness.read_page_ok()
            harness.tools.execute = AsyncMock(return_value={"success": True})

            async def _on_event(evt: Dict[str, Any]) -> None:
                harness.events.append(evt)
                if evt.get("type") == "step_start":
                    agent._mark_run_lease_lost()

            agent.on_event = _on_event

            result = await self._run(harness)

            harness.tools.execute.assert_not_awaited()
            self.assertIn("step_start", harness.event_types())
            self.assertEqual(result["status"], "failed")
            self.assertIn("lease", result["summary"])

            # step_start was published before ownership was lost, so it has to be
            # closed under the same step_id: the frontend opens a step row on
            # step_start and only resolves it on a terminal frame, so a dangling
            # start spins for the life of the stream.
            starts = [e for e in harness.events if e["type"] == "step_start"]
            failures = [e for e in harness.events if e["type"] == "step_failed"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(failures), 1, "the announced step was left unresolved")
            self.assertEqual(failures[0]["step_id"], starts[0]["step_id"])
            self.assertEqual(failures[0]["action"], starts[0]["action"])
            self.assertIn("aborted", failures[0]["error"])
            # Live only: a superseded execution must not write a durable step row.
            self.assertEqual(harness.agent.persist_step.await_count, 0)

    async def test_13_approval_gate_does_not_consume_a_decision_it_lost_ownership_of(self) -> None:
            """The decision read is an await, so the check cannot sit only before it.

            Consuming the decision anyway would clear the approval keys and, on the
            rejection path, mark the invoice skipped in Neon -- for a run this execution no
            longer owns.
            """
            harness = _LoopHarness([_text_message("still working")])
            agent = harness.agent
            redis = _unconfigured_redis()
            redis.is_configured = True
            redis.execute_command = AsyncMock(return_value=True)
            redis.set_approval_pending = AsyncMock(return_value=True)
            redis.clear_approval = AsyncMock(return_value=True)
            redis.set_session_state = AsyncMock(return_value=True)

            # A decision carrying the nonce the request actually issued. A wrong
            # nonce would be discarded by the fail-closed check below regardless of
            # ownership, so the test would still pass with the post-read guard
            # removed -- it would prove nothing about the guard.
            issued: Dict[str, Any] = {}

            async def _capture(_run_id: str, approval_data: Dict[str, Any]) -> bool:
                issued.update(approval_data)
                return True

            redis.set_approval_pending = AsyncMock(side_effect=_capture)

            async def _decide(run_id: str) -> Optional[Dict[str, Any]]:
                agent._mark_run_lease_lost()
                return {"nonce": issued.get("nonce"), "decision": "approved"}

            redis.get_approval_decision_record = AsyncMock(side_effect=_decide)
            agent.get_redis = AsyncMock(return_value=redis)

            outcome = await asyncio.wait_for(
                agent.handle_approval_gate(
                    vendor="Acme", amount=60000.0, invoice_id="inv-1", po_number="PO-1"
                ),
                timeout=20.0,
            )

            self.assertEqual(outcome, "lease_lost")
            self.assertTrue(issued.get("nonce"), "the decision's nonce must be the issued one")
            redis.clear_approval.assert_not_awaited()
            self.assertEqual(agent.update_run_status.await_count, 1)
            self.assertEqual(agent.persist_step.await_count, 1)

class TestBackgroundTerminalWriteIsFenced(unittest.IsolatedAsyncioTestCase):
    """backend/main.py: the fallback terminal write must respect the run lease.

    ``_mark_agent_run_terminal`` is reached only when the execution produced no result
    of its own -- a cancellation, or an exception out of ``agent.run`` -- and an expired
    lease does not stop its former holder. Without a fence on that write, the owner
    fence in ``ReActAgent.update_run_status`` is bypassed by the very path a
    superseded execution is most likely to take.
    """

    async def _mark(
        self,
        row: Dict[str, Any],
        owner_id: Optional[str],
        db_error: bool = False,
    ) -> Tuple[Dict[str, Any], _FakeHub, List[Any]]:
        from backend import main as main_module

        pool = _FakePool([row], fail_on=["UPDATE agent_runs"] if db_error else None)
        hub = _FakeHub()

        async def _get_pool() -> Any:
            return pool

        redis = MagicMock()
        redis.is_configured = False
        redis.get_session_state = AsyncMock(return_value={"run_id": row["run_id"]})
        redis.set_session_state = AsyncMock(return_value=True)

        with patch.object(main_module, "get_db_pool", new=_get_pool), patch(
            "backend.redis_client.get_redis_client", return_value=redis
        ), patch.object(main_module, "run_event_hub", hub):
            await asyncio.wait_for(
                main_module._mark_agent_run_terminal(
                    str(row["run_id"]), "failed", owner_id
                ),
                timeout=5.0,
            )
        return row, hub, redis.set_session_state.await_args_list

    async def test_01_a_newer_owner_is_not_overwritten(self) -> None:
        """The whole point: a superseded execution must not stamp 'failed'.

        The Redis snapshot is asserted too, not just the row and the hub.
        set_session_state replaces the status outright, so a fenced-out write that
        still reached it would hand a client reading the mirror exactly the status
        the durable store just refused.
        """
        row, hub, redis_writes = await self._mark(
            _row(
                status="running",
                owner_id="owner-new",
                lease_expires_at=_live(),
                attempt=1,
            ),
            owner_id="owner-old",
        )

        self.assertEqual(row["status"], "running")
        self.assertEqual(row["owner_id"], "owner-new")
        self.assertEqual(hub.published, [], "a fenced-out write has no status to announce")
        self.assertEqual(redis_writes, [], "a fenced-out write must not touch the snapshot")

    async def test_02_a_landed_write_hands_the_lease_back(self) -> None:
        """A 'failed' row that still holds its lease refuses a retry for the full window."""
        row, hub, redis_writes = await self._mark(
            _row(status="running", owner_id="owner-me", lease_expires_at=_live(), attempt=1),
            owner_id="owner-me",
        )

        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["owner_id"])
        self.assertIsNone(row["lease_expires_at"])
        self.assertEqual([e[1]["status"] for e in hub.published], ["failed"])
        self.assertEqual(len(redis_writes), 1, "a landed write still mirrors to Redis")

    async def test_03_cancelled_before_the_claim_still_records_failed(self) -> None:
        """A NULL owner is writable, or a cancellation racing the claim strands the run."""
        row, _, _ = await self._mark(
            _row(status="running", owner_id=None, lease_expires_at=None, attempt=0),
            owner_id="owner-me",
        )

        self.assertEqual(row["status"], "failed")

    async def test_04_no_agent_never_touches_a_live_lease(self) -> None:
        """No agent means no claim, so this request has no standing on the row at all.

        The NULL-expiry shape is here because it is the one the SQL treats less
        obviously: `lease_expires_at <= now()` against NULL is NULL, which is not
        true, so Postgres refuses the row rather than reading the absent expiry as
        "no lease". The fake has to agree or this test would pass against a guard
        Postgres does not enforce.
        """
        for lease_expiry in (_live(), None):
            with self.subTest(lease_expires_at=lease_expiry):
                row, hub, redis_writes = await self._mark(
                    _row(
                        status="running",
                        owner_id="owner-somebody-else",
                        lease_expires_at=lease_expiry,
                        attempt=1,
                    ),
                    owner_id=None,
                )

                self.assertEqual(row["status"], "running")
                self.assertEqual(row["owner_id"], "owner-somebody-else")
                self.assertEqual(hub.published, [])
                self.assertEqual(redis_writes, [])

    async def test_05_no_agent_may_stamp_an_unowned_row(self) -> None:
        """Pre-existing behaviour, pinned: an unowned row is still ours to fail."""
        row, _, redis_writes = await self._mark(
            _row(status="running", owner_id=None, lease_expires_at=None, attempt=0),
            owner_id=None,
        )

        self.assertEqual(row["status"], "failed")
        self.assertEqual(len(redis_writes), 1)

    async def test_06_a_database_error_still_reaches_redis(self) -> None:
        """The fallback that must survive the fence: nothing is known about the row.

        A database error says nothing about who owns the run, so the read model gets
        the best information available. This is distinct from a fenced-out write, where
        the newer owner's state is known to be correct, and it is the contract
        test_issue29_nonblocking pins for a stalled database.
        """
        row, hub, redis_writes = await self._mark(
            _row(status="running", owner_id="owner-me", lease_expires_at=_live(), attempt=1),
            owner_id="owner-me",
            db_error=True,
        )

        self.assertEqual(row["status"], "running", "the injected error changed no row")
        self.assertEqual(len(redis_writes), 1)
        self.assertEqual(redis_writes[0].args[1]["status"], "failed")
        self.assertEqual(hub.published, [], "nothing reached the durable store to announce")

    # --- through _execute_agent_run_background, where the owner id has to be passed ---

    def _browser(self) -> Any:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session(*args: Any, **kwargs: Any) -> Any:
            yield MagicMock(page=MagicMock())

        return _session

    async def _drive_exception_path(
        self, row_owner: str, agent_owner: str
    ) -> Tuple[Dict[str, Any], _FakeHub]:
        """Make agent.run raise, with the agent's real lease identity wired through."""
        from backend import main as main_module

        run_id = str(uuid.uuid4())
        row = _row(
            status="running",
            owner_id=row_owner,
            lease_expires_at=_live(),
            attempt=1,
            run_id=run_id,
        )
        pool = _FakePool([row])
        hub = _FakeHub()
        agent = _agent(pool, run_id)
        agent._run_owner_id = agent_owner

        async def _boom(goal: str) -> Dict[str, Any]:
            raise RuntimeError("persisted step write failed")

        agent.run = _boom

        async def _get_pool() -> Any:
            return pool

        redis = MagicMock()
        redis.is_configured = False
        redis.get_session_state = AsyncMock(return_value={"run_id": run_id})
        redis.set_session_state = AsyncMock(return_value=True)

        with patch.object(main_module, "get_db_pool", new=_get_pool), patch(
            "backend.redis_client.get_redis_client", return_value=redis
        ), patch.object(
            main_module, "run_event_hub", hub
        ), patch(
            "backend.browser.get_browser_session", new=self._browser()
        ), patch(
            "backend.agent.ReActAgent", new=lambda **kwargs: agent
        ), patch.object(
            main_module, "TERMINAL_WRITE_IO_TIMEOUT_SECONDS", 5.0
        ):
            with self.assertLogs("hulchul.backend", level="ERROR"):
                await asyncio.wait_for(
                    main_module._execute_agent_run_background(run_id, "Process invoices"),
                    timeout=10.0,
                )
        return row, hub

    async def test_07_an_exception_does_not_stomp_the_new_owner(self) -> None:
        row, hub = await self._drive_exception_path("owner-new", "owner-old")

        self.assertEqual(
            row["status"],
            "running",
            "the replacement owner's run must survive the superseded execution's exception",
        )
        self.assertEqual(row["owner_id"], "owner-new")
        self.assertEqual(hub.published, [])

    async def test_08_an_exception_under_our_own_lease_still_fails_the_run(self) -> None:
        """The fence must not swallow the write it exists to allow."""
        row, hub = await self._drive_exception_path("owner-me", "owner-me")

        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["owner_id"], "a terminal row must not keep a live lease")
        self.assertEqual([e[1]["status"] for e in hub.published], ["failed"])

    async def test_09_both_terminal_arms_forward_the_lease_owner(self) -> None:
        """Pins the call sites, not just the helper: `None` is a silent unfenced write."""
        from backend import main as main_module

        seen: List[Tuple[str, Optional[str]]] = []

        async def _capture(run_id_str: str, status: str, owner_id: Optional[str] = None) -> None:
            seen.append((status, owner_id))

        run_id = str(uuid.uuid4())
        pool = _FakePool([])
        agent = _agent(pool, run_id)
        agent._run_owner_id = "owner-me"

        async def _boom(goal: str) -> Dict[str, Any]:
            raise RuntimeError("boom")

        async def _get_pool() -> Any:
            return pool

        started = asyncio.Event()

        async def _hang(goal: str) -> Dict[str, Any]:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        with patch.object(main_module, "get_db_pool", new=_get_pool), patch(
            "backend.redis_client.get_redis_client", return_value=MagicMock(
                is_configured=False, get_session_state=AsyncMock(return_value=None)
            )
        ), patch("backend.browser.get_browser_session", new=self._browser()), patch(
            "backend.agent.ReActAgent", new=lambda **kwargs: agent
        ), patch.object(
            main_module, "_mark_agent_run_terminal", new=_capture
        ):
            # Exception arm.
            agent.run = _boom
            await asyncio.wait_for(
                main_module._execute_agent_run_background(run_id, "Process invoices"),
                timeout=10.0,
            )

            # Cancellation arm.
            agent.run = _hang
            task = asyncio.create_task(
                main_module._execute_agent_run_background(run_id, "Process invoices")
            )
            await asyncio.wait_for(started.wait(), timeout=5.0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10.0)

        self.assertEqual(seen, [("failed", "owner-me"), ("failed", "owner-me")])


class TestStartupReconciliationHook(unittest.IsolatedAsyncioTestCase):
    """backend/main.py: reconciliation must run after pool init and never block boot."""

    async def _run_lifespan(self, reconcile: Any, get_pool: Any) -> List[str]:
        from backend import main as main_module

        order: List[str] = []

        async def _pool():
            order.append("pool")
            return MagicMock()

        async def _reconcile(pool: Any) -> List[str]:
            order.append("reconcile")
            return await reconcile(pool)

        with patch.object(main_module, "init_db_pool", side_effect=_pool), patch.object(
            main_module, "get_db_pool", new=_pool
        ), patch.object(main_module, "reconcile_orphaned_agent_runs", new=_reconcile), patch.object(
            main_module, "close_db_pool", new=_noop
        ):
            cm = main_module.lifespan(main_module.app)
            await cm.__aenter__()
            await cm.__aexit__(None, None, None)
        return order

    async def test_01_reconciliation_runs_after_pool_init(self) -> None:
        async def _reconcile(pool: Any) -> List[str]:
            return []

        order = await self._run_lifespan(_reconcile, None)
        self.assertIn("reconcile", order)
        self.assertLess(order.index("pool"), order.index("reconcile"))

    async def test_02_reconcile_failure_does_not_block_startup(self) -> None:
        from backend import main as main_module

        async def _boom(pool: Any) -> List[str]:
            raise RuntimeError("database unreachable")

        with patch.object(main_module, "init_db_pool", new=_noop), patch.object(
            main_module, "get_db_pool", new=_noop
        ), patch.object(main_module, "reconcile_orphaned_agent_runs", new=_boom), patch.object(
            main_module, "close_db_pool", new=_noop
        ):
            cm = main_module.lifespan(main_module.app)
            await cm.__aenter__()
            await cm.__aexit__(None, None, None)

    async def test_03_pool_init_failure_does_not_block_startup(self) -> None:
        from backend import main as main_module

        async def _boom() -> Any:
            raise RuntimeError("neon unreachable")

        async def _reconcile(pool: Any) -> List[str]:
            raise AssertionError("reconciliation must not run without a pool")

        with patch.object(main_module, "init_db_pool", new=_boom), patch.object(
            main_module, "get_db_pool", new=_boom
        ), patch.object(main_module, "reconcile_orphaned_agent_runs", new=_reconcile), patch.object(
            main_module, "close_db_pool", new=_noop
        ):
            cm = main_module.lifespan(main_module.app)
            await cm.__aenter__()
            await cm.__aexit__(None, None, None)

    async def test_04_a_hanging_sweep_cannot_stop_the_app_from_serving(self) -> None:
        """`except` cannot catch a hang, and this runs before `yield`.

        The pool has no command_timeout and acquire() is called without one, so a
        connection that is accepted and then never answers parks the sweep forever.
        The comment above the hook claims a failed sweep cannot stop startup; a sweep
        that never returns is the case that claim missed.
        """
        from backend import main as main_module

        async def _never_returns(pool: Any) -> List[str]:
            await asyncio.Event().wait()
            return []  # pragma: no cover - unreachable

        with patch.object(main_module, "STARTUP_RECONCILE_TIMEOUT_SECONDS", 0.05), patch.object(
            main_module, "init_db_pool", new=_noop
        ), patch.object(main_module, "get_db_pool", new=_noop), patch.object(
            main_module, "reconcile_orphaned_agent_runs", new=_never_returns
        ), patch.object(main_module, "close_db_pool", new=_noop):
            cm = main_module.lifespan(main_module.app)
            # Reached only if the sweep was bounded; without the ceiling this raises
            # TimeoutError and the app never serves a request.
            await asyncio.wait_for(cm.__aenter__(), timeout=5.0)
            await cm.__aexit__(None, None, None)

    async def test_05_a_completed_sweep_is_not_cut_short(self) -> None:
        """The ceiling is a ceiling, not a replacement timeout for working sweeps."""
        async def _reconcile(pool: Any) -> List[str]:
            await asyncio.sleep(0)
            return ["run-a"]

        order = await self._run_lifespan(_reconcile, None)

        self.assertIn("reconcile", order)


class TestRunEndpointDoesNotStealRunStatus(unittest.IsolatedAsyncioTestCase):
    """backend/main.py: the pre-dispatch upsert must not touch an existing row.

    run_agent_endpoint writes agent_runs before the background task claims the lease, so
    it cannot know whether this request will be accepted. `DO UPDATE SET status =
    'running'` therefore stamped 'running' on a run whose claim was about to be refused,
    and reset a live owner's 'paused' / 'awaiting_approval' back to 'running' on every
    duplicate request -- a status that owner never re-reads.
    """

    def _endpoint_run_sql(self) -> str:
        """The agent_runs INSERT literal inside run_agent_endpoint, comments excluded."""
        from backend import main as main_module

        source = Path(main_module.__file__).read_text(encoding="utf-8")
        start = source.index("async def run_agent_endpoint")
        body = source[start:source.index("\n@app.", start)]
        # The statement is a triple-quoted literal; slicing it out keeps the
        # surrounding explanatory comments out of the assertions. The endpoint's
        # docstring is also a triple-quoted literal, so pick the one that is SQL.
        sql_literals = [
            m.group(1) for m in re.finditer(r'"""(.*?)"""', body, re.DOTALL)
        ]
        matches = [s for s in sql_literals if "INSERT INTO agent_runs" in s]
        assert len(matches) == 1, (
            f"expected exactly one agent_runs INSERT literal, found {len(matches)}"
        )
        return matches[0]

    async def test_01_the_endpoint_upsert_never_updates_an_existing_row(self) -> None:
        sql = self._endpoint_run_sql()

        self.assertIn("ON CONFLICT (run_id) DO NOTHING", sql)
        self.assertNotIn("DO UPDATE", sql)

    async def test_02_the_endpoint_still_creates_a_missing_row(self) -> None:
        """The insert itself is still needed: the SSE stream replays agent_runs on connect."""
        sql = self._endpoint_run_sql()

        self.assertIn("INSERT INTO agent_runs", sql)

    async def test_03_the_redis_seed_is_gated_on_the_row_actually_being_created(self) -> None:
        """set_session_state is a plain SET, so it must not run for an existing row.

        Seeding unconditionally replaced a live run's whole snapshot -- status back to
        'running' and step index back to 0 -- on every duplicate request, from any
        process, while the Postgres row the agent and the SSE poll read kept its real
        status.

        Driven through the endpoint for both command tags rather than asserted on the
        source: 'INSERT 0 0' is what a duplicate request gets, and only 'INSERT 0 1'
        means this request created the row.
        """
        from backend import main as main_module

        for tag, expect_seeded in (("INSERT 0 1", True), ("INSERT 0 0", False)):
            with self.subTest(command_tag=tag):
                mock_conn = AsyncMock()
                mock_conn.execute.return_value = tag
                cm = AsyncMock()
                cm.__aenter__.return_value = mock_conn
                cm.__aexit__.return_value = False
                pool = MagicMock()
                pool.acquire.return_value = cm

                async def _get_pool() -> MagicMock:
                    return pool

                mock_redis = AsyncMock()
                mock_redis.set_session_state.return_value = True

                async def _background(run_id_str: str, goal: str) -> None:
                    return None

                main_module.app.dependency_overrides[_require_session_dep] = (
                    _operator_session
                )
                try:
                    with patch.object(main_module, "get_db_pool", new=_get_pool), patch(
                        "backend.redis_client.get_redis_client", return_value=mock_redis
                    ), patch.object(
                        main_module, "_execute_agent_run_background", new=_background
                    ):
                        transport = ASGITransport(app=main_module.app)
                        async with AsyncClient(
                            transport=transport, base_url="http://test"
                        ) as ac:
                            resp = await ac.post(
                                "/agent/run",
                                json={"goal": "Process all pending invoices"},
                            )
                finally:
                    main_module.app.dependency_overrides.pop(_require_session_dep, None)
                    main_module._reserved_agent_runs.clear()
                    main_module._active_agent_tasks.clear()

                self.assertEqual(resp.status_code, 202)
                self.assertEqual(
                    mock_redis.set_session_state.await_count > 0,
                    expect_seeded,
                    f"{tag} should {'seed' if expect_seeded else 'not seed'} the snapshot",
                )


async def _operator_session() -> Session:
    """Bypass the auth guard: the endpoint's behaviour here is not about sessions."""
    return Session(sub="operator", exp=9999999999)


async def _noop(*args: Any, **kwargs: Any) -> Any:
    return None


class TestRunLeaseConfiguration(unittest.TestCase):
    def test_01_lease_must_exceed_pause_timeout(self) -> None:
        with self.assertRaises(ValueError):
            validate_run_lease_settings(300.0, 60.0, 300.0)
        with self.assertRaises(ValueError):
            validate_run_lease_settings(120.0, 60.0, 300.0)

    def test_02_shipped_defaults_are_valid(self) -> None:
        validate_run_lease_settings(
            settings.RUN_LEASE_SECONDS,
            settings.RUN_HEARTBEAT_SECONDS,
            settings.PAUSE_TIMEOUT_SECONDS,
        )
        self.assertGreater(settings.RUN_LEASE_SECONDS, settings.PAUSE_TIMEOUT_SECONDS)

    def test_03_heartbeat_must_be_shorter_than_the_lease(self) -> None:
        with self.assertRaises(ValueError):
            validate_run_lease_settings(900.0, 900.0, 300.0)
        with self.assertRaises(ValueError):
            validate_run_lease_settings(900.0, 0.0, 300.0)

    def test_04_env_example_documents_both_keys(self) -> None:
        path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("RUN_LEASE_SECONDS", content)
        self.assertIn("RUN_HEARTBEAT_SECONDS", content)

    def test_05_non_finite_timings_are_rejected(self) -> None:
        """NaN and Infinity slip through comparison-only checks.

        `nan <= x` and `nan >= x` are both False, so every inequality in
        validate_run_lease_settings passes and the bad value only surfaces later as a
        Postgres error on `make_interval(secs => 'NaN')` -- on the first run claim,
        mid-flight, rather than at boot.
        """
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    validate_run_lease_settings(bad, 60.0, 300.0)
                with self.assertRaises(ValueError):
                    validate_run_lease_settings(900.0, bad, 300.0)


class TestLeaseDdl(unittest.TestCase):
    """Guards the CREATE TABLE IF NOT EXISTS is-a-no-op trap.

    agent_runs already exists in every deployed environment, so the lease columns
    only reach production if init_db.py adds them with ALTER TABLE. There is no
    migration framework in this repo -- init_db.py IS the migration surface.
    """

    def _init_db_source(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", "init_db.py")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_01_lease_columns_added_via_alter_table(self) -> None:
        source = self._init_db_source()
        for column, col_type in (
            ("owner_id", "TEXT"),
            ("lease_expires_at", "TIMESTAMPTZ"),
            ("attempt", "INT NOT NULL DEFAULT 0"),
        ):
            with self.subTest(column=column):
                self.assertIn(
                    f"ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS {column} {col_type}",
                    source,
                )

    def test_02_lease_columns_not_inline_in_create_table(self) -> None:
        source = self._init_db_source()
        create_table = source.split("CREATE TABLE IF NOT EXISTS agent_runs (", 1)[1].split(");", 1)[0]
        for column in ("owner_id", "lease_expires_at", "attempt"):
            with self.subTest(column=column):
                self.assertNotIn(column, create_table)

    def test_03_status_lease_index_exists(self) -> None:
        self.assertIn(
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_status_lease ON agent_runs(status, lease_expires_at)",
            self._init_db_source(),
        )


if __name__ == "__main__":
    unittest.main()
