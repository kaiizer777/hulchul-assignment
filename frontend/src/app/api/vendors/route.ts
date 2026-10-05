import { NextResponse } from 'next/server';
import { unauthorizedIfNoSession } from '@/lib/auth';
import { getDb } from '@/lib/db';
import { VendorDTO } from '@/lib/types';

export const dynamic = 'force-dynamic';

const CANONICAL_VENDORS = [
  'Acme Corp',
  'Bharat Supplies',
  'Nova Tech',
  'Delta Goods',
  'Zenith Parts',
];

function toVendorId(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
}

export async function GET(request: Request) {
  // Checked before the query: the catch block below degrades to the
  // hardcoded CANONICAL_VENDORS array (same VendorDTO[] shape, 200 + an
  // `X-Vendors-Fallback: true` header), so a guard placed after it would be
  // bypassable by anything that could make the database error out.
  const denied = await unauthorizedIfNoSession(request);
  if (denied) return denied;

  try {
    const sql = getDb();
    const rows = (await sql`
      SELECT DISTINCT vendor FROM (
        SELECT vendor FROM purchase_orders
        UNION
        SELECT vendor FROM invoices
      ) AS v
      WHERE vendor IS NOT NULL AND TRIM(vendor) != ''
      ORDER BY vendor ASC
    `) as { vendor: string }[];

    const dbVendors = rows.map((r) => r.vendor.trim());

    // Deduplicate combining CANONICAL_VENDORS and any database vendors
    const vendorMap = new Map<string, string>();

    // Canonical vendors take priority
    for (const vendor of CANONICAL_VENDORS) {
      const id = toVendorId(vendor);
      vendorMap.set(id, vendor);
    }

    // Add any vendors present in DB
    for (const vendor of dbVendors) {
      const id = toVendorId(vendor);
      if (!vendorMap.has(id)) {
        vendorMap.set(id, vendor);
      }
    }

    const vendors: VendorDTO[] = Array.from(vendorMap.entries()).map(([id, name]) => ({
      id,
      name,
    }));

    return NextResponse.json(vendors);
  } catch (err: unknown) {
    // DB down must not take down the vendors directory or the invoice form
    // dropdown: both consumers already accept a plain VendorDTO[] (vendors
    // page guards with Array.isArray; invoices/new only uses res.ok arrays),
    // so serve the canonical list in-shape with a header marker, no client
    // changes required.
    console.error('GET /api/vendors error, serving canonical fallback:', err);
    const vendors: VendorDTO[] = CANONICAL_VENDORS.map((name) => ({
      id: toVendorId(name),
      name,
    }));
    return NextResponse.json(vendors, {
      headers: { 'X-Vendors-Fallback': 'true' },
    });
  }
}
