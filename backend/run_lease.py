"""
Durable ownership of accepted agent runs (issue #56).

Without an owner, a run that was left in ``status = 'running'`` is indistinguishable
from a run that is genuinely progressing: the in-process task that used to own it is
gone (Lambda freeze/thaw, container replacement) and nothing is left to finish it.
This module gives a run a lease with a named owner and an expiry in Postgres, so
"who is executing this" is durable state rather than a process-local dict, and so an
interrupted run can be identified and reclaimed on the next startup.

Design notes:

* The lease lives in **Postgres**, not Redis. ``UpstashRedisClient.execute_command``
  issues exactly one Redis command per HTTP POST (backend/redis_client.py), so there
  is no MULTI/EXEC/WATCH/EVAL and therefore no compare-and-swap primitive to build a
  lease on. Postgres gives row locks and conditional UPDATE, which is what the claim
  actually needs.
* Every mutation is a single conditional statement, so ownership transfer is decided
  by the database rather than by a read-then-write race in Python.
* ``reconcile_orphaned_agent_runs`` deliberately only sweeps ``status = 'running'``.
  A run in ``paused`` or ``awaiting_approval`` is waiting on a human, not orphaned,
  and failing it would destroy a run the operator is still in the middle of.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

import asyncpg

from backend.redis_client import get_redis_client

logger = logging.getLogger(__name__)

# Statuses from which no further execution happens, i.e. the lease can be handed back.
# This is deliberately a superset of the frontend's terminal set {done, failed,
# session_lost}: the agent also parks runs in 'completed' and 'stalled', and for
# ownership purposes those are terminal too.
TERMINAL_RUN_STATUSES = frozenset(
    {"done", "failed", "completed", "session_lost", "stalled"}
)

# Statuses from which execution may still continue, i.e. the owner still holds the
# lease and the heartbeat must keep renewing it.
#
# This is NOT simply the complement of TERMINAL_RUN_STATUSES: a run parks itself in
# 'paused' (up to PAUSE_TIMEOUT_SECONDS) and 'awaiting_approval' (up to
# APPROVAL_TIMEOUT_SECONDS) while it is very much alive and waiting on a human.
# Renewal has to match those states too. Scoping renewal to 'running' alone made the
# heartbeat treat a legitimate pause as a lost lease, stop for good, and leave the
# resumed run executing with a dead lease -- at which point a duplicate request could
# claim the run and double-run it, which is the precise failure this module exists to
# prevent.
ACTIVE_RUN_STATUSES = frozenset({"running", "paused", "awaiting_approval"})

assert not (ACTIVE_RUN_STATUSES & TERMINAL_RUN_STATUSES), (
    "a status cannot be both terminal and still-reachable; "
    "a run marked terminal must have its lease released, not renewed"
)

# Upper bound on how many orphans a single startup sweep will reclaim, so a database
# that has been unreachable for a while cannot turn cold start into a long write
# burst on the first warm invocation. Reclaiming continues for up to
# DEFAULT_RECONCILE_MAX_BATCHES full batches so a backlog larger than one batch is not
# deferred to the next cold start.
DEFAULT_RECONCILE_LIMIT = 50
DEFAULT_RECONCILE_MAX_BATCHES = 10

# The status an orphan is moved to. Reusing an existing terminal value keeps the
# frontend's terminal set intact; the specific cause is recorded as an 'orphaned'
# agent_steps row instead of inventing a status the UI would never handle.
ORPHANED_RUN_STATUS = "failed"
ORPHANED_STEP_ACTION = "orphaned"

_UUID_MISSING = object()


def _coerce_run_uuid(run_id: Any) -> Any:
    """Return a UUID for a run id, or ``_UUID_MISSING`` when it is not one.

    A malformed id must never reach the database: ``uuid.UUID`` raises rather than
    letting Postgres raise a less obvious error from inside a multi-statement flow.
    """
    try:
        return uuid.UUID(str(run_id))
    except (ValueError, AttributeError, TypeError):
        return _UUID_MISSING


def _lease_interval_expr() -> str:
    """SQL expression turning a float-seconds parameter into an interval."""
    # make_interval(secs => $n) rather than "$n::interval": a bare float is not a
    # valid interval literal in Postgres ('900'::interval raises), so the cast form
    # in the design notes does not actually parse.
    return "make_interval(secs => $3::double precision)"


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a successful run-lease claim."""

    run_id: str
    status: str
    owner_id: Optional[str]
    attempt: Optional[int]
    lease_expires_at: Optional[datetime]


