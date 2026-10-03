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
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agent import ReActAgent
from backend.config import settings, validate_run_lease_settings
from backend.run_lease import (
    ORPHANED_RUN_STATUS,
    ORPHANED_STEP_ACTION,
    TERMINAL_RUN_STATUSES,
    claim_run_lease,
    reconcile_orphaned_agent_runs,
    release_run_lease,
    renew_run_lease,
)
from backend.tools import PlaywrightTools

# Guards that appear in the conditional statements this suite exercises. The fake
# connection below evaluates them, so a run that must not be touched cannot be
# touched even if a caller offered it as a candidate.
STATUS_GUARD = "status = 'running'"
LEASE_FREE_GUARD = "lease_expires_at IS NULL OR lease_expires_at < now()"
# Anchored on AND so it only ever matches a WHERE clause. The bare "owner_id = $2"
# also appears in claim_run_lease's SET list, which would make the fake demand that
# an unowned row already be owned before it can be claimed.
OWNER_SCOPED_GUARD = "AND owner_id = $2"
UPSERT_LEASE_GUARD = "agent_runs.lease_expires_at IS NULL"
UPSERT_EXPIRED_GUARD = "agent_runs.lease_expires_at <= now()"
UPSERT_SAME_OWNER_GUARD = "agent_runs.owner_id = EXCLUDED.owner_id"


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

        # The row-lock probe issued before the claim. Returns the row as it stands
        # so a caller can see what it locked; it changes nothing.
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
            # Deliberately over-broad: returns every seeded row regardless of the
            # WHERE clause. That is what makes the CAS in reconcile_orphaned_agent_runs
            # the load-bearing protection -- if the sweep ever lost its status guard,
            # a paused or awaiting_approval run would be failed by these rows.
            return [dict(r) for r in self._pool.rows]
        return []

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

    def acquire(self) -> _FakeAcquire:
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
        if OWNER_SCOPED_GUARD in query and len(args) > 1:
            if row.get("owner_id") != args[1]:
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


