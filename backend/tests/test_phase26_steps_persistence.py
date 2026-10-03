import asyncio
import base64
import contextlib
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
from httpx import ASGITransport, AsyncClient
from groq import AsyncGroq

from backend.config import settings
from backend.main import app
from backend.agent import ReActAgent
from backend.tools import PlaywrightTools
from backend.redis_client import UpstashRedisClient, get_redis_client

_TRANSIENT_DB_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.TooManyConnectionsError,
    OSError,
    asyncio.TimeoutError,
)


class TestPhase26StepsPersistence(unittest.IsolatedAsyncioTestCase):
    """
    Comprehensive tests for Phase 2.6:
    Persist every step to agent_steps table in Neon (action, result, screenshot_b64, timestamp).

    Test isolation (issue #41): each test owns a dedicated asyncpg pool that is
    closed in tearDown. This class never reads or closes the process-global
    backend.db._db_pool, so a sibling module's teardown cannot close the pool
    mid-setup (previously surfaced as ConnectionDoesNotExistError in
    asyncSetUp). Global pool lookups are patched to the isolated pool for the
    duration of each test so the FastAPI endpoints under test read the same
    rows. Only transient Neon connection failures retry-then-skip legibly
    instead of erroring; authentication, catalog and syntax errors propagate
    and fail the test. A pool that was already created is closed on every
    failed validation path, transient or fatal, because a fatal error exits
    asyncSetUp and never reaches asyncTearDown.
    """

    async def _init_isolated_pool(self):
        """Create a per-test pool; retry once on transient failure, else SkipTest."""
        if not settings.DATABASE_URL:
            raise unittest.SkipTest("DATABASE_URL is not set; skipping live-Neon phase26 tests")
        last_exc = None
        for attempt in (1, 2):
            try:
                pool = await asyncpg.create_pool(
                    dsn=settings.DATABASE_URL,
                    min_size=1,
                    max_size=2,
                )
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
                        f"Neon unavailable for phase26 (attempt {attempt}): "
                        f"{type(last_exc).__name__}: {last_exc}"
                    ) from exc
        raise unittest.SkipTest(f"Neon unavailable for phase26: {last_exc}")

    async def asyncSetUp(self):
        """Initialize the isolated Neon pool, patched pool lookups, and Redis client."""
        self._db_patchers = []
        self.pool = await self._init_isolated_pool()
        for target in (
            "backend.db.get_db_pool",
            "backend.main.get_db_pool",
            "backend.agent.get_db_pool",
        ):
            with contextlib.suppress(AttributeError, ModuleNotFoundError):
                patcher = patch(target, new=AsyncMock(return_value=self.pool))
                patcher.start()
                self._db_patchers.append(patcher)
        self.redis = get_redis_client()
        self.test_run_ids: List[str] = []

    async def asyncTearDown(self):
        """Clean up created test run records, then release pool and Redis client."""
        cleanup_error = None
        try:
            if getattr(self, "test_run_ids", None) and getattr(self, "pool", None) is not None:
                valid_uuids = []
                for r in self.test_run_ids:
                    try:
                        valid_uuids.append(uuid.UUID(str(r)))
                    except (ValueError, TypeError):
                        pass
                if valid_uuids:
                    try:
                        async with self.pool.acquire() as conn:
                            await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                            await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                    except _TRANSIENT_DB_ERRORS as exc:
                        cleanup_error = exc
        finally:
            for patcher in getattr(self, "_db_patchers", []):
                with contextlib.suppress(RuntimeError):
                    patcher.stop()
            self._db_patchers = []
            pool = getattr(self, "pool", None)
            self.pool = None
            if pool is not None:
                with contextlib.suppress(*_TRANSIENT_DB_ERRORS):
                    await pool.close()
            redis = getattr(self, "redis", None)
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.close()
        if cleanup_error is not None:
            raise AssertionError(
                f"phase26 cleanup failed, test rows may be leaked: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            ) from cleanup_error

    # -----------------------------------------------------------------------
    # 1. Step Creation, Verification & Timestamps in Neon
    # -----------------------------------------------------------------------
    async def test_01_persist_step_basic_and_timestamps(self):
        """Verify basic step persistence to Neon with timestamp, action, and result."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test step persistence basic")

        before_ts = datetime.now(timezone.utc)
        step_id = await agent.persist_step(
            action="navigate",
            result="navigated to /invoices (status: 200)",
        )
        after_ts = datetime.now(timezone.utc)

        self.assertIsNotNone(step_id)
        step_uuid = uuid.UUID(step_id)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT step_id, run_id, action, result, screenshot_b64, timestamp FROM agent_steps WHERE step_id = $1;",
                step_uuid,
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["run_id"], uuid.UUID(test_run_id))
            self.assertEqual(row["action"], "navigate")
            self.assertEqual(row["result"], "navigated to /invoices (status: 200)")
            self.assertIsNone(row["screenshot_b64"])
            self.assertIsNotNone(row["timestamp"])
            # Timestamp should fall in valid range
            self.assertGreaterEqual(row["timestamp"], before_ts.replace(microsecond=0))

    async def test_02_persist_step_with_screenshot_b64(self):
        """Verify step persistence with base64 encoded screenshot buffer."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test step persistence with screenshot")

        fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        step_id = await agent.persist_step(
            action="take_screenshot",
            result="screenshot captured (85 bytes)",
            screenshot_b64=fake_b64,
        )

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT step_id, action, result, screenshot_b64 FROM agent_steps WHERE step_id = $1;",
                uuid.UUID(step_id),
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["action"], "take_screenshot")
            self.assertEqual(row["screenshot_b64"], fake_b64)

    async def test_03_update_step_and_attach_screenshot(self):
        """Verify updating an existing step (e.g. attaching screenshot after execution)."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test step update")

        step_id = await agent.persist_step(action="click", result="clicked Submit Button")

        update_ok = await agent.update_step(
            step_id=step_id,
            result="clicked Submit Button (DOM confirmed)",
            screenshot_b64="dXBkYXRlZF9zY3JlZW5zaG90X2Jhc2U2NA==",
        )
        self.assertTrue(update_ok)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT result, screenshot_b64 FROM agent_steps WHERE step_id = $1;",
                uuid.UUID(step_id),
            )
            self.assertEqual(row["result"], "clicked Submit Button (DOM confirmed)")
            self.assertEqual(row["screenshot_b64"], "dXBkYXRlZF9zY3JlZW5zaG90X2Jhc2U2NA==")

    # -----------------------------------------------------------------------
    # 2. Chronological Ordering & Keyset Retrieval
    # -----------------------------------------------------------------------
    async def test_04_get_steps_chronological_ordering(self):
        """Verify querying steps returns full chronological list."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test chronological ordering")

        s1 = await agent.persist_step(action="navigate", result="navigated to /invoices")
        await asyncio.sleep(0.02)
        s2 = await agent.persist_step(action="read_page", result="read_page: 2450 bytes")
        await asyncio.sleep(0.02)
        s3 = await agent.persist_step(
            action="click",
            result="clicked Create Invoice",
            screenshot_b64="c2NyZWVuc2hvdF8x",
        )

        steps = await agent.get_steps(include_screenshots=True)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["step_id"], s1)
        self.assertEqual(steps[1]["step_id"], s2)
        self.assertEqual(steps[2]["step_id"], s3)
        self.assertEqual(steps[0]["action"], "navigate")
        self.assertEqual(steps[1]["action"], "read_page")
        self.assertEqual(steps[2]["action"], "click")
        self.assertTrue(steps[2]["has_screenshot"])
        self.assertEqual(steps[2]["screenshot_b64"], "c2NyZWVuc2hvdF8x")

        # Verify include_screenshots=False excludes b64 string
        steps_compact = await agent.get_steps(include_screenshots=False)
        self.assertIsNone(steps_compact[2]["screenshot_b64"])
        self.assertTrue(steps_compact[2]["has_screenshot"])

    async def test_05_get_step_by_id(self):
        """Verify fetching single step by ID."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test get_step_by_id")

        step_id = await agent.persist_step(action="fill", result="filled Amount = 25000")
        step_data = await agent.get_step_by_id(step_id)

        self.assertIsNotNone(step_data)
        self.assertEqual(step_data["step_id"], step_id)
        self.assertEqual(step_data["action"], "fill")
        self.assertEqual(step_data["result"], "filled Amount = 25000")

        # Nonexistent step
        fake_id = str(uuid.uuid4())
        self.assertIsNone(await agent.get_step_by_id(fake_id))

    # -----------------------------------------------------------------------
    # 3. Foreign Key Cascade & Auto-recovery
    # -----------------------------------------------------------------------
    async def test_06_foreign_key_cascade_deletion(self):
        """Verify ON DELETE CASCADE removes agent_steps when agent_run is deleted."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("Test cascade deletion")

        await agent.persist_step(action="navigate", result="success")
        await agent.persist_step(action="click", result="success")

        # Verify 2 steps exist
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM agent_steps WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(count, 2)

            # Delete the run
            await conn.execute("DELETE FROM agent_runs WHERE run_id = $1;", uuid.UUID(test_run_id))

            # Verify steps are automatically deleted via CASCADE
            count_after = await conn.fetchval(
                "SELECT count(*) FROM agent_steps WHERE run_id = $1;",
                uuid.UUID(test_run_id),
            )
            self.assertEqual(count_after, 0)

    async def test_07_persist_step_auto_recovers_if_run_missing(self):
        """Verify persist_step automatically ensures agent_runs record if called before ensure_run_record."""
        unregistered_run_id = str(uuid.uuid4())
        self.test_run_ids.append(unregistered_run_id)

        agent = ReActAgent(run_id=unregistered_run_id, pool=self.pool)
        # Call persist_step directly without calling ensure_run_record first
        step_id = await agent.persist_step(action="auto_init_step", result="created safely")
        self.assertIsNotNone(step_id)

        async with self.pool.acquire() as conn:
            run_exists = await conn.fetchrow(
                "SELECT run_id, status FROM agent_runs WHERE run_id = $1;",
                uuid.UUID(unregistered_run_id),
            )
            self.assertIsNotNone(run_exists)

    # -----------------------------------------------------------------------
    # 4. ReAct Loop Step Persistence (All Tool Actions & Completions)
    # -----------------------------------------------------------------------
    async def test_08_react_loop_persists_every_action_and_done(self):
        """
        Verify that during a multi-step ReAct run, every single tool execution
        and completion is faithfully persisted to Neon agent_steps.
        """
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()

        mock_tools.read_page = AsyncMock(return_value={
            "success": True,
            "url": "http://localhost:3051/invoices/new",
            "title": "New Invoice",
            "snapshot": "- input 'Amount'\n- button 'Create Invoice'",
            "size_bytes": 1200,
        })
        mock_tools.execute = AsyncMock(side_effect=[
            {"success": True, "url": "http://localhost:3051/invoices/new", "status": 200},
            {"success": True, "selector": "Vendor", "selected": "Acme Corp"},
            {"success": True, "selector": "Amount", "value": "35000"},
            {"success": True, "selector": "Create Invoice", "clicked": True},
        ])
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})

        # Mock Groq completions
        mock_groq = MagicMock(spec=AsyncGroq)
        mock_completions = MagicMock()

        def make_msg(name, args):
            m = MagicMock()
            tc = MagicMock()
            tc.id = f"call_{name}_{uuid.uuid4().hex[:6]}"
            tc.function.name = name
            tc.function.arguments = json.dumps(args)
            m.tool_calls = [tc]
            m.content = None
            return MagicMock(choices=[MagicMock(message=m)])

        def make_done_msg(text):
            m = MagicMock()
            m.tool_calls = None
            m.content = text
            return MagicMock(choices=[MagicMock(message=m)])

        mock_completions.create = AsyncMock(side_effect=[
            make_msg("navigate", {"url": "/invoices/new"}),
            make_msg("select", {"selector": "Vendor", "value": "Acme Corp"}),
            make_msg("fill", {"selector": "Amount", "value": "35000"}),
            make_msg("click", {"selector": "Create Invoice"}),
            make_done_msg("Done! Invoice for Acme Corp created successfully."),
        ])
        mock_groq.chat = MagicMock(completions=mock_completions)

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=10,
        )

        res = await agent.run(goal="Create invoice for Acme Corp amount 35000")
        self.assertEqual(res["status"], "completed")

        # Query all steps persisted in Neon
        steps = await agent.get_steps()
        actions = [s["action"] for s in steps]

        # Verify every action was persisted in order
        self.assertIn("navigate", actions)
        self.assertIn("select", actions)
        self.assertIn("fill", actions)
        self.assertIn("click", actions)
        self.assertIn("done", actions)

        # Check results
        done_step = next(s for s in steps if s["action"] == "done")
        self.assertIn("Acme Corp created successfully", done_step["result"])

    async def test_09_failed_tool_step_persists_failure_and_attaches_screenshot(self):
        """Verify failed tool execution persists error result and attaches failure screenshot."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.run_id = test_run_id
        mock_tools.set_run_id = MagicMock()
        mock_tools.read_page = AsyncMock(return_value={"success": True, "snapshot": "empty", "size_bytes": 10})
        mock_tools.execute = AsyncMock(return_value={"success": False, "error": "Element '#nonexistent' not found"})

        failure_b64 = "ZmFpbHVyZV9zY3JlZW5zaG90X2Jhc2U2NA=="
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": failure_b64})

        mock_groq = MagicMock(spec=AsyncGroq)
        m = MagicMock()
        tc = MagicMock()
        tc.id = "call_fail_1"
        tc.function.name = "click"
        tc.function.arguments = json.dumps({"selector": "#nonexistent"})
        m.tool_calls = [tc]
        m.content = None

        m_done = MagicMock()
        m_done.tool_calls = None
        m_done.content = "Failed and done"

        mock_groq.chat = MagicMock(completions=MagicMock(create=AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=m)]),
            MagicMock(choices=[MagicMock(message=m_done)]),
        ])))

        agent = ReActAgent(
            run_id=test_run_id,
            tools=mock_tools,
            pool=self.pool,
            groq_client=mock_groq,
            redis_client=self.redis,
            max_iterations=5,
        )

        await agent.run(goal="Try to click nonexistent element")

        steps = await agent.get_steps()
        failed_step = next((s for s in steps if s["action"] == "click"), None)
        self.assertIsNotNone(failed_step)
        self.assertIn("failed", failed_step["result"])
        self.assertIn("Element '#nonexistent' not found", failed_step["result"])
        self.assertTrue(failed_step["has_screenshot"])
        self.assertEqual(failed_step["screenshot_b64"], failure_b64)

    # -----------------------------------------------------------------------
    # 5. FastAPI Endpoints for Step Retrieval
    # -----------------------------------------------------------------------
    async def test_10_fastapi_step_endpoints(self):
        """Verify GET /agent/runs/{run_id}/steps and GET /agent/steps/{step_id} endpoints."""
        test_run_id = str(uuid.uuid4())
        self.test_run_ids.append(test_run_id)

        agent = ReActAgent(run_id=test_run_id, pool=self.pool)
        await agent.ensure_run_record("API Endpoint Step Test")

        s1 = await agent.persist_step(action="navigate", result="navigated to /invoices")
        s2 = await agent.persist_step(
            action="take_screenshot",
            result="screenshot taken",
            screenshot_b64="YXBpX3Rlc3Rfc2NyZWVuc2hvdA==",
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            from conftest import issue_test_session
            auth = {"Cookie": issue_test_session()}

            # 1. Fetch steps list without screenshots
            res = await client.get(f"/agent/runs/{test_run_id}/steps", headers=auth)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["run_id"], test_run_id)
            self.assertEqual(data["count"], 2)
            self.assertIsNone(data["steps"][1]["screenshot_b64"])
            self.assertTrue(data["steps"][1]["has_screenshot"])

            # 2. Fetch steps list with screenshots
            res_full = await client.get(f"/agent/runs/{test_run_id}/steps?include_screenshots=true", headers=auth)
            self.assertEqual(res_full.status_code, 200)
            data_full = res_full.json()
            self.assertEqual(data_full["steps"][1]["screenshot_b64"], "YXBpX3Rlc3Rfc2NyZWVuc2hvdA==")

            # 3. Fetch single step by step_id
            res_single = await client.get(f"/agent/steps/{s2}", headers=auth)
            self.assertEqual(res_single.status_code, 200)
            data_single = res_single.json()
            self.assertEqual(data_single["step_id"], s2)
            self.assertEqual(data_single["action"], "take_screenshot")
            self.assertEqual(data_single["screenshot_b64"], "YXBpX3Rlc3Rfc2NyZWVuc2hvdA==")

            # 4. 404 for non-existent run and step
            res_404_run = await client.get(f"/agent/runs/{uuid.uuid4()}/steps", headers=auth)
            self.assertEqual(res_404_run.status_code, 404)

            res_404_step = await client.get(f"/agent/steps/{uuid.uuid4()}", headers=auth)
            self.assertEqual(res_404_step.status_code, 404)


def _mock_pool(fetchval_side_effect=None, execute_side_effect=None, close_side_effect=None):
    """Build a stand-in asyncpg pool whose acquire() yields a configurable conn."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=fetchval_side_effect, return_value=1)
    conn.execute = AsyncMock(side_effect=execute_side_effect)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.close = AsyncMock(side_effect=close_side_effect)
    return pool, conn