async def claim_run_lease(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    lease_seconds: float,
) -> Optional[ClaimResult]:
    """Atomically take ownership of a run that is already recorded as running.

    Returns ``None`` when the claim is refused, which is the normal outcome for a
    retry or a duplicate request: refusing is what stops two executions from driving
    the same run_id. A claim is granted only when all of the following hold, decided
    by the database in one statement:

    * the row is in ``status = 'running'``;
    * no owner currently holds a live lease (``lease_expires_at IS NULL`` or already
      past ``now()``).

    Concurrency is handled by that conditional ``UPDATE`` alone, not by a preceding
    ``SELECT ... FOR UPDATE SKIP LOCKED``. ``pool.acquire()`` is not a transaction, so
    such a ``SELECT`` autocommits and drops its row lock before the ``UPDATE`` runs and
    makes the two statements non-atomic -- the lock is released before it can exclude
    anyone. What actually serialises concurrent claimants is that a second ``UPDATE``
    blocks on the row lock the first one holds and then re-evaluates its ``WHERE``
    against the committed row, finding a live lease and matching zero rows. Issuing an
    extra statement to hold a lock that is already released would only cost a round
    trip, so it is not issued; an earlier draft's probe was inert and its docstring
    overstated what it did.

    On success ``attempt`` is incremented so a run that keeps getting reclaimed leaves
    a durable trail of how many owners it has had.
    """
    run_uuid = _coerce_run_uuid(run_id)
    if run_uuid is _UUID_MISSING:
        logger.error(f"Refusing run lease claim for malformed run_id: {run_id!r}")
        return None

    query = f"""
        UPDATE agent_runs
        SET owner_id = $2,
            lease_expires_at = now() + {_lease_interval_expr()},
            attempt = agent_runs.attempt + 1
        WHERE run_id = $1
          AND status = 'running'
          AND (lease_expires_at IS NULL OR lease_expires_at < now())
        RETURNING run_id, status, owner_id, attempt, lease_expires_at
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, run_uuid, owner_id, float(lease_seconds))

    if row is None:
        return None

    return ClaimResult(
        run_id=str(row["run_id"]),
        status=row["status"],
        owner_id=row["owner_id"],
        attempt=row["attempt"],
        lease_expires_at=row["lease_expires_at"],
    )


async def renew_run_lease(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    lease_seconds: float,
) -> bool:
    """Extend a lease the caller already owns. Owner-scoped.

    Matches every status from which execution can still continue
    (``ACTIVE_RUN_STATUSES``), not just ``running``: a run that is parked in
    ``paused`` or ``awaiting_approval`` is still the owner's, and letting its lease
    lapse there would hand the run to a duplicate execution the moment it resumed.

    Terminal statuses are deliberately excluded, so a finished run never holds a live
    lease and stays claimable by the resume/recovery path.

    Returns ``False`` when the caller no longer owns the run (it was reclaimed, or the
    run finished), so the heartbeat can stop instead of resurrecting a lease it lost.
    """
    run_uuid = _coerce_run_uuid(run_id)
    if run_uuid is _UUID_MISSING:
        logger.error(f"Refusing run lease renewal for malformed run_id: {run_id!r}")
        return False

    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""
            UPDATE agent_runs
            SET lease_expires_at = now() + {_lease_interval_expr()}
            WHERE run_id = $1
              AND owner_id = $2
              AND status = ANY($4::text[])
            """,
            run_uuid,
            owner_id,
            float(lease_seconds),
            sorted(ACTIVE_RUN_STATUSES),
        )

    # asyncpg returns the affected row count as a command tag string.
    return affected_rows(result) > 0


