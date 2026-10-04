import { NextResponse } from 'next/server';

import { getBackendUrl } from '@/lib/backend-url';
import { relayAuthResponse } from '../relay';

export const dynamic = 'force-dynamic';

/**
 * Bounded so a stuck Lambda cannot pin the Worker. Logout is a single Redis
 * DEL, so this is pure headroom.
 */
const UPSTREAM_TIMEOUT_MS = 15_000;

/**
 * POST /api/auth/logout — revocation is the backend's job and it can only
 * delete the session key if it can read the token (§5), so the incoming Cookie
 * header is forwarded verbatim. The upstream clearing `Set-Cookie` is relayed
 * back untouched.
 *
 * Not session-guarded: the backend's logout is idempotent and must succeed with
 * no cookie or an already-dead token.
 */
export async function POST(request: Request) {
  const cookieHeader = request.headers.get('cookie');

  try {
    const upstream = await fetch(`${getBackendUrl()}/auth/logout`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        ...(cookieHeader ? { Cookie: cookieHeader } : {}),
      },
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)]),
    });
    return await relayAuthResponse(upstream, request);
  } catch {
    // No local cookie clear on this path. Without the upstream DELETE the Redis
    // key survives, so clearing the cookie would only hide a still-valid session
    // from the operator while leaving the token usable.
    console.error('[auth] POST /api/auth/logout upstream request failed');
    return NextResponse.json({ error: 'Sign-in service unavailable' }, { status: 503 });
  }
}