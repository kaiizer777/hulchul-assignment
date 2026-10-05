"""
backend/tests/test_db_invoice_terminal_states.py
Unit tests for the DB-integration writers (TRACK A):

- G1: ReActAgent records per-invoice dispositions during the run and the
  run-completion path settles them to terminal statuses (completed / flagged
  / failed, rejected -> skipped), transitioning from pending only.
- G3: tools.take_screenshot ensures the agent_runs row in the sweep-visible
  lease form (attempt >= 1, no live lease) instead of a stranded leaseless row.

All database interaction runs against asyncpg-shaped fakes; no live DB needed.
"""

import base64
import unittest
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import ANY, AsyncMock, MagicMock

from backend.agent import ReActAgent


# ---------------------------------------------------------------------------
# asyncpg-shaped fakes
# ---------------------------------------------------------------------------

class _FakeConn:
    """Minimal asyncpg connection double over an in-memory store."""

    def __init__(self, store: Dict[str, Any]):
        self._store = store

    async def fetchrow(self, sql: str, *args: Any) -> Optional[Dict[str, Any]]:
        if "FROM purchase_orders" in sql:
            approved = self._store["pos"].get(args[0])
            if approved is None:
                return None
            return {"approved_amount": approved}
        if "FROM invoices" in sql:
            if "WHERE id = $1" in sql:
                row = self._store["invoices"].get(args[0])
                return dict(row) if row else None
            if "WHERE po_number = $1" in sql:
                for row in self._store["invoices"].values():
                    if row.get("po_number") == args[0]:
                        return dict(row)
                return None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO agent_steps" in sql:
            step_id = uuid.uuid4()
            self._store.setdefault("steps", []).append(
                {"step_id": step_id, "sql": sql, "args": args}
            )
            return step_id
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self._store.setdefault("executed", []).append({"sql": sql, "args": args})
        if "INSERT INTO agent_runs" in sql:
            self._store.setdefault("runs", []).append({"sql": sql, "args": args})
            return "INSERT 0 1"
        if sql.strip().upper().startswith("UPDATE INVOICES SET STATUS"):
            target_status = args[0]
            row_id = args[1]
            row = self._store["invoices"].get(row_id)
            if row is None:
                return "UPDATE 0"
            # Faithful pending guard: the statement carries AND status='pending'.
            if "status = 'pending'" in sql.lower() and (row.get("status") or "") != "pending":
                return "UPDATE 0"
            row["status"] = target_status
            return "UPDATE 1"
        return "UPDATE 0"


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, store: Dict[str, Any]):
        self._store = store

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(_FakeConn(self._store))


def _new_store() -> Dict[str, Any]:
    return {"invoices": {}, "pos": {}, "executed": [], "runs": [], "steps": []}


def _add_invoice(
    store: Dict[str, Any],
    inv_id: uuid.UUID,
    po_number: Optional[str],
    amount: Decimal,
    status: str = "pending",
) -> None:
    store["invoices"][inv_id] = {
        "id": inv_id,
        "status": status,
        "amount": amount,
        "po_number": po_number,
    }


def _make_agent(store: Dict[str, Any]) -> ReActAgent:
    tools = MagicMock()
    agent = ReActAgent(
        run_id=str(uuid.uuid4()),
        tools=tools,
        pool=_FakePool(store),  # type: ignore[arg-type]
    )
    agent.emit_event = AsyncMock()  # type: ignore[method-assign]
    agent.persist_step = AsyncMock(return_value=str(uuid.uuid4()))  # type: ignore[method-assign]
    agent.update_run_status = AsyncMock()  # type: ignore[method-assign]
    agent.stop_run_lease = AsyncMock()  # type: ignore[method-assign]
    return agent


# ---------------------------------------------------------------------------
# G1: disposition tracking
# ---------------------------------------------------------------------------