async def release_run_lease(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
) -> None:
    """Hand a lease back so the run can be claimed or reclaimed. Owner-scoped.

    Scoping the UPDATE to ``owner_id`` is what stops a stale owner that woke up late
    from clearing the lease a newer owner is actively holding.
    """
    run_uuid = _coerce_run_uuid(run_id)
    if run_uuid is _UUID_MISSING:
        logger.error(f"Refusing run lease release for malformed run_id: {run_id!r}")
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs
            SET owner_id = NULL,
                lease_expires_at = NULL
            WHERE run_id = $1
              AND owner_id = $2
            """,
            run_uuid,
            owner_id,
        )


def affected_rows(command_tag: Any) -> int:
    """Extract the affected row count from an asyncpg command tag.

    ``execute`` reports "UPDATE 3"; anything unparseable counts as zero, which is the
    fail-safe direction for every caller: a lease we cannot prove we still hold is
    treated as lost.
    """
    try:
        return int(str(command_tag).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def run_lease_heartbeat(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    interval_seconds: float,
    lease_seconds: float,
    owner_task: Optional[asyncio.Task] = None,
    on_ownership_lost: Optional[Callable[[], None]] = None,
) -> None:
    """Renew ``run_id``'s lease every ``interval_seconds`` until cancelled.

    Runs as a sibling task of the agent loop rather than being driven from inside it.
    A single agent iteration can far exceed any naive interval -- it is a Groq call
    plus a full CDP session acquire plus a browser action -- so renewal cannot depend
    on the loop reaching a checkpoint.

    The heartbeat only lives as long as the execution task that started it. That bound
    matters: a heartbeat that outlived its run would keep renewing forever, and the
    run would then never be reclaimable, which is strictly worse than the stranded run
    this issue is about. When the owner task is done the heartbeat stops renewing and
    lets the lease lapse, which puts the run back on the reconciliation path.

    A failed renewal is logged and retried rather than raised. It cannot stop the run
    from a helper task, and a transient blip must not become an unhandled task
    exception; the lease window is sized with enough margin (see
    ``validate_run_lease_settings``) to absorb short outages.

    Retrying is bounded by that same window. A renewal that keeps *raising* -- pool
    acquire timeouts, an exhausted pool, a network partition on this host only --
    never returns zero rows, so the "lease lost" branch below is never reached and the
    retry would otherwise be unbounded. Meanwhile the lease in ``agent_runs`` expires
    ``lease_seconds`` after the last renewal that actually landed, reconciliation on
    another instance can fail the run, and a duplicate dispatch can claim it: the
    double-run this module exists to prevent, reached through the one path the
    owner-scoped fence does not cover. So the elapsed time since the last successful
    renewal is tracked on a monotonic clock and reaching ``lease_seconds`` reports
    ownership loss. The run stops instead of continuing to act on a lease it can no
    longer prove it holds -- which is the correct direction to be wrong in, since a
    premature stop leaves a resumable run while an unfenced run duplicates invoices.

    Losing the lease is reported through ``on_ownership_lost`` before the heartbeat
    returns, on either path: a renewal that matches zero rows (the run was reclaimed,
    or a newer execution claimed it) or a renewal that could not be completed inside
    the window. Stopping silently is not enough -- the owner-scoped fence in
    ``ReActAgent.update_run_status`` only drops a status write, it does not stop
    ``tools.execute``, step persistence, Redis writes or the ``done`` event, so the
    loop has to be told to unwind. The callback is invoked at most once and its own
    failure is logged rather than raised: the heartbeat is already returning.
    """
    def _report_ownership_lost() -> None:
        if on_ownership_lost is None:
            return
        try:
            on_ownership_lost()
        except Exception as cb_err:
            logger.warning(
                f"Run lease ownership-lost callback for {run_id} raised: {cb_err}"
            )

    # Monotonic, so an NTP step or a wall-clock change cannot extend or collapse the
    # window. Seeded here because the lease was claimed by ensure_run_record
    # immediately before this task was created.
    loop = asyncio.get_event_loop()
    last_renewed_at = loop.time()

    try:
        while True:
            if owner_task is not None and owner_task.done():
                logger.info(
                    f"Run lease heartbeat for {run_id} stopping: owner task finished."
                )
                return
            await asyncio.sleep(interval_seconds)
            if owner_task is not None and owner_task.done():
                logger.info(
                    f"Run lease heartbeat for {run_id} stopping: owner task finished."
                )
                return
            try:
                renewed = await renew_run_lease(pool, run_id, owner_id, lease_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Run lease heartbeat renewal failed for {run_id}: {e}")
                if loop.time() - last_renewed_at >= lease_seconds:
                    logger.error(
                        f"Run lease for {run_id} could not be renewed within its "
                        f"{lease_seconds}s window; treating ownership as lost."
                    )
                    _report_ownership_lost()
                    return
                continue
            if not renewed:
                logger.error(
                    f"Run lease heartbeat lost ownership of {run_id}; stopping renewal."
                )
                _report_ownership_lost()
                return
            last_renewed_at = loop.time()
    except asyncio.CancelledError:
        logger.info(f"Run lease heartbeat for {run_id} cancelled.")
        raise


async def reconcile_orphaned_agent_runs(
    pool: asyncpg.Pool,
    limit: int = DEFAULT_RECONCILE_LIMIT,
    reason: str = "execution environment was replaced before the run reached a terminal state",
    max_batches: int = DEFAULT_RECONCILE_MAX_BATCHES,
) -> List[str]:
    """Fail every run that no live owner is executing. Returns the reclaimed run ids.

    Sweeps in bounded batches until one comes back short, so a backlog larger than a
    single ``limit`` is drained on the same cold start instead of waiting for the next
    instance to boot. The batch count is itself capped, which keeps the guarantee that
    a database that was unreachable for a long time cannot turn a cold start into an
    unbounded write burst. A final full batch means the backlog may not be exhausted;
    that is logged rather than looped on.

    See :func:`_reconcile_batch` for what counts as a candidate and how ownership
    transfer is decided.
    """
    reclaimed: List[str] = []

    for batch_number in range(1, max_batches + 1):
        found = await _reconcile_batch(pool, limit, reason)
        reclaimed.extend(found)
        if len(found) < limit:
            return reclaimed
        logger.warning(
            f"Orphan sweep batch {batch_number} reclaimed a full {limit} run(s); "
            "continuing to the next batch."
        )

    logger.error(
        f"Orphan sweep stopped after {max_batches} batch(es) of {limit}; any runs "
        "beyond that are still 'running' and will be retried on the next cold start."
    )
    return reclaimed


async def _reconcile_batch(
    pool: asyncpg.Pool,
    limit: int,
    reason: str,
) -> List[str]:
    """Reclaim up to ``limit`` orphaned runs. Returns the ids this batch reclaimed.

    A candidate is a row in ``status = 'running'`` whose lease is NULL or expired.
    Two things are never candidates:

    * Rows in ``paused`` or ``awaiting_approval``. They are parked on a human, and
      failing them would kill a run that is still in progress from the operator's point
      of view.
    * Rows with ``attempt = 0``, i.e. anything this lease scheme has never owned. A
      row only reaches ``attempt >= 1`` by being claimed through ``ensure_run_record``,
      so ``attempt = 0`` is exactly "no lease-aware execution has ever owned this".
      During a rollout that matters: an execution started by the pre-lease code is
      still driving a browser while writing no ``owner_id`` and no
      ``lease_expires_at``, so a sweep running in a freshly deployed environment sees a
      NULL lease on a *live* run and would fail it out from under itself. Such a row is
      indistinguishable from a stranded legacy run, and guessing wrong means an
      execution keeps clicking in an ERP after its run has been declared failed, so
      the sweep declines it. It is picked up once a lease-aware execution claims it.

    Each candidate is moved to a terminal status with a conditional UPDATE carrying the
    same guards, so a run that was legitimately claimed between the SELECT and the
    UPDATE is left alone.
    The database write, the Redis mirror, the hub publish and the ``orphaned`` step
    row are each isolated: a Redis or hub outage must never stop the terminal status
    from reaching the database, or the run is stranded again -- the exact failure this
    function exists to repair.
    """
    reclaimed: List[str] = []

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id
                FROM agent_runs
                WHERE status = 'running'
                  AND attempt > 0
                  AND (lease_expires_at IS NULL OR lease_expires_at < now())
                ORDER BY created_at ASC
                LIMIT $1
                """,
                limit,
            )
    except Exception as e:
        logger.error(f"Failed to enumerate orphaned agent runs: {e}")
        return reclaimed

    candidates = [str(row["run_id"]) for row in rows]
    if not candidates:
        return reclaimed

    logger.warning(
        f"Found {len(candidates)} orphaned agent run(s) with no live lease: {candidates}"
    )

    for candidate_id in candidates:
        run_uuid = _coerce_run_uuid(candidate_id)
        if run_uuid is _UUID_MISSING:
            logger.error(f"Skipping orphaned agent run with malformed run_id: {candidate_id!r}")
            continue

        # 1. Terminal status in Postgres. Must not be blocked by the mirrors below.
        reclaimed_here = False
        try:
            async with pool.acquire() as conn:
                claimed = await conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = $2,
                        owner_id = NULL,
                        lease_expires_at = NULL
                    WHERE run_id = $1
                      AND status = 'running'
                      AND attempt > 0
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
                    """,
                    run_uuid,
                    ORPHANED_RUN_STATUS,
                )
            reclaimed_here = affected_rows(claimed) > 0
        except Exception as e:
            logger.error(f"Failed to mark orphaned agent run {candidate_id} as failed: {e}")
            continue

        if not reclaimed_here:
            # A live owner (or a concurrent sweeper) got there first.
            logger.info(
                f"Orphaned agent run {candidate_id} was claimed by a live owner; leaving it alone."
            )
            continue

        reclaimed.append(candidate_id)
        logger.error(
            f"Reclaimed orphaned agent run {candidate_id} as '{ORPHANED_RUN_STATUS}': {reason}"
        )

        # 2. Record why, as a durable step row.
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_steps (run_id, action, result, timestamp)
                    VALUES ($1, $2, $3, now())
                    """,
                    run_uuid,
                    ORPHANED_STEP_ACTION,
                    f"Run reclaimed by startup reconciliation: {reason}",
                )
        except Exception as e:
            logger.error(f"Failed to record orphaned step for agent run {candidate_id}: {e}")

        # 3. Re-read ownership before touching the read models below. The database is
        # already correct and authoritative; Redis and the hub are not. A resume or a
        # duplicate request can claim this run in the window between the UPDATE above
        # and these writes -- its upsert legitimately takes over a terminal row with a
        # NULL lease -- and `set_session_state` replaces the whole hash, so writing
        # here would overwrite the new owner's `running` snapshot with a terminal one
        # and broadcast a status_change for a run that is being executed right now.
        mirrors_suppressed = await _run_has_live_owner(pool, run_uuid)
        if mirrors_suppressed:
            logger.warning(
                f"Orphaned agent run {candidate_id} was reclaimed by a new owner before "
                "its Redis and SSE mirrors were written; leaving them to that owner."
            )
            continue

        # 4. Mirror into Redis so the session snapshot stops advertising a running run.
        try:
            redis = get_redis_client()
            await redis.set_session_state(
                candidate_id,
                {
                    "run_id": candidate_id,
                    "status": ORPHANED_RUN_STATUS,
                    "error": reason,
                    "reconciled": True,
                },
            )
        except Exception as e:
            logger.error(f"Failed to mirror orphaned status to Redis for {candidate_id}: {e}")

        # 5. Tell any live SSE subscriber in this process.
        try:
            from backend.main import run_event_hub

            await run_event_hub.publish(
                candidate_id,
                {
                    "type": "status_change",
                    "run_id": candidate_id,
                    "status": ORPHANED_RUN_STATUS,
                    "terminal": True,
                    "reason": reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            logger.error(f"Failed to publish orphaned status for {candidate_id}: {e}")

    return reclaimed


async def _run_has_live_owner(pool: asyncpg.Pool, run_uuid: Any) -> bool:
    """True when ``run_uuid`` has been claimed by an execution since the sweep wrote it.

    Fails closed towards *writing* the mirrors: a read that cannot be completed is
    reported as "no live owner" so the reconciliation still repairs the Redis snapshot
    and tells subscribers, which is the behaviour that existed before this check and the
    one that leaves the run visible as failed everywhere. Getting this wrong in that
    direction leaves a stale `running` read model; getting it wrong the other way
    silently skips the notification for a genuinely dead run.
    """
    try:
        async with pool.acquire() as conn:
            owner_id = await conn.fetchval(
                "SELECT owner_id FROM agent_runs WHERE run_id = $1;",
                run_uuid,
            )
        return owner_id is not None
    except Exception as e:
        logger.warning(f"Could not re-check ownership of agent run {run_uuid}: {e}")
        return False