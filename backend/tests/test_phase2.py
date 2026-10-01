import asyncio
import os
import unittest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport

from backend.config import settings, Settings
from backend.db import init_db_pool, close_db_pool, check_db_health, get_db_pool
from backend.browser import (
    get_browser_session,
    verify_cdp_connection,
    BrowserConnectionError,
)
from backend.main import app


class TestPhase2Backend(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Initialize db pool for tests
        await init_db_pool()

    async def asyncTearDown(self):
        # Close db pool cleanly
        await close_db_pool()

    async def test_01_config_loaded(self):
        """Phase 2.1: Verify environment settings are properly loaded and typed."""
        self.assertTrue(bool(settings.DATABASE_URL), "DATABASE_URL should not be empty")
        self.assertTrue(bool(settings.BROWSER_WS_ENDPOINT), "BROWSER_WS_ENDPOINT should not be empty")
        self.assertGreater(settings.DB_POOL_MIN_SIZE, 0)
        self.assertGreater(settings.DB_POOL_MAX_SIZE, settings.DB_POOL_MIN_SIZE)

    async def test_02_asyncpg_db_health(self):
        """Phase 2.1: Verify asyncpg connection pool connects and health check passes."""
        is_healthy = await check_db_health()
        self.assertTrue(is_healthy, "Database health probe (SELECT 1) failed")

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT current_database()")
            self.assertIsNotNone(val)

    async def test_03_fastapi_endpoints(self):
        """Phase 2.1: Verify FastAPI root and health endpoints return 200 with correct schema."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Test Root
            res_root = await client.get("/")
            self.assertEqual(res_root.status_code, 200)
            data_root = res_root.json()
            self.assertIn("Hulchul Backend API", data_root["message"])
            self.assertEqual(data_root["phase"], "Phase 2.1 & 2.2 Complete")

            # Test Health
            res_health = await client.get("/health")
            self.assertEqual(res_health.status_code, 200)
            data_health = res_health.json()
            self.assertEqual(data_health["status"], "healthy")
            self.assertEqual(data_health["database"], "connected")

    async def test_04_cdp_browser_error_on_missing_endpoint(self):
        """Phase 2.2: Verify BrowserConnectionError is raised when endpoint is missing."""
        with self.assertRaises(BrowserConnectionError):
            async with get_browser_session(endpoint=""):
                pass

    async def test_05_cdp_browser_error_on_invalid_endpoint(self):
        """Phase 2.2: Verify BrowserConnectionError is raised when endpoint is invalid."""
        with self.assertRaises(BrowserConnectionError):
            async with get_browser_session(endpoint="wss://invalid-ws-url-that-does-not-exist.local", timeout_ms=3000):
                pass

    async def test_06_remote_cdp_connection_live(self):
        """Phase 2.2: Verify live connection to Browserless / Steel.dev over CDP without local browser binaries."""
        self.assertTrue(
            settings.BROWSER_WS_ENDPOINT.startswith("wss://") or settings.BROWSER_WS_ENDPOINT.startswith("ws://"),
            "BROWSER_WS_ENDPOINT must be a valid WebSocket URL"
        )
        
        async with get_browser_session() as session:
            self.assertIsNotNone(session.browser)
            self.assertTrue(session.browser.is_connected())
            self.assertEqual(session.browser.browser_type.name, "chromium")

            # Verify page navigation over CDP
            await session.page.goto("about:blank")
            title = await session.page.title()
            self.assertEqual(title, "")

    async def test_07_verify_cdp_connection_diagnostic(self):
        """Phase 2.2: Verify the verify_cdp_connection probe function."""
        diag = await verify_cdp_connection()
        self.assertTrue(diag["connected"], f"CDP diagnostic probe failed: {diag}")
        self.assertEqual(diag["browser_type"], "chromium")
        self.assertGreater(diag["latency_ms"], 0)

    async def test_08_fastapi_browser_health_endpoint(self):
        """Phase 2.2: Verify GET /health/browser endpoint returns 200 with CDP connectivity details."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/health/browser")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["connected"])
            self.assertEqual(data["browser_type"], "chromium")
            self.assertTrue(data["endpoint_configured"])
            self.assertIsNone(data["error"])


if __name__ == "__main__":
    unittest.main()
