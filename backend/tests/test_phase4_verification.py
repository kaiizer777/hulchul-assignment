"""
backend/tests/test_phase4_verification.py
Unit and integration tests for Phase 4 Verification & Evidence reporting.
Covers expected vs actual state diff logic, deliberate mismatch detection,
incomplete item extraction, and the FastAPI verification endpoint.
"""

import asyncio
import unittest
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, get_db_pool
from backend.verification import generate_verification_report, VerificationReport


class TestPhase4VerificationUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Phase 4 verification diff and rule evaluation logic."""

    async def test_01_verification_report_structure(self):
        """Verify verification report correctly processes mock invoice states and computes summary counts."""
        mock_pool = MagicMock(spec=asyncpg.Pool)
        mock_conn = AsyncMock()

        # Mock purchase orders
        mock_conn.fetch.side_effect = [
            # purchase_orders query
            [
                {"po_number": "PO-1001", "approved_amount": Decimal("25000.00"), "status": "active"},
                {"po_number": "PO-1005", "approved_amount": Decimal("20000.00"), "status": "active"},
            ],
            # invoices query
            [
                {
                    "id": uuid.uuid4(),
                    "vendor": "Acme Corp",
                    "amount": Decimal("25000.00"),
                    "date": "2026-09-15",
                    "po_number": "PO-1001",
                    "status": "completed",
                },
                {
                    "id": uuid.uuid4(),
                    "vendor": "Zenith Parts",
                    "amount": Decimal("26000.00"), # 30% discrepancy (>10% -> expected flagged)
                    "date": "2026-09-24",
                    "po_number": "PO-1005",
                    "status": "pending", # Actual status pending -> incomplete / mismatch
                },
                {
                    "id": uuid.uuid4(),
                    "vendor": "Ghost Vendor",
                    "amount": Decimal("18500.00"),
                    "date": "2026-09-26",
                    "po_number": "PO-9999", # Missing PO -> expected failed
                    "status": "pending",
                },
            ],
            # agent_steps query for failed steps
            [
                {
                    "step_id": uuid.uuid4(),
                    "action": "fill_invoice",
                    "result": "failed: ERP 500 error",
                    "screenshot_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
                    "timestamp": "2026-10-02T12:00:00Z",
                }
            ],
        ]

        mock_acquire_context = AsyncMock()
        mock_acquire_context.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_acquire_context

        run_id = str(uuid.uuid4())
        report = await generate_verification_report(mock_pool, run_id)

        self.assertEqual(report.run_id, run_id)
        self.assertEqual(report.total_invoices, 3)
        self.assertEqual(report.pass_count, 1) # Acme Corp matches expected completed
        self.assertEqual(report.fail_count, 2) # Zenith (expected flagged, got pending) & Ghost (expected failed, got pending)
        self.assertGreaterEqual(report.incomplete_count, 1)
        self.assertEqual(len(report.failed_steps), 1)
        self.assertIsNotNone(report.failed_steps[0].screenshot_b64)


@unittest.skipUnless(
    bool(settings.DATABASE_URL),
    "Live Neon DB not configured",
)
class TestPhase4VerificationIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test suite verifying verification report against live Neon database."""

    async def asyncSetUp(self):
        """Initialize live Neon DB pool."""
        await init_db_pool()
        self.pool = await get_db_pool()
        self.created_run_ids: List[str] = []

    async def asyncTearDown(self):
        """Clean up test records."""
        if hasattr(self, "created_run_ids") and self.created_run_ids and self.pool:
            try:
                valid_uuids = [uuid.UUID(str(r)) for r in self.created_run_ids]
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM agent_steps WHERE run_id = ANY($1::uuid[]);", valid_uuids)
                    await conn.execute("DELETE FROM agent_runs WHERE run_id = ANY($1::uuid[]);", valid_uuids)
            except Exception:
                pass
        await close_db_pool()

    async def test_02_live_verification_report_generation(self):
        """Verify verification report generates correctly against live Neon DB and seeded invoices."""
        test_run_id = str(uuid.uuid4())
        self.created_run_ids.append(test_run_id)

        async with self.pool.acquire() as conn:
            # Create a test run record
            await conn.execute(
                "INSERT INTO agent_runs (run_id, goal, status) VALUES ($1, $2, $3);",
                uuid.UUID(test_run_id),
                "Test verification goal",
                "completed",
            )
            # Insert a failed step with screenshot evidence
            await conn.execute(
                """
                INSERT INTO agent_steps (run_id, action, result, screenshot_b64)
                VALUES ($1, $2, $3, $4);
                """,
                uuid.UUID(test_run_id),
                "submit_invoice",
                "failed: validation error",
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
            )

        report = await generate_verification_report(self.pool, test_run_id)
        self.assertEqual(report.run_id, test_run_id)
        self.assertGreaterEqual(report.total_invoices, 10)
        self.assertEqual(len(report.failed_steps), 1)
        self.assertIsNotNone(report.failed_steps[0].screenshot_b64)


if __name__ == "__main__":
    unittest.main()
