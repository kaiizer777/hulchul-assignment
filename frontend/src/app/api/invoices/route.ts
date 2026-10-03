import { NextResponse } from 'next/server';
import { unauthorizedIfNoSession } from '@/lib/auth';
import { getDb } from '@/lib/db';
import { CreateInvoiceSchema, formatInvoice, RawInvoiceRow } from '@/lib/types';

export const dynamic = 'force-dynamic';

/**
 * Module-level counter tracking invoice submissions for simulated failure testing in Phase 6.
 */
let invoiceSubmissionCount = 0;

/**
 * Determines whether to simulate an internal server error based on `fail_after` query parameter
 * or `SIMULATE_FAILURE_AFTER` environment variable, or resets the counter if `reset_failure=true`.
 * In production failure injection is disabled unless `ENABLE_TEST_FAILURE_INJECTION` is set;
 * client-supplied headers are never consulted.
 *
 * @param request - The incoming HTTP request (unused; headers must not enable injection).
 * @param url - The incoming request URL containing search parameters.
 * @returns True if failure should be simulated, false otherwise.
 */
function shouldSimulateFailure(request: Request, url: URL): boolean {
  void request;
  const isProd = process.env.NODE_ENV === 'production';
  const enableTestInjection = process.env.ENABLE_TEST_FAILURE_INJECTION === 'true';

  if (isProd && !enableTestInjection) {
    return false;
  }

  const resetFailure = url.searchParams.get('reset_failure');
  if (resetFailure === 'true') {
    invoiceSubmissionCount = 0;
    return false;
  }

  const failAfterStr = url.searchParams.get('fail_after') || process.env.SIMULATE_FAILURE_AFTER;
  if (!failAfterStr) {
    return false;
  }

  if (!/^\d+$/.test(failAfterStr)) {
    throw new Error('INVALID_FAIL_AFTER');
  }

  const failAfter = parseInt(failAfterStr, 10);
  invoiceSubmissionCount += 1;
  return invoiceSubmissionCount >= failAfter;
}

/**
 * Handles GET requests to retrieve all invoices ordered by creation date and ID descending.
 *
 * @param request - The incoming HTTP request, used for the session guard.
 * @returns NextResponse containing the array of formatted invoices or an error response.
 */
export async function GET(request: Request) {
  // Re-verified per request inside the handler, before any query runs. Route
  // handlers are public endpoints regardless of what the UI links to, so the
  // session cannot be checked anywhere less direct than this.
  const denied = await unauthorizedIfNoSession(request);
  if (denied) return denied;

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

/**
 * Handles POST requests to create a new invoice with validation and Phase 6 failure injection support.
 *
 * @param request - The incoming HTTP request containing JSON invoice payload and optional query parameters.
 * @returns NextResponse with the created invoice, validation errors, or simulated ERP failure.
 */
export async function POST(request: Request) {
  // Guard first: an unauthenticated caller must not be able to reach the failure
  // injection counter, the validator or the database.
  const denied = await unauthorizedIfNoSession(request);
  if (denied) return denied;

  try {
    const url = new URL(request.url);
    try {
      if (shouldSimulateFailure(request, url)) {
        return NextResponse.json(
          { error: 'Simulated ERP internal server error' },
          { status: 500 }
        );
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.message === 'INVALID_FAIL_AFTER') {
        return NextResponse.json(
          { error: 'Invalid fail_after parameter: must be a pure positive integer' },
          { status: 400 }
        );
      }
      throw err;
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