class TestRecordInvoiceDisposition(unittest.TestCase):
    def test_uuid_invoice_id_is_the_key(self) -> None:
        agent = _make_agent(_new_store())
        inv = str(uuid.uuid4())
        key = agent.record_invoice_disposition("submitted", invoice_id=inv, po_number="PO-1")
        self.assertEqual(key, inv)
        self.assertEqual(agent._processed_invoices[inv]["disposition"], "submitted")

    def test_po_fallback_key_when_no_invoice_id(self) -> None:
        agent = _make_agent(_new_store())
        key = agent.record_invoice_disposition("approved", po_number="PO-1001")
        self.assertEqual(key, "po:PO-1001")

    def test_no_refs_returns_none_and_tracks_nothing(self) -> None:
        agent = _make_agent(_new_store())
        self.assertIsNone(agent.record_invoice_disposition("submitted"))
        self.assertEqual(agent._processed_invoices, {})

    def test_latest_disposition_wins(self) -> None:
        agent = _make_agent(_new_store())
        inv = str(uuid.uuid4())
        agent.record_invoice_disposition("approved", invoice_id=inv, po_number="PO-1")
        agent.record_invoice_disposition("submitted", invoice_id=inv, po_number="PO-1")
        self.assertEqual(agent._processed_invoices[inv]["disposition"], "submitted")


# ---------------------------------------------------------------------------
# G1: pending-only terminal writer
# ---------------------------------------------------------------------------

class TestMarkInvoiceTerminalStatus(unittest.IsolatedAsyncioTestCase):
    async def test_pending_invoice_is_marked(self) -> None:
        store = _new_store()
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1001", Decimal("25000.00"))
        agent = _make_agent(store)
        self.assertTrue(await agent.mark_invoice_terminal_status("completed", invoice_id=str(inv)))
        self.assertEqual(store["invoices"][inv]["status"], "completed")

    async def test_po_fallback_resolves_latest_row(self) -> None:
        store = _new_store()
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1002", Decimal("39000.00"))
        agent = _make_agent(store)
        self.assertTrue(await agent.mark_invoice_terminal_status("skipped", po_number="PO-1002"))
        self.assertEqual(store["invoices"][inv]["status"], "skipped")

    async def test_human_settled_state_is_never_overwritten(self) -> None:
        store = _new_store()
        for settled in ("approved", "completed", "flagged", "failed", "skipped", "rejected"):
            inv = uuid.uuid4()
            _add_invoice(store, inv, f"PO-{settled}", Decimal("100.00"), status=settled)
            agent = _make_agent(store)
            self.assertFalse(
                await agent.mark_invoice_terminal_status("completed", invoice_id=str(inv)),
                f"settled status {settled!r} must stand",
            )
            self.assertEqual(store["invoices"][inv]["status"], settled)
        updates = [e for e in store["executed"] if e["sql"].strip().upper().startswith("UPDATE INVOICES")]
        self.assertEqual(updates, [], "no UPDATE may be issued against settled rows")

    async def test_update_statement_carries_the_pending_guard(self) -> None:
        store = _new_store()
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1001", Decimal("25000.00"))
        agent = _make_agent(store)
        await agent.mark_invoice_terminal_status("completed", invoice_id=str(inv))
        updates = [e for e in store["executed"] if e["sql"].strip().upper().startswith("UPDATE INVOICES")]
        self.assertEqual(len(updates), 1)
        self.assertIn("status = 'pending'", updates[0]["sql"].lower())

    async def test_unknown_invoice_resolves_to_false(self) -> None:
        agent = _make_agent(_new_store())
        self.assertFalse(
            await agent.mark_invoice_terminal_status("completed", po_number="PO-NOPE")
        )

    async def test_lease_lost_writes_nothing(self) -> None:
        store = _new_store()
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1001", Decimal("25000.00"))
        agent = _make_agent(store)
        agent._mark_run_lease_lost()
        self.assertFalse(await agent.mark_invoice_terminal_status("completed", invoice_id=str(inv)))
        self.assertEqual(store["invoices"][inv]["status"], "pending")
        self.assertEqual(store["executed"], [])


# ---------------------------------------------------------------------------
# G1: completion flush classification
# ---------------------------------------------------------------------------

