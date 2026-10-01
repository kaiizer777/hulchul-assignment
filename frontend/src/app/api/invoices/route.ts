import { NextResponse } from 'next/server';
import { getDb } from '@/lib/db';
import { CreateInvoiceSchema, formatInvoice, RawInvoiceRow } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const sql = getDb();
    const rows = (await sql`
      SELECT id, vendor, amount, date::text as date, po_number, status, created_at
      FROM invoices
      ORDER BY created_at DESC, id DESC
    `) as RawInvoiceRow[];

    const invoices = rows.map(formatInvoice);
    return NextResponse.json(invoices);
  } catch (err: unknown) {
    console.error('GET /api/invoices error:', err);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

export async function POST(request: Request) {
  try {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json(
        { error: 'Invalid JSON payload' },
        { status: 400 }
      );
    }

    const parseResult = CreateInvoiceSchema.safeParse(body);
    if (!parseResult.success) {
      return NextResponse.json(
        {
          error: 'Validation failed',
          details: parseResult.error.flatten().fieldErrors,
        },
        { status: 400 }
      );
    }

    const { vendor, amount, date, po_number, status } = parseResult.data;

    const sql = getDb();
    const rows = (await sql`
      INSERT INTO invoices (vendor, amount, date, po_number, status)
      VALUES (${vendor}, ${amount}, ${date}, ${po_number}, ${status})
      RETURNING id, vendor, amount, date::text as date, po_number, status, created_at
    `) as RawInvoiceRow[];

    if (!rows || rows.length === 0) {
      return NextResponse.json(
        { error: 'Failed to create invoice' },
        { status: 500 }
      );
    }

    const createdInvoice = formatInvoice(rows[0]);
    return NextResponse.json(createdInvoice, { status: 201 });
  } catch (err: unknown) {
    console.error('POST /api/invoices error:', err);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
