import { NextResponse } from 'next/server';
import { getDb } from '@/lib/db';
import { PurchaseOrderDTO } from '@/lib/types';

export const dynamic = 'force-dynamic';

interface RawPurchaseOrderRow {
  po_number: string;
  vendor: string;
  approved_amount: string | number;
  status: string;
}

export async function GET() {
  try {
    const sql = getDb();
    const rows = (await sql`
      SELECT po_number, vendor, approved_amount, status
      FROM purchase_orders
      ORDER BY po_number ASC
    `) as RawPurchaseOrderRow[];

    const purchaseOrders: PurchaseOrderDTO[] = rows.map((row) => ({
      po_number: row.po_number,
      vendor: row.vendor,
      approved_amount: Number(row.approved_amount),
      status: row.status,
    }));

    return NextResponse.json(purchaseOrders);
  } catch (err: unknown) {
    console.error('GET /api/purchase-orders error:', err);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