class TestRunLeaseClaim(unittest.IsolatedAsyncioTestCase):
    """claim_run_lease: the property that makes a duplicate request harmless."""

    async def test_01_claim_succeeds_on_fresh_running_row(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", run_id=run_id)])

        result = await claim_run_lease(pool, run_id, "owner-a", 900.0)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.owner_id, "owner-a")
        self.assertEqual(result.attempt, 1)
        self.assertEqual(pool.row(run_id)["owner_id"], "owner-a")

    async def test_02_claim_refused_while_lease_is_unexpired(self) -> None:
        """The 'retries cannot double-run' property.

        A refused claim must not transfer ownership, and the statement it issued must
        still carry the expiry guard -- that guard is what makes the refusal real.
        """
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="owner-a", lease_expires_at=_live(), run_id=run_id)])

        result = await claim_run_lease(pool, run_id, "owner-b", 900.0)

        self.assertIsNone(result)
        self.assertEqual(pool.row(run_id)["owner_id"], "owner-a", "ownership was stolen")
        self.assertEqual(pool.row(run_id)["attempt"], 0, "attempt incremented on a refused claim")

        claims = pool.find("UPDATE agent_runs")
        self.assertEqual(len(claims), 1)
        self.assertIn(LEASE_FREE_GUARD, claims[0][0])
        self.assertIn(STATUS_GUARD, claims[0][0])

    async def test_03_claim_refused_for_terminal_row(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="done", owner_id="owner-a", lease_expires_at=None, run_id=run_id)])

        result = await claim_run_lease(pool, run_id, "owner-b", 900.0)

        self.assertIsNone(result)
        self.assertEqual(pool.row(run_id)["owner_id"], "owner-a")

    async def test_04_claim_takes_expired_lease_and_increments_attempt(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="dead-owner", lease_expires_at=_expired(), attempt=3, run_id=run_id)])

        result = await claim_run_lease(pool, run_id, "owner-b", 900.0)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.attempt, 4, "reclaim must leave a durable trail of owners")
        self.assertEqual(pool.row(run_id)["owner_id"], "owner-b")
        self.assertIn("attempt = agent_runs.attempt + 1", pool.find("UPDATE agent_runs")[0][0])

    async def test_05_two_concurrent_claims_exactly_one_wins(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", run_id=run_id)])

        first, second = await asyncio.gather(
            claim_run_lease(pool, run_id, "owner-a", 900.0),
            claim_run_lease(pool, run_id, "owner-b", 900.0),
        )

        winners = [r for r in (first, second) if r is not None]
        self.assertEqual(len(winners), 1, "exactly one claimant may take a free lease")
        self.assertEqual(pool.row(run_id)["attempt"], 1)
        self.assertIn(pool.row(run_id)["owner_id"], {"owner-a", "owner-b"})

    async def test_06_claim_refused_for_malformed_run_id(self) -> None:
        pool = _FakePool([])
        self.assertIsNone(await claim_run_lease(pool, "not-a-uuid", "owner-a", 900.0))
        self.assertEqual(pool.executed, [], "a malformed id must never reach the database")

    async def test_07_claim_locks_the_row_before_claiming(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", run_id=run_id)])

        await claim_run_lease(pool, run_id, "owner-a", 900.0)

        locks = pool.find("FOR UPDATE SKIP LOCKED")
        self.assertEqual(len(locks), 1)
        self.assertLess(
            pool.executed.index(locks[0]),
            pool.executed.index(pool.find("UPDATE agent_runs")[0]),
            "the lock must be taken before the claim",
        )


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
    ) -> List[str]:
        redis = redis or _FakeRedis()
        hub = hub or _FakeHub()
        with patch("backend.run_lease.get_redis_client", return_value=redis), patch(
            "backend.main.run_event_hub", hub
        ):
            return await reconcile_orphaned_agent_runs(pool)

    async def test_01_sweeps_expired_running_row_and_records_the_reason(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", owner_id="dead", lease_expires_at=_expired(), run_id=run_id)])

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
        expired_running = _row(status="running", owner_id="dead", lease_expires_at=_expired())
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
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), run_id=run_id)])
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
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), run_id=run_id)])
        hub = _FakeHub()

        reclaimed = await self._reconcile(pool, _FakeRedis(fail=True), hub)

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertEqual(len(hub.published), 1, "a Redis outage must not stop the SSE publish")

    async def test_07_survives_hub_failure_and_still_mirrors_to_redis(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="running", lease_expires_at=_expired(), run_id=run_id)])
        redis = _FakeRedis()

        reclaimed = await self._reconcile(pool, redis, _FakeHub(fail=True))

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertIn(run_id, redis.states)

    async def test_08_survives_step_insert_failure(self) -> None:
        run_id = str(uuid.uuid4())
        pool = _FakePool(
            [_row(status="running", lease_expires_at=_expired(), run_id=run_id)],
            fail_on=["INSERT INTO agent_steps"],
        )
        redis, hub = _FakeRedis(), _FakeHub()

        reclaimed = await self._reconcile(pool, redis, hub)

        self.assertEqual(reclaimed, [run_id])
        self.assertEqual(pool.row(run_id)["status"], ORPHANED_RUN_STATUS)
        self.assertEqual(len(hub.published), 1)

    async def test_09_primary_db_write_failure_reclaims_nothing(self) -> None:
        pool = _FakePool(
            [_row(status="running", lease_expires_at=_expired())],
            fail_on=["SET status = $2"],
        )
        with self.assertLogs("backend.run_lease", level="ERROR"):
            self.assertEqual(await self._reconcile(pool), [])

    async def test_10_candidate_enumeration_failure_reclaims_nothing(self) -> None:
        pool = _FakePool([_row(status="running", lease_expires_at=_expired())], fail_on=["SELECT run_id"])
        with self.assertLogs("backend.run_lease", level="ERROR"):
            self.assertEqual(await self._reconcile(pool), [])

    async def test_11_uses_existing_terminal_status(self) -> None:
        """A novel status would leave the frontend's terminal set hanging."""
        self.assertIn(ORPHANED_RUN_STATUS, TERMINAL_RUN_STATUSES)
        self.assertEqual(ORPHANED_RUN_STATUS, "failed")


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

        from backend.run_lease import run_lease_heartbeat

        run_id = str(uuid.uuid4())
        pool = _FakePool([_row(status="done", owner_id="someone-else", lease_expires_at=_live(), run_id=run_id)])

        task = asyncio.create_task(
            run_lease_heartbeat(pool, run_id, "owner-a", 0.01, 900.0)
        )
        await asyncio.wait_for(task, timeout=2.0)

        self.assertEqual(pool.row(run_id)["owner_id"], "someone-else")


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