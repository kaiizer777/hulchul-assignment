import asyncio
import base64
import unittest
import uuid
from httpx import AsyncClient, ASGITransport

from backend.config import settings
from backend.db import init_db_pool, close_db_pool, get_db_pool
from backend.browser import get_browser_session
from backend.tools import (
    navigate,
    read_page,
    click,
    fill,
    select,
    take_screenshot,
    check_exists,
    PlaywrightTools,
    TOOL_DEFINITIONS,
)
from backend.main import app


class TestPhase23Tools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db_pool()

    async def asyncTearDown(self):
        await close_db_pool()

    async def test_01_tool_definitions_schema(self):
        """Phase 2.3: Verify all 7 Playwright tools are defined with valid OpenAI/Groq schemas."""
        self.assertEqual(len(TOOL_DEFINITIONS), 7)
        tool_names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
        expected_names = [
            "navigate",
            "read_page",
            "click",
            "fill",
            "select",
            "take_screenshot",
            "check_exists",
        ]
        self.assertEqual(tool_names, expected_names)

        for tool in TOOL_DEFINITIONS:
            self.assertEqual(tool["type"], "function")
            fn = tool["function"]
            self.assertIn("name", fn)
            self.assertIn("description", fn)
            self.assertIn("parameters", fn)
            self.assertEqual(fn["parameters"]["type"], "object")

    async def test_02_check_exists_idempotency(self):
        """
        Phase 2.3: Unit test: idempotency check returns exists: true for duplicate,
        exists: false for new.
        """
        # 1. Check existing purchase order (PO-1001 was seeded)
        po_existing = await check_exists("purchase_order", "PO-1001")
        self.assertTrue(po_existing["exists"], "PO-1001 must exist in seed data")
        self.assertEqual(po_existing["entity_type"], "purchase_order")
        self.assertIsNotNone(po_existing["record"])
        self.assertEqual(po_existing["record"]["po_number"], "PO-1001")

        # 2. Check non-existent purchase order
        po_new = await check_exists("purchase_order", "PO-NONEXISTENT-9999")
        self.assertFalse(po_new["exists"], "PO-NONEXISTENT-9999 should not exist")
        self.assertIsNone(po_new["record"])

        # 3. Check existing invoice (PO-1002 was seeded)
        inv_existing = await check_exists("invoice", "PO-1002")
        self.assertTrue(inv_existing["exists"], "Invoice with PO-1002 must exist in seed data")
        self.assertEqual(inv_existing["entity_type"], "invoice")
        self.assertIsNotNone(inv_existing["record"])
        self.assertEqual(inv_existing["record"]["po_number"], "PO-1002")

        # 4. Check non-existent invoice
        inv_new = await check_exists("invoice", "PO-NONEXISTENT-9999")
        self.assertFalse(inv_new["exists"], "Non-existent invoice should return exists: false")
        self.assertIsNone(inv_new["record"])

        # 5. Check existing vendor
        vendor_existing = await check_exists("vendor", "Nova Tech")
        self.assertTrue(vendor_existing["exists"], "Vendor 'Nova Tech' must exist")
        self.assertIsNotNone(vendor_existing["record"])

        # 6. Check non-existent vendor
        vendor_new = await check_exists("vendor", "Ghost Vendor Corp")
        self.assertFalse(vendor_new["exists"], "Ghost Vendor Corp should not exist")
        self.assertIsNone(vendor_new["record"])

    async def test_03_browser_tools_execution(self):
        """
        Phase 2.3: Unit test: each tool function (navigate, fill, click, read_page, select)
        returns correct shape and operates on remote CDP session.
        """
        async with get_browser_session() as session:
            page = session.page

            # 1. Test navigate
            res_nav = await navigate(page, "about:blank")
            self.assertTrue(res_nav["success"])
            self.assertEqual(res_nav["url"], "about:blank")
            self.assertEqual(res_nav["status"], 200)

            # Set up a test form inside the browser session
            html_content = """
            <!DOCTYPE html>
            <html>
            <head><title>Mock ERP Form</title></head>
            <body>
                <h1>Invoice Entry</h1>
                <nav><a href="#ledger">View Invoices</a></nav>
                <form id="invoice-form" onsubmit="event.preventDefault(); document.getElementById('status').innerText = 'Submitted';">
                    <div>
                        <label for="vendor">Vendor</label>
                        <select id="vendor">
                            <option value="">Select Vendor</option>
                            <option value="Acme Corp">Acme Corp</option>
                            <option value="Nova Tech">Nova Tech</option>
                        </select>
                    </div>
                    <div>
                        <label for="amount">Amount (USD)</label>
                        <input id="amount" type="number" placeholder="0.00" />
                    </div>
                    <div>
                        <label for="po_number">PO Number</label>
                        <input id="po_number" type="text" placeholder="e.g. PO-1001" />
                    </div>
                    <button type="submit" id="submit-btn">Create Invoice</button>
                    <p id="status">Pending</p>
                </form>
            </body>
            </html>
            """
            await page.set_content(html_content)

            # 2. Test read_page (accessibility tree snapshot)
            res_read = await read_page(page)
            self.assertTrue(res_read["success"])
            self.assertIn("Invoice Entry", res_read["snapshot"])
            self.assertIn("Create Invoice", res_read["snapshot"])
            self.assertGreater(res_read["size_bytes"], 0)

            # 3. Test select (select dropdown option by label)
            res_select = await select(page, "Vendor", "Acme Corp")
            self.assertTrue(res_select["success"], f"select failed: {res_select.get('error')}")
            self.assertEqual(res_select["selected"], "Acme Corp")
            selected_val = await page.locator("#vendor").input_value()
            self.assertEqual(selected_val, "Acme Corp")

            # 4. Test fill (form fields)
            res_fill_amount = await fill(page, "Amount (USD)", 42000)
            self.assertTrue(res_fill_amount["success"], f"fill amount failed: {res_fill_amount.get('error')}")
            self.assertEqual(res_fill_amount["value"], "42000")
            amt_val = await page.locator("#amount").input_value()
            self.assertEqual(amt_val, "42000")

            res_fill_po = await fill(page, "PO Number", "PO-1001")
            self.assertTrue(res_fill_po["success"], f"fill po failed: {res_fill_po.get('error')}")
            po_val = await page.locator("#po_number").input_value()
            self.assertEqual(po_val, "PO-1001")

            # 5. Test click (by accessibility label 'Create Invoice')
            res_click = await click(page, "Create Invoice")
            self.assertTrue(res_click["success"], f"click failed: {res_click.get('error')}")
            self.assertTrue(res_click["clicked"])
            status_text = await page.locator("#status").inner_text()
            self.assertEqual(status_text, "Submitted")

    async def test_04_take_screenshot_and_persistence(self):
        """
        Phase 2.3: Unit test: take_screenshot captures base64 buffer and persists
        to Neon agent_steps table when run_id is supplied.
        """
        async with get_browser_session() as session:
            page = session.page
            await page.set_content("<html><body><h1>Screenshot Test</h1></body></html>")

            # A. Test take_screenshot without run_id (in-memory buffer)
            res_raw = await take_screenshot(page)
            self.assertTrue(res_raw["success"])
            self.assertIsNotNone(res_raw["screenshot_b64"])
            self.assertGreater(res_raw["size_bytes"], 100)
            self.assertFalse(res_raw["persisted"])

            # Verify it is valid base64 PNG
            raw_bytes = base64.b64decode(res_raw["screenshot_b64"])
            self.assertTrue(raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

            # B. Test take_screenshot with run_id (persists to Neon agent_steps)
            test_run_id = str(uuid.uuid4())
            res_persisted = await take_screenshot(
                page=page,
                run_id=test_run_id,
                action="test_verification",
                result="decision point reached",
            )
            self.assertTrue(res_persisted["success"])
            self.assertTrue(res_persisted["persisted"])
            self.assertIsNotNone(res_persisted["step_id"])

            # Verify in Neon database
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                step_row = await conn.fetchrow(
                    "SELECT step_id, run_id, action, result, screenshot_b64 FROM agent_steps WHERE step_id = $1",
                    uuid.UUID(res_persisted["step_id"]),
                )
                self.assertIsNotNone(step_row)
                self.assertEqual(str(step_row["run_id"]), test_run_id)
                self.assertEqual(step_row["action"], "test_verification")
                self.assertEqual(step_row["result"], "decision point reached")
                self.assertEqual(step_row["screenshot_b64"], res_persisted["screenshot_b64"])

    async def test_05_playwright_tools_class_dispatcher(self):
        """
        Phase 2.3: Unit test: PlaywrightTools class and dynamic .execute() dispatcher.
        """
        async with get_browser_session() as session:
            tools = PlaywrightTools(page=session.page)

            # Test execute navigate
            res_nav = await tools.execute("navigate", {"url": "about:blank"})
            self.assertTrue(res_nav["success"])

            await session.page.set_content("<!DOCTYPE html><html><head><style>body { width: 800px; height: 600px; }</style></head><body><button id='b'>Click Me</button></body></html>")
            await asyncio.sleep(0.5)

            # Test execute click
            res_click = await tools.execute("click", {"selector": "Click Me"})
            self.assertTrue(res_click["success"], f"tools.execute click failed: {res_click.get('error')}")

            # Test execute check_exists
            res_check = await tools.execute("check_exists", {"entity_type": "purchase_order", "identifier": "PO-1001"})
            self.assertTrue(res_check["exists"])

            # Test execute unknown tool
            res_unknown = await tools.execute("invalid_action", {})
            self.assertFalse(res_unknown["success"])
            self.assertIn("Unknown tool", res_unknown["error"])

    async def test_06_fastapi_tools_endpoints(self):
        """
        Phase 2.3: Verify FastAPI /tools and /tools/check-exists endpoints.
        """
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Test GET /tools
            res_tools = await client.get("/tools")
            self.assertEqual(res_tools.status_code, 200)
            data_tools = res_tools.json()
            self.assertEqual(data_tools["count"], 7)
            self.assertEqual(len(data_tools["tools"]), 7)

            # Test POST /tools/check-exists (existing PO)
            res_check_exist = await client.post(
                "/tools/check-exists",
                json={"entity_type": "purchase_order", "identifier": "PO-1001"},
            )
            self.assertEqual(res_check_exist.status_code, 200)
            data_check_exist = res_check_exist.json()
            self.assertTrue(data_check_exist["exists"])
            self.assertEqual(data_check_exist["identifier"], "PO-1001")

            # Test POST /tools/check-exists (non-existent PO)
            res_check_missing = await client.post(
                "/tools/check-exists",
                json={"entity_type": "purchase_order", "identifier": "PO-MISSING-000"},
            )
            self.assertEqual(res_check_missing.status_code, 200)
            data_check_missing = res_check_missing.json()
            self.assertFalse(data_check_missing["exists"])


if __name__ == "__main__":
    unittest.main()
