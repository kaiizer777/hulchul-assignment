import asyncio
import os
import sys
from decimal import Decimal
from datetime import date
from dotenv import load_dotenv
import asyncpg

# Load environment variables from backend/.env
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in environment or backend/.env", file=sys.stderr)
    sys.exit(1)

# Synthetic seed data
VENDORS = [
    "Acme Corp",
    "Bharat Supplies",
    "Nova Tech",
    "Delta Goods",
    "Zenith Parts"
]

PURCHASE_ORDERS = [
    {"po_number": "PO-1001", "vendor": "Acme Corp", "approved_amount": Decimal("25000.00"), "status": "active"},
    {"po_number": "PO-1002", "vendor": "Bharat Supplies", "approved_amount": Decimal("40000.00"), "status": "active"},
    {"po_number": "PO-1003", "vendor": "Nova Tech", "approved_amount": Decimal("15000.00"), "status": "active"},
    {"po_number": "PO-1004", "vendor": "Delta Goods", "approved_amount": Decimal("30000.00"), "status": "active"},
    {"po_number": "PO-1005", "vendor": "Zenith Parts", "approved_amount": Decimal("20000.00"), "status": "active"},
    {"po_number": "PO-1006", "vendor": "Acme Corp", "approved_amount": Decimal("60000.00"), "status": "active"},
    {"po_number": "PO-1007", "vendor": "Bharat Supplies", "approved_amount": Decimal("75000.00"), "status": "active"},
    {"po_number": "PO-1008", "vendor": "Nova Tech", "approved_amount": Decimal("35000.00"), "status": "active"},
]

INVOICES = [
    # Case 1: 4 valid invoices (PO exists, discrepancy <= 10%, amount <= 50,000)
    {"vendor": "Acme Corp", "amount": Decimal("25500.00"), "date": date(2026, 9, 15), "po_number": "PO-1001", "status": "pending"},
    {"vendor": "Bharat Supplies", "amount": Decimal("39000.00"), "date": date(2026, 9, 18), "po_number": "PO-1002", "status": "pending"},
    {"vendor": "Nova Tech", "amount": Decimal("15000.00"), "date": date(2026, 9, 20), "po_number": "PO-1003", "status": "pending"},
    {"vendor": "Delta Goods", "amount": Decimal("32000.00"), "date": date(2026, 9, 22), "po_number": "PO-1004", "status": "pending"},

    # Case 2: 2 mismatched invoices (PO exists, discrepancy > 10%, amount <= 50,000)
    {"vendor": "Zenith Parts", "amount": Decimal("26000.00"), "date": date(2026, 9, 24), "po_number": "PO-1005", "status": "pending"},
    {"vendor": "Nova Tech", "amount": Decimal("28000.00"), "date": date(2026, 9, 25), "po_number": "PO-1008", "status": "pending"},

    # Case 3: 2 missing PO invoices (po_number does not exist in purchase_orders)
    {"vendor": "Delta Goods", "amount": Decimal("18500.00"), "date": date(2026, 9, 26), "po_number": "PO-9991", "status": "pending"},
    {"vendor": "Acme Corp", "amount": Decimal("22000.00"), "date": date(2026, 9, 27), "po_number": "PO-9992", "status": "pending"},

    # Case 4: 2 over threshold invoices (PO exists, amount > 50,000)
    {"vendor": "Acme Corp", "amount": Decimal("62000.00"), "date": date(2026, 9, 28), "po_number": "PO-1006", "status": "pending"},
    {"vendor": "Bharat Supplies", "amount": Decimal("76500.00"), "date": date(2026, 9, 29), "po_number": "PO-1007", "status": "pending"},
]


