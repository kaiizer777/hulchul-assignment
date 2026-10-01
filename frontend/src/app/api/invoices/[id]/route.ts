import { NextResponse } from 'next/server';
import { z } from 'zod';
import { getDb } from '@/lib/db';
import {
  formatInvoice,
  RawInvoiceRow,
  UpdateInvoiceStatusSchema,
} from '@/lib/types';

export const dynamic = 'force-dynamic';

const UuidSchema = z.string().uuid();

export async function GET(
  _request: Request,
  props: { params: Promise<{ id: string }> }
) {
  try {
    const { id } = await props.params;

    if (!UuidSchema.safeParse(id).success) {
      return NextResponse.json({ error: 'Not found' }, { status: 404 });
    }

    const sql = getDb();
    const rows = (await sql`
      SELECT id, vendor, amount, date::text as date, po_number, status, created_at
      FROM invoices
      WHERE id = ${id}
      LIMIT 1
    `) as RawInvoiceRow[];

    if (!rows || rows.length === 0) {
      return NextResponse.json({ error: 'Not found' }, { status: 404 });
    }

    return NextResponse.json(formatInvoice(rows[0]));
  } catch (err: unknown) {
    console.error('GET /api/invoices/[id] error:', err);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

export async function PATCH(
  request: Request,
  props: { params: Promise<{ id: string }> }
) {
  try {
    const { id } = await props.params;

    if (!UuidSchema.safeParse(id).success) {
      return NextResponse.json({ error: 'Not found' }, { status: 404 });
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json(
        { error: 'Invalid JSON payload' },
        { status: 400 }
      );
    }

    const parseResult = UpdateInvoiceStatusSchema.safeParse(body);
    if (!parseResult.success) {
      return NextResponse.json(
        {
          error: 'Validation failed',
          details: parseResult.error.flatten().fieldErrors,
        },
        { status: 400 }
      );
    }

    const { status } = parseResult.data;

    const sql = getDb();
    const rows = (await sql`
      UPDATE invoices
      SET status = ${status}
      WHERE id = ${id}
      RETURNING id, vendor, amount, date::text as date, po_number, status, created_at
    `) as RawInvoiceRow[];

    if (!rows || rows.length === 0) {
      return NextResponse.json({ error: 'Not found' }, { status: 404 });
    }

    return NextResponse.json(formatInvoice(rows[0]));
  } catch (err: unknown) {
    console.error('PATCH /api/invoices/[id] error:', err);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
