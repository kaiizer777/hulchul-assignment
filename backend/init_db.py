import asyncio
import os
import sys
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
import asyncpg

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in environment or backend/.env", file=sys.stderr)
    sys.exit(1)

CREATE_SCHEMA_SQL = """
-- Ensure UUID generator functions exist
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. invoices
CREATE TABLE IF NOT EXISTS invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    date DATE NOT NULL,
    po_number TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. purchase_orders
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    approved_amount NUMERIC NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

-- 3. agent_runs
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. agent_steps
CREATE TABLE IF NOT EXISTS agent_steps (
    step_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    result TEXT,
    screenshot_b64 TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Foreign key & query performance indexes
DROP INDEX IF EXISTS idx_agent_steps_run_id;
-- Leading (run_id, timestamp) serves every per-run step listing and their
-- ORDER BY timestamp. The trailing step_id completes the key the SSE durable
-- poll needs: its row comparison (timestamp, step_id) > ($2, $3) spans two
-- columns, so it can only become an index qual if both are in the index.
-- Without it the comparison degrades to a filter over the run's steps.
CREATE INDEX IF NOT EXISTS idx_agent_steps_run_timestamp ON agent_steps(run_id, timestamp ASC, step_id ASC);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_po_number ON invoices(po_number);
"""

VERIFY_TABLES_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('invoices', 'purchase_orders', 'agent_runs', 'agent_steps')
ORDER BY table_name;
"""

VERIFY_COLUMNS_SQL = """
SELECT 
    table_name,
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('invoices', 'purchase_orders', 'agent_runs', 'agent_steps')
ORDER BY table_name, ordinal_position;
"""

async def init_db():
    # Remove sslmode query param for asyncpg if present, and handle ssl arg
    parsed = urlparse(DATABASE_URL)
    query_params = parse_qs(parsed.query)
    
    # asyncpg handles standard postgresql:// connection strings
    print("Connecting to Neon Postgres...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print("Executing schema DDL...")
        await conn.execute(CREATE_SCHEMA_SQL)
        print("Schema DDL applied successfully.")

        print("\n--- Verifying Tables in information_schema.tables ---")
        tables = await conn.fetch(VERIFY_TABLES_SQL)
        table_names = [row["table_name"] for row in tables]
        for name in table_names:
            print(f"  [TABLE] {name}")

        print("\n--- Verifying Columns in information_schema.columns ---")
        columns = await conn.fetch(VERIFY_COLUMNS_SQL)
        current_table = None
        for col in columns:
            if col["table_name"] != current_table:
                current_table = col["table_name"]
                print(f"\nTable: {current_table}")
                print(f"{'Column':<20} {'Data Type':<20} {'Nullable':<10} {'Default'}")
                print("-" * 75)
            print(f"{col['column_name']:<20} {col['data_type']:<20} {col['is_nullable']:<10} {str(col['column_default'])}")

        return table_names, columns
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(init_db())