class TestFinalizeProcessedInvoices(unittest.IsolatedAsyncioTestCase):
    async def test_valid_submitted_invoice_completes(self) -> None:
        store = _new_store()
        store["pos"]["PO-1001"] = Decimal("25000.00")
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1001", Decimal("25500.00"))
        agent = _make_agent(store)
        agent.record_invoice_disposition("submitted", invoice_id=str(inv), po_number="PO-1001")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(store["invoices"][inv]["status"], "completed")

    async def test_mismatched_submitted_invoice_is_flagged(self) -> None:
        store = _new_store()
        store["pos"]["PO-1005"] = Decimal("20000.00")
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1005", Decimal("26000.00"))  # 30% off
        agent = _make_agent(store)
        agent.record_invoice_disposition("submitted", invoice_id=str(inv), po_number="PO-1005")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["flagged"], 1)
        self.assertEqual(store["invoices"][inv]["status"], "flagged")

    async def test_missing_po_submitted_invoice_fails(self) -> None:
        store = _new_store()
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-9991", Decimal("18500.00"))
        agent = _make_agent(store)
        agent.record_invoice_disposition("submitted", invoice_id=str(inv), po_number="PO-9991")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(store["invoices"][inv]["status"], "failed")

    async def test_rejected_entry_is_ensured_skipped(self) -> None:
        store = _new_store()
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1006", Decimal("62000.00"))
        agent = _make_agent(store)
        agent.record_invoice_disposition("rejected", invoice_id=str(inv), po_number="PO-1006")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(store["invoices"][inv]["status"], "skipped")

    async def test_approved_without_submit_is_left_alone(self) -> None:
        store = _new_store()
        store["pos"]["PO-1006"] = Decimal("60000.00")
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1006", Decimal("62000.00"))
        agent = _make_agent(store)
        agent.record_invoice_disposition("approved", invoice_id=str(inv), po_number="PO-1006")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["untouched"], 1)
        self.assertEqual(store["invoices"][inv]["status"], "pending")

    async def test_already_settled_row_is_left_alone(self) -> None:
        store = _new_store()
        store["pos"]["PO-1001"] = Decimal("25000.00")
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1001", Decimal("25500.00"), status="approved")
        agent = _make_agent(store)
        agent.record_invoice_disposition("submitted", invoice_id=str(inv), po_number="PO-1001")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["untouched"], 1)
        self.assertEqual(store["invoices"][inv]["status"], "approved")

    async def test_unresolvable_refs_do_not_break_the_flush(self) -> None:
        store = _new_store()
        store["pos"]["PO-1001"] = Decimal("25000.00")
        good = uuid.uuid4()
        _add_invoice(store, good, "PO-1001", Decimal("25500.00"))
        agent = _make_agent(store)
        agent.record_invoice_disposition("submitted", invoice_id="not-a-uuid", po_number=None)
        agent.record_invoice_disposition("submitted", invoice_id=str(good), po_number="PO-1001")
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts["untouched"], 1)
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(store["invoices"][good]["status"], "completed")

    async def test_empty_run_touches_nothing(self) -> None:
        store = _new_store()
        agent = _make_agent(store)
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(counts, {"completed": 0, "flagged": 0, "failed": 0, "skipped": 0, "untouched": 0})
        self.assertEqual(store["executed"], [])

    async def test_lease_lost_touches_nothing(self) -> None:
        store = _new_store()
        store["pos"]["PO-1001"] = Decimal("25000.00")
        inv = uuid.uuid4()
        _add_invoice(store, inv, "PO-1001", Decimal("25500.00"))
        agent = _make_agent(store)
        agent.record_invoice_disposition("submitted", invoice_id=str(inv), po_number="PO-1001")
        agent._mark_run_lease_lost()
        counts = await agent._finalize_processed_invoices()
        self.assertEqual(store["invoices"][inv]["status"], "pending")
        self.assertEqual(store["executed"], [])
        self.assertEqual(counts["completed"], 0)


# ---------------------------------------------------------------------------
# G1: done-branch wiring
# ---------------------------------------------------------------------------

