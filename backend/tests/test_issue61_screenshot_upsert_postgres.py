"""
backend/tests/test_issue61_screenshot_upsert_postgres.py
Live-Postgres proof for the take_screenshot upsert in backend/tools.py.

test_issue61_screenshot_identity.py runs the same statement against an
asyncpg-shaped fake, which cannot tell us whether the SQL is legal: whether
`ON CONFLICT (step_id)` names a real arbiter index, whether asyncpg accepts the
`$4::text` parameter under the statement's inferred types, or whether the
`COALESCE` actually leaves a stored result alone. Only Postgres can answer
that, so this class runs the real tools.take_screenshot against the real
schema and asserts on the real rows.

Gated on DATABASE_URL like test_phase26_steps_persistence.py, so it runs in CI
(the only environment with a database) and skips locally.
"""

import asyncio
import base64
import contextlib
import unittest
import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import asyncpg

from backend.config import settings
from backend.tools import take_screenshot

_TRANSIENT_DB_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.TooManyConnectionsError,
    OSError,
    asyncio.TimeoutError,
)

# What the stub page hands back, and therefore exactly what take_screenshot must
# base64-encode into screenshot_b64.
_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"
_FAKE_PNG_B64 = base64.b64encode(_FAKE_PNG_BYTES).decode("utf-8")


def _fake_page() -> MagicMock:
    """A Playwright-shaped page whose screenshot returns real PNG bytes."""
    page = MagicMock()
    page.screenshot = AsyncMock(return_value=_FAKE_PNG_BYTES)
    return page


class TestScreenshotUpsertAgainstPostgres(unittest.IsolatedAsyncioTestCase):
    """The caller-supplied-step_id branch, executed by Postgres itself.

    Isolation (issue #41): a dedicated pool per test, closed in tearDown, and
    never the process-global backend.db pool. Rows are deleted by run_id so a
    failure cannot leak them.
    """

    async def _init_isolated_pool(self):
        """Create a per-test pool; retry once on transient failure, else SkipTest."""
        if not settings.DATABASE_URL:
            raise unittest.SkipTest("DATABASE_URL is not set; skipping live-Neon screenshot upsert test")
        last_exc = None
        for attempt in (1, 2):
            try:
                pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=2)
                try:
                    async with pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                except BaseException:
                    with contextlib.suppress(Exception):
                        await pool.close()
                    raise
                return pool
            except _TRANSIENT_DB_ERRORS as exc:
                last_exc = exc
                if attempt == 2:
                    raise unittest.SkipTest(
                        f"Neon unavailable for screenshot upsert test (attempt {attempt}): "
                        f"{type(last_exc).__name__}: {last_exc}"
                    ) from exc
        raise unittest.SkipTest(f"Neon unavailable for screenshot upsert test: {last_exc}")

    async def asyncSetUp(self):
        """Open the isolated pool and plant the run row the steps will hang off."""
        self.pool = await self._init_isolated_pool()
        self.run_id = uuid.uuid4()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_runs (run_id, goal, status) VALUES ($1, $2, 'running') ON CONFLICT (run_id) DO NOTHING;",
                self.run_id,
                "issue 61 screenshot upsert",
            )

    async def asyncTearDown(self):
        """Delete this run's rows and release the pool, even if the body failed."""
        try:
            if getattr(self, "pool", None) is not None:
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM agent_steps WHERE run_id = $1;", self.run_id)
                    await conn.execute("DELETE FROM agent_runs WHERE run_id = $1;", self.run_id)
        finally:
            pool = getattr(self, "pool", None)
            self.pool = None
            if pool is not None:
                with contextlib.suppress(*_TRANSIENT_DB_ERRORS):
                    await pool.close()

    async def _shoot(self, step_id: Any, action: str = "take_screenshot", result: Any = None) -> Dict[str, Any]:
        """Call the real tools.take_screenshot against the live pool."""
        return await take_screenshot(
            page=_fake_page(),  # type: ignore[arg-type]
            run_id=str(self.run_id),
            step_id=step_id,
            action=action,
            result=result,
            pool=self.pool,
        )

    async def _rows(self, step_id: uuid.UUID) -> List[Any]:
        """Every row this run holds under `step_id`, so a duplicate is visible."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT step_id, run_id, action, result, screenshot_b64 FROM agent_steps"
                " WHERE run_id = $1 AND step_id = $2;",
                self.run_id,
                step_id,
            )

    async def test_upsert_creates_the_row_under_the_caller_step_id(self):
        """The LLM path: no row exists yet, so the upsert's INSERT branch runs.

        Proves `ON CONFLICT (step_id)` resolves against the real primary key
        and that RETURNING yields the id the caller already minted, rather than
        a server-generated one.
        """
        step_id = uuid.uuid4()

        out = await self._shoot(str(step_id))

        self.assertTrue(out["success"], out)
        self.assertTrue(out["persisted"], out)
        self.assertEqual(out["step_id"], str(step_id))
        rows = await self._rows(step_id)
        self.assertEqual(len(rows), 1, f"expected exactly one row, got {rows}")
        self.assertEqual(rows[0]["action"], "take_screenshot")
        self.assertEqual(rows[0]["result"], "screenshot captured")
        self.assertEqual(rows[0]["screenshot_b64"], _FAKE_PNG_B64)
        self.assertEqual(rows[0]["run_id"], self.run_id)

    async def test_upsert_conflict_keeps_the_stored_failed_result(self):
        """The failure path: the failed row exists, so the DO UPDATE branch runs.

        The stored result is what classifies the row as step_failed when the
        durable replay reaches the stream, so a NULL $4 must leave it alone and
        only the screenshot may be attached. This is the case the fakes cannot
        prove: it depends on COALESCE($4::text, agent_steps.result) resolving
        to the existing column under asyncpg's inferred parameter types.
        """
        step_id = uuid.uuid4()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_steps (step_id, run_id, action, result) VALUES ($1, $2, 'click', $3);",
                step_id,
                self.run_id,
                "failed: Element '#nonexistent' not found",
            )

        out = await self._shoot(str(step_id), action="click")

        self.assertTrue(out["persisted"], out)
        self.assertEqual(out["step_id"], str(step_id))
        rows = await self._rows(step_id)
        self.assertEqual(len(rows), 1, f"the upsert must not add a row, got {rows}")
        self.assertEqual(rows[0]["action"], "click", "the conflict branch must not relabel the row")
        self.assertEqual(rows[0]["result"], "failed: Element '#nonexistent' not found")
        self.assertEqual(rows[0]["screenshot_b64"], _FAKE_PNG_B64)

    async def test_upsert_conflict_result_argument_overwrites_the_stored_one(self):
        """A caller-supplied result wins, which is the other COALESCE branch.

        execute forwards arguments.get("result"), so a non-NULL $4 has to
        replace the stored result rather than be silently dropped.
        """
        step_id = uuid.uuid4()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_steps (step_id, run_id, action, result) VALUES ($1, $2, 'take_screenshot', $3);",
                step_id,
                self.run_id,
                "screenshot captured (old)",
            )

        await self._shoot(str(step_id), result="screenshot captured (new)")

        rows = await self._rows(step_id)
        self.assertEqual(len(rows), 1, f"the upsert must not add a row, got {rows}")
        self.assertEqual(rows[0]["result"], "screenshot captured (new)")

    async def test_upsert_is_idempotent_across_repeated_calls(self):
        """A replayed screenshot must not multiply rows.

        The reattach path re-dispatches the same step under the same id
        (agent.py passes the loop's step_id on the retry), so the statement has
        to converge on one row however many times it runs.
        """
        step_id = uuid.uuid4()

        for _ in range(3):
            await self._shoot(str(step_id))

        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM agent_steps WHERE run_id = $1 AND step_id = $2;",
                self.run_id,
                step_id,
            )
        self.assertEqual(total, 1)