async def seed_and_verify():
    print("Connecting to Neon Postgres...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        async with conn.transaction():
            print("Cleaning existing invoices and purchase_orders...")
            await conn.execute("DELETE FROM invoices;")
            await conn.execute("DELETE FROM purchase_orders;")

            print(f"Inserting {len(PURCHASE_ORDERS)} purchase orders...")
            for po in PURCHASE_ORDERS:
                await conn.execute(
                    """
                    INSERT INTO purchase_orders (po_number, vendor, approved_amount, status)
                    VALUES ($1, $2, $3, $4)
                    """,
                    po["po_number"],
                    po["vendor"],
                    po["approved_amount"],
                    po["status"]
                )

            print(f"Inserting {len(INVOICES)} invoices...")
            for inv in INVOICES:
                await conn.execute(
                    """
                    INSERT INTO invoices (vendor, amount, date, po_number, status)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    inv["vendor"],
                    inv["amount"],
                    inv["date"],
                    inv["po_number"],
                    inv["status"]
                )

        print("\n--- SEED VERIFICATION ---")
        po_count = await conn.fetchval("SELECT COUNT(*) FROM purchase_orders;")
        inv_count = await conn.fetchval("SELECT COUNT(*) FROM invoices;")
        print(f"Total Purchase Orders: {po_count} (Expected: 8)")
        print(f"Total Invoices: {inv_count} (Expected: 10)")

        assert po_count == 8, f"Expected 8 POs, got {po_count}"
        assert inv_count == 10, f"Expected 10 Invoices, got {inv_count}"

        query = """
        SELECT 
            i.id,
            i.vendor AS inv_vendor,
            i.amount AS inv_amount,
            i.date AS inv_date,
            i.po_number AS inv_po,
            i.status AS inv_status,
            po.approved_amount AS po_approved_amount,
            po.status AS po_status
        FROM invoices i
        LEFT JOIN purchase_orders po ON i.po_number = po.po_number
        ORDER BY i.date ASC;
        """
        rows = await conn.fetch(query)

        classification_counts = {
            "Case 1: Valid": 0,
            "Case 2: Mismatched": 0,
            "Case 3: Missing PO": 0,
            "Case 4: Over Threshold (>50k)": 0,
        }

        print("\n" + "=" * 110)
        print(f"{'PO Number':<10} {'Vendor':<16} {'Inv Amount':<12} {'PO Approved':<12} {'Discrepancy %':<15} {'Status':<10} {'Classification'}")
        print("=" * 110)

        for r in rows:
            po_num = r["inv_po"] or "None"
            vendor = r["inv_vendor"]
            inv_amt = r["inv_amount"]
            po_amt = r["po_approved_amount"]
            status = r["inv_status"]

            if po_amt is None:
                discrepancy_str = "N/A"
                classification = "Case 3: Missing PO"
                classification_counts["Case 3: Missing PO"] += 1
            else:
                diff_pct = (abs(inv_amt - po_amt) / po_amt) * Decimal("100")
                discrepancy_str = f"{diff_pct:.2f}%"
                if inv_amt > Decimal("50000.00"):
                    classification = "Case 4: Over Threshold (>50k)"
                    classification_counts["Case 4: Over Threshold (>50k)"] += 1
                elif diff_pct > Decimal("10.00"):
                    classification = "Case 2: Mismatched"
                    classification_counts["Case 2: Mismatched"] += 1
                else:
                    classification = "Case 1: Valid"
                    classification_counts["Case 1: Valid"] += 1

            po_amt_str = f"${po_amt:,.2f}" if po_amt is not None else "NULL"
            inv_amt_str = f"${inv_amt:,.2f}"
            print(f"{po_num:<10} {vendor:<16} {inv_amt_str:<12} {po_amt_str:<12} {discrepancy_str:<15} {status:<10} {classification}")

        print("=" * 110)
        print("\nSummary of Invoice Classifications:")
        for cat, cnt in classification_counts.items():
            print(f"  - {cat}: {cnt}")

        assert classification_counts["Case 1: Valid"] == 4, f"Expected 4 valid, got {classification_counts['Case 1: Valid']}"
        assert classification_counts["Case 2: Mismatched"] == 2, f"Expected 2 mismatched, got {classification_counts['Case 2: Mismatched']}"
        assert classification_counts["Case 3: Missing PO"] == 2, f"Expected 2 missing PO, got {classification_counts['Case 3: Missing PO']}"
        assert classification_counts["Case 4: Over Threshold (>50k)"] == 2, f"Expected 2 over threshold, got {classification_counts['Case 4: Over Threshold (>50k)']}"

        print("\nALL VERIFICATIONS PASSED SUCCESSFULLY!")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_and_verify())
