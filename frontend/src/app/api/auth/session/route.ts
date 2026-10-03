import { NextResponse } from 'next/server';

import { verifySession } from '@/lib/auth';

export const dynamic = 'force-dynamic';

/**
 * GET /api/auth/session — resolved entirely inside the Worker against Redis.
 * There is no upstream hop: the browser learns whether it holds a live cookie,
 * and nothing else. The response body is the `Session` DTO and nothing else, so
 * no stored session field can leak by accident.
 */
export async function GET(request: Request) {
  const session = await verifySession(request);

  if (!session) {
    return NextResponse.json(
      { error: 'Unauthorized' },
      { status: 401, headers: { 'Cache-Control': 'no-store' } }
    );
  }

  return NextResponse.json(session, { headers: { 'Cache-Control': 'no-store' } });
}