class TestPhase26PoolInitErrorClassification(unittest.IsolatedAsyncioTestCase):
    """Verify pool init retries-and-skips only transient failures, never fatal ones."""

    def _case(self):
        """Return a bare phase26 instance to drive lifecycle methods directly."""
        return TestPhase26StepsPersistence("test_01_persist_step_basic_and_timestamps")

    def _settings_patch(self):
        """Patch this module's settings with a fake DATABASE_URL, since it is frozen."""
        return patch.object(
            sys.modules[__name__],
            "settings",
            MagicMock(DATABASE_URL="postgresql://user:pw@127.0.0.1:5432/postgres"),
        )

    async def test_transient_failure_then_success_returns_pool_without_skip(self):
        """A transient ConnectionDoesNotExistError on attempt 1 retries and returns the pool."""
        broken, _ = _mock_pool(fetchval_side_effect=asyncpg.ConnectionDoesNotExistError("gone"))
        healthy, _ = _mock_pool()
        create_pool = AsyncMock(side_effect=[broken, healthy])

        with self._settings_patch(), patch("asyncpg.create_pool", new=create_pool):
            pool = await self._case()._init_isolated_pool()

        self.assertIs(pool, healthy)
        self.assertEqual(create_pool.await_count, 2)
        broken.close.assert_awaited_once()

    async def test_transient_failure_on_both_attempts_skips(self):
        """Two transient failures skip the test instead of erroring."""
        first, _ = _mock_pool(fetchval_side_effect=asyncpg.ConnectionDoesNotExistError("gone"))
        second, _ = _mock_pool(fetchval_side_effect=asyncpg.ConnectionDoesNotExistError("gone again"))
        create_pool = AsyncMock(side_effect=[first, second])

        with self._settings_patch(), \
                patch("asyncpg.create_pool", new=create_pool), \
                self.assertRaises(unittest.SkipTest) as raised:
            await self._case()._init_isolated_pool()

        self.assertIn("ConnectionDoesNotExistError", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, asyncpg.ConnectionDoesNotExistError)
        self.assertEqual(create_pool.await_count, 2)
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()

    async def test_transient_pool_close_failure_does_not_mask_skip(self):
        """A pool that also fails to close still skips on the original transient error."""
        first, _ = _mock_pool(
            fetchval_side_effect=asyncpg.ConnectionDoesNotExistError("gone"),
            close_side_effect=asyncpg.ConnectionDoesNotExistError("close failed"),
        )
        second, _ = _mock_pool(
            fetchval_side_effect=asyncpg.ConnectionDoesNotExistError("gone"),
            close_side_effect=asyncpg.ConnectionDoesNotExistError("close failed"),
        )
        create_pool = AsyncMock(side_effect=[first, second])

        with self._settings_patch(), \
                patch("asyncpg.create_pool", new=create_pool), \
                self.assertRaises(unittest.SkipTest):
            await self._case()._init_isolated_pool()

    async def test_fatal_validation_error_propagates_after_closing_created_pool(self):
        """A fatal error once the pool exists propagates unchanged, and the pool is closed."""
        for stage in ("acquire", "fetchval"):
            with self.subTest(stage=stage):
                cause = ValueError("root cause")
                fatal = asyncpg.UndefinedTableError("relation missing")
                fatal.__cause__ = cause
                pool, conn = _mock_pool()
                if stage == "fetchval":
                    conn.fetchval = AsyncMock(side_effect=fatal)
                else:
                    pool.acquire.side_effect = fatal
                create_pool = AsyncMock(return_value=pool)

                with self._settings_patch(), \
                        patch("asyncpg.create_pool", new=create_pool), \
                        self.assertRaises(asyncpg.UndefinedTableError) as raised:
                    await self._case()._init_isolated_pool()

                self.assertIs(raised.exception, fatal)
                self.assertIs(raised.exception.__cause__, cause)
                self.assertNotIsInstance(raised.exception, unittest.SkipTest)
                self.assertEqual(create_pool.await_count, 1)
                pool.close.assert_awaited_once()

    async def test_auth_and_catalog_errors_propagate_without_retry_or_skip(self):
        """Bad credentials or a bad database name fail the test on the first attempt."""
        for exc_cls in (asyncpg.InvalidPasswordError, asyncpg.InvalidCatalogNameError):
            with self.subTest(exc_cls=exc_cls.__name__):
                create_pool = AsyncMock(side_effect=exc_cls("nope"))
                with self._settings_patch(), \
                        patch("asyncpg.create_pool", new=create_pool), \
                        self.assertRaises(exc_cls) as raised:
                    await self._case()._init_isolated_pool()
                self.assertNotIsInstance(raised.exception, unittest.SkipTest)
                self.assertEqual(create_pool.await_count, 1)

    async def test_malformed_dsn_propagates_as_configuration_error(self):
        """A malformed DSN is a configuration error, so it must not be swallowed."""
        create_pool = AsyncMock(side_effect=asyncpg.ClientConfigurationError("invalid DSN"))

        with self._settings_patch(), \
                patch("asyncpg.create_pool", new=create_pool), \
                self.assertRaises(asyncpg.ClientConfigurationError):
            await self._case()._init_isolated_pool()

        self.assertEqual(create_pool.await_count, 1)

    async def test_missing_database_url_skips_without_touching_network(self):
        """An unset DATABASE_URL skips without ever calling asyncpg.create_pool."""
        create_pool = AsyncMock()

        with patch.object(sys.modules[__name__], "settings", MagicMock(DATABASE_URL=None)), \
                patch("asyncpg.create_pool", new=create_pool), \
                self.assertRaises(unittest.SkipTest):
            await self._case()._init_isolated_pool()

        create_pool.assert_not_awaited()

    async def test_setup_and_teardown_leave_global_db_pool_untouched(self):
        """asyncSetUp/asyncTearDown never read, replace or close backend.db._db_pool."""
        import backend.db

        initial_pool = backend.db._db_pool
        pool, conn = _mock_pool()
        redis = MagicMock()
        redis.close = AsyncMock()
        case = self._case()

        with self._settings_patch(), \
                patch("asyncpg.create_pool", new=AsyncMock(return_value=pool)), \
                patch.object(sys.modules[__name__], "get_redis_client", return_value=redis):
            await case.asyncSetUp()
            self.assertEqual(len(case._db_patchers), 3)
            self.assertIs(backend.db._db_pool, initial_pool)
            case.test_run_ids.append(str(uuid.uuid4()))
            await case.asyncTearDown()
            self.assertIs(backend.db._db_pool, initial_pool)

        self.assertEqual(conn.execute.await_count, 2)
        pool.close.assert_awaited_once()
        redis.close.assert_awaited_once()

    async def test_failed_cleanup_delete_is_reported_after_resources_released(self):
        """A failed cleanup DELETE surfaces as a failure, but the pool still closes."""
        cases = (
            (asyncpg.ConnectionDoesNotExistError("connection closed mid-delete"), AssertionError),
            (asyncpg.UndefinedTableError("relation missing"), asyncpg.UndefinedTableError),
        )
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                pool, _ = _mock_pool(execute_side_effect=exc)
                redis = MagicMock()
                redis.close = AsyncMock()
                case = self._case()
                case.pool = pool
                case.redis = redis
                case._db_patchers = []
                case.test_run_ids = [str(uuid.uuid4())]

                with self.assertRaises(expected) as raised:
                    await case.asyncTearDown()

                self.assertIsNone(case.pool)
                pool.close.assert_awaited_once()
                redis.close.assert_awaited_once()
                if expected is AssertionError:
                    self.assertIn("phase26 cleanup failed", str(raised.exception))
                    self.assertIsInstance(raised.exception.__cause__, type(exc))
                else:
                    self.assertIsInstance(raised.exception, type(exc))


if __name__ == "__main__":
    unittest.main()