class TestRunCompletionFlushWiring(unittest.IsolatedAsyncioTestCase):
    def _wired_agent(self, store: Dict[str, Any]) -> ReActAgent:
        agent = _make_agent(store)
        agent.tools.page = None  # otherwise _tool_page_is_closed sees a truthy mock
        agent.ensure_run_record = AsyncMock(return_value=True)  # type: ignore[method-assign]
        agent.get_last_successful_step_index = AsyncMock(return_value=0)  # type: ignore[method-assign]
        agent.call_llm = AsyncMock(  # type: ignore[method-assign]
            return_value={"content": "done. All invoices processed."}
        )
        redis = MagicMock()
        redis.is_configured = False
        redis.set_session_state = AsyncMock(return_value=True)
        agent.get_redis = AsyncMock(return_value=redis)  # type: ignore[method-assign]
        agent.tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "ok", "url": "http://x", "title": "t"}
        )
        return agent

    async def test_done_path_awaits_the_finalize(self) -> None:
        agent = self._wired_agent(_new_store())
        agent._finalize_processed_invoices = AsyncMock(  # type: ignore[method-assign]
            return_value={"completed": 2, "flagged": 0, "failed": 0, "skipped": 0, "untouched": 0}
        )
        result = await agent.run("Process all pending invoices")
        self.assertEqual(result["status"], "completed")
        agent._finalize_processed_invoices.assert_awaited_once()
        agent.persist_step.assert_any_call(action="finalize_invoices", result=ANY)
        finalized = [
            c for c in agent.emit_event.await_args_list if c.args[0] == "invoices_finalized"
        ]
        self.assertEqual(len(finalized), 1)

    async def test_done_path_without_invoices_emits_nothing_extra(self) -> None:
        store = _new_store()
        agent = self._wired_agent(store)
        result = await agent.run("Process all pending invoices")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(store["executed"], [], "no invoice SQL may run for an invoice-free run")
        extra = [
            c
            for c in agent.emit_event.await_args_list
            if c.args[0] in ("invoices_finalized",)
        ]
        self.assertEqual(extra, [])
        finalize_steps = [
            c for c in agent.persist_step.await_args_list if c.kwargs.get("action") == "finalize_invoices"
        ]
        self.assertEqual(finalize_steps, [])


# ---------------------------------------------------------------------------
# G3: screenshot run-row is sweep-visible
# ---------------------------------------------------------------------------

class TestScreenshotRunRowLeaseForm(unittest.IsolatedAsyncioTestCase):
    async def _shoot(self, store: Dict[str, Any], run_id: str):
        from backend.tools import take_screenshot

        page = MagicMock()
        page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfake")
        return await take_screenshot(
            page=page,
            run_id=run_id,
            pool=_FakePool(store),  # type: ignore[arg-type]
        )

    async def test_fallback_row_is_orphan_sweep_visible(self) -> None:
        store = _new_store()
        out = await self._shoot(store, str(uuid.uuid4()))
        self.assertTrue(out["success"])
        self.assertTrue(out["persisted"])
        run_inserts = [e for e in store["runs"]]
        self.assertEqual(len(run_inserts), 1)
        sql = run_inserts[0]["sql"]
        lowered = sql.lower()
        # The lease columns must be present: without attempt >= 1 the orphan
        # sweep (status='running' AND attempt > 0 AND lease NULL/expired)
        # can never reclaim the row.
        self.assertIn("attempt", lowered)
        self.assertIn("lease_expires_at", lowered)
        self.assertIn("owner_id", lowered)
        self.assertNotIn("values ($1, 'agent execution run', 'running')", lowered)

    async def test_existing_run_row_is_never_touched(self) -> None:
        store = _new_store()
        run_id = str(uuid.uuid4())
        await self._shoot(store, run_id)
        await self._shoot(store, run_id)
        run_inserts = [e for e in store["runs"]]
        self.assertEqual(len(run_inserts), 2)
        for entry in run_inserts:
            self.assertIn("do nothing", entry["sql"].lower())
        # Both screenshots still persisted their own step rows.
        self.assertEqual(len(store["steps"]), 2)


if __name__ == "__main__":
    unittest.main()
