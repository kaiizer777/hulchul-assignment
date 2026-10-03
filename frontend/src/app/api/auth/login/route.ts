import { NextResponse } from 'next/server';
import { z } from 'zod';

import { getBackendUrl } from '@/lib/backend-url';
import { relayAuthResponse } from '../relay';

export const dynamic = 'force-dynamic';

/**
 * Mirrors the backend's `LoginRequest` (§5): a single JSON field, no extras.
 * The upper bound is a DoS control, not a policy — the upstream argon2 verify
 * cost scales with the password length, so an unbounded string would let an
 * unauthenticated caller buy server CPU with one request.
 */
const LoginRequestSchema = z.object({ password: z.string().min(1).max(1024) }).strict();

/**
 * argon2id with m=65536,t=3,p=4 plus a Redis round trip. Generous relative to the
 * expected ~200ms, bounded so a stuck Lambda cannot pin the Worker.
 */
const UPSTREAM_TIMEOUT_MS = 15_000;

/**
 * POST /api/auth/login — session establishment is delegated to the backend,
 * which owns the password hash and the Redis write. This handler validates the
 * body, forwards it, and relays the upstream response (including `Set-Cookie`)
 * back to the browser on this origin.
 *
 * Deliberately not session-guarded: this is the credential check.
 */
export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON payload' }, { status: 400 });
  }

  const parsed = LoginRequestSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({ error: 'Validation failed' }, { status: 400 });
  }

  try {
    const upstream = await fetch(`${getBackendUrl()}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ password: parsed.data.password }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)]),
    });
    return await relayAuthResponse(upstream);
  } catch {
    // The submitted password is never included in the log line.
    console.error('[auth] POST /api/auth/login upstream request failed');
    return NextResponse.json({ error: 'Sign-in service unavailable' }, { status: 503 });
  }
}