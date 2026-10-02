"""
backend/verification.py
Phase 4: Verification and Evidence reporting module for agent runs.
Compares actual invoice states in Neon against expected states derived from seed data rules,
extracts incomplete items, and gathers failed step screenshots.
"""

import logging
from typing import Any, Dict, List, Optional
import asyncpg
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class VerificationRow(BaseModel):
    """Structured verification record for an individual invoice."""
    model_config = ConfigDict(extra="forbid")
    invoice_id: str
    vendor: str
    amount: float
    po_number: Optional[str] = None
    expected_status: str
    actual_status: str
    pass_fail: bool
    reason: str
    classification: str


class IncompleteItem(BaseModel):
    """Record of an incomplete or flagged invoice item requiring attention."""
    model_config = ConfigDict(extra="forbid")
    invoice_id: str
    vendor: str
    amount: float
    po_number: Optional[str] = None
    status: str
    reason: str


class FailedStepEvidence(BaseModel):
    """Failed execution step evidence including base64 screenshot."""
    model_config = ConfigDict(extra="forbid")
    step_id: str
    action: str
    result: Optional[str] = None
    screenshot_b64: Optional[str] = None
    timestamp: str


class VerificationReport(BaseModel):
    """Comprehensive verification report for an agent run."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    total_invoices: int
    pass_count: int
    fail_count: int
    incomplete_count: int
    verification_table: List[VerificationRow]
    incomplete_items: List[IncompleteItem]
    failed_steps: List[FailedStepEvidence]


async def generate_verification_report(pool: asyncpg.Pool, run_id: str) -> VerificationReport:
    """
    Generates a structured verification report comparing actual invoice states in Neon
    against expected seed rules, extracting incomplete items and failed step screenshots.

    Args:
        pool: asyncpg connection pool to Neon Postgres.
        run_id: Agent run UUID string.

    Returns:
        VerificationReport containing summary metrics, verification table, incomplete items, and failed steps.
    """
    async with pool.acquire() as conn:
        # Fetch all purchase orders for approved amount lookup
        po_rows = await conn.fetch("SELECT po_number, approved_amount, status FROM purchase_orders;")
        po_map = {r["po_number"]: r["approved_amount"] for r in po_rows}

        # Fetch all invoices
        invoice_rows = await conn.fetch(
            "SELECT id, vendor, amount, date, po_number, status FROM invoices ORDER BY date ASC;"
        )

        # Fetch failed steps or steps with screenshots for this run
        step_rows = await conn.fetch(
            """
            SELECT step_id, action, result, screenshot_b64, timestamp
            FROM agent_steps
            WHERE run_id = $1 AND (result ILIKE '%fail%' OR screenshot_b64 IS NOT NULL)
            ORDER BY timestamp ASC;
            """,
            run_id,
        )

        failed_steps = [
            FailedStepEvidence(
                step_id=str(r["step_id"]),
                action=r["action"],
                result=r["result"],
                screenshot_b64=r["screenshot_b64"],
                timestamp=r["timestamp"].isoformat() if hasattr(r["timestamp"], "isoformat") else str(r["timestamp"]),
            )
            for r in step_rows
        ]

        verification_table: List[VerificationRow] = []
        incomplete_items: List[IncompleteItem] = []

        pass_count = 0
        fail_count = 0
        incomplete_count = 0

        for inv in invoice_rows:
            inv_id = str(inv["id"])
            vendor = inv["vendor"]
            amount = float(inv["amount"])
            po_number = inv["po_number"]
            actual_status = (inv["status"] or "pending").lower().strip()

            # Determine expected status and classification based on seed rules
            expected_status = "completed"
            classification = "Valid"
            reason = "Valid invoice processed successfully"

            if not po_number or po_number not in po_map:
                expected_status = "failed"
                classification = "Missing PO"
                reason = f"PO number '{po_number or 'None'}' not found in purchase orders"
            else:
                approved_amt = float(po_map[po_number])
                diff_pct = (abs(amount - approved_amt) / approved_amt) * 100.0 if approved_amt > 0 else 0.0

                if diff_pct > 10.0:
                    expected_status = "flagged"
                    classification = "Mismatched"
                    reason = f"Amount discrepancy {diff_pct:.1f}% exceeds 10% limit (PO approved: ₹{approved_amt:,.2f})"
                elif amount > 50000.0:
                    expected_status = "completed"
                    classification = "Over Threshold (>50k)"
                    reason = f"Amount ₹{amount:,.2f} exceeds ₹50,000 threshold (requires approval)"
                else:
                    expected_status = "completed"
                    classification = "Valid"
                    reason = "Valid invoice processed within limits"

            # Evaluate pass/fail match
            pass_match = False
            if expected_status == "completed":
                pass_match = actual_status in ("completed", "skipped", "processed")
            elif expected_status == "flagged":
                pass_match = actual_status in ("flagged", "flagged_mismatch", "flagged_discrepancy")
            elif expected_status == "failed":
                pass_match = actual_status in ("failed", "missing_po", "error")

            is_incomplete = actual_status in ("pending", "awaiting_approval", "flagged", "failed", "skipped")

            if pass_match:
                pass_count += 1
            else:
                fail_count += 1

            if is_incomplete and actual_status != "completed":
                incomplete_count += 1
                incomplete_items.append(
                    IncompleteItem(
                        invoice_id=inv_id,
                        vendor=vendor,
                        amount=amount,
                        po_number=po_number,
                        status=actual_status,
                        reason=reason,
                    )
                )

            verification_table.append(
                VerificationRow(
                    invoice_id=inv_id,
                    vendor=vendor,
                    amount=amount,
                    po_number=po_number,
                    expected_status=expected_status,
                    actual_status=actual_status,
                    pass_fail=pass_match,
                    reason=reason,
                    classification=classification,
                )
            )

        total_invoices = len(invoice_rows)

        return VerificationReport(
            run_id=run_id,
            total_invoices=total_invoices,
            pass_count=pass_count,
            fail_count=fail_count,
            incomplete_count=incomplete_count,
            verification_table=verification_table,
            incomplete_items=incomplete_items,
            failed_steps=failed_steps,
        )
