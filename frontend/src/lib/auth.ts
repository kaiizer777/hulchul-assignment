import { NextResponse } from 'next/server';

/**
 * Session contract. `sub` is always the literal "operator" (single-operator
 * app) and both timestamps are integer UNIX seconds.
 */
export type Session = { sub: string; exp: number };

/**
 * Cookie name is selected by transport only (§2). `__Host-` is rejected by
 * browsers on plain-HTTP localhost, so the dev name must differ; accepting the
 * non-Secure name over HTTPS would let a cookie set by a downgraded origin
 * authenticate.
 */
const SESSION_COOKIE_HTTPS = '__Host-hulchul_session';
const SESSION_COOKIE_HTTP = 'hulchul_session';

/**
 * Raw tokens are unpadded base64url. The bounds are a sanity gate, not the
 * spec: the Redis key is a SHA-256 hex digest, so cookie content can never
 * influence key structure, and the only thing a junk value buys an attacker is
 * a Redis round trip per request.
 */
const RAW_TOKEN_PATTERN = /^[A-Za-z0-9_-]{20,128}$/;

/** Namespaced exclusively by this feature; never shared with agent state. */
const SESSION_KEY_PREFIX = 'hulchul:auth:session:';

/**
 * Redis is on the critical path of every authenticated request. A hung socket
 * must resolve to "no valid session" rather than hold the Worker open until the
 * platform's own limit.
 */
const REDIS_TIMEOUT_MS = 3000;

class SessionLookupFailure extends Error {}

const readCookie = (request: Request, name: string): string | null => {
  const header = request.headers.get('cookie');
  if (!header) return null;

  // First occurrence wins. Browsers order same-name cookies by longest path
  // first, so the first value is the most specific one — taking the last would
  // let a narrower-scoped cookie shadow the host cookie (cookie tossing).
  for (const pair of header.split(';')) {
    const separator = pair.indexOf('=');
    if (separator === -1) continue;
    if (pair.slice(0, separator).trim() !== name) continue;

    const value = pair.slice(separator + 1).trim();
    return value.length > 0 ? value : null;
  }
  return null;
};

const isHttpsRequest = (request: Request): boolean => {
  try {
    const { protocol } = new URL(request.url);
    return protocol === 'https:';
  } catch {
    return false;
  }
};

/**
 * Reads the raw session token from the request cookie.
 *
 * Returns the raw token, not the digest: hashing is a separate step so a caller
 * can decide not to spend the CPU. Returns null when the cookie is absent,
 * empty, or malformed.
 */
export function getSessionToken(request: Request): string | null {
  const hostToken = readCookie(request, SESSION_COOKIE_HTTPS);
  if (hostToken && RAW_TOKEN_PATTERN.test(hostToken)) return hostToken;
  // Never accept the non-Secure name over HTTPS: it would let a cookie set by
  // a downgraded origin authenticate.
  if (isHttpsRequest(request)) return null;
  const httpToken = readCookie(request, SESSION_COOKIE_HTTP);
  if (!httpToken) return null;
  return RAW_TOKEN_PATTERN.test(httpToken) ? httpToken : null;
}

/**
 * Lowercase hex SHA-256 of a raw token, matching the digest the backend stores
 * the Redis key under (§1). Uses standard Web Crypto (crypto.subtle) for
 * edge and runtime portability.
 */
export async function hashSessionToken(token: string): Promise<string> {
  const msgUint8 = new TextEncoder().encode(token);
  const hashBuffer = await crypto.subtle.digest('SHA-256', msgUint8);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Single Redis command over the Upstash REST API, mirroring
 * `backend/redis_client.py:60-90`: POST a JSON array of stringified arguments,
 * unwrap `{"result": ...}`, treat `{"error": ...}` as a failure.
 *
 * Failure messages carry a reason only — never the response body, the
 * Authorization header, or the command's key, which contains the session digest.
 */
const redisCommand = async (
  args: readonly string[],
  url: string,
  token: string
): Promise<unknown> => {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(args.map((arg) => String(arg))),
    signal: AbortSignal.timeout(REDIS_TIMEOUT_MS),
  });

  if (!response.ok) {
    throw new SessionLookupFailure(`redis responded with status ${response.status}`);
  }

  const envelope: unknown = await response.json();
  if (envelope === null || typeof envelope !== 'object' || Array.isArray(envelope)) {
    throw new SessionLookupFailure('redis response was not a JSON object');
  }
  if ('error' in envelope) {
    throw new SessionLookupFailure('redis returned a command error');
  }
  return (envelope as Record<string, unknown>).result ?? null;
};

/** Strict shape check on the stored session. Never widens a malformed value. */
const parseSession = (stored: unknown): Session | null => {
  if (typeof stored !== 'string' || stored.length === 0 || stored.length > 1024) return null;

  let decoded: unknown;
  try {
    decoded = JSON.parse(stored);
  } catch {
    return null;
  }
  if (decoded === null || typeof decoded !== 'object' || Array.isArray(decoded)) return null;

  const { sub, exp } = decoded as Record<string, unknown>;
  if (typeof sub !== 'string' || sub.length === 0) return null;
  if (typeof exp !== 'number' || !Number.isInteger(exp)) return null;

  return { sub, exp };
};

const lookupFailureReason = (err: unknown): string => {
  if (err instanceof SessionLookupFailure) return err.message;
  if (err instanceof Error && err.name === 'TimeoutError') return 'redis request timed out';
  return 'redis request failed';
};

/**
 * Validates the session cookie against Redis.
 *
 * Fails closed in every direction: a missing cookie, an unconfigured or
 * unreachable Redis, a non-200, a malformed envelope, an unparseable payload or
 * a past `exp` all resolve to null. It never throws and never hands back a
 * session it could not verify, because every caller turns null into a 401 and
 * there is no other check downstream in the Worker.
 */
export async function verifySession(request: Request): Promise<Session | null> {
  try {
    const token = getSessionToken(request);
    if (!token) return null;

    const url = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
    if (!url || !redisToken) {
      console.error('[auth] session verification unavailable: Upstash Redis is not configured');
      return null;
    }

    const digest = await hashSessionToken(token);
    const stored = await redisCommand([`GET`, `${SESSION_KEY_PREFIX}${digest}`], url, redisToken);
    if (typeof stored !== 'string' || stored.length === 0) return null;

    const session = parseSession(stored);
    if (!session) {
      console.error('[auth] session verification failed: stored session payload is malformed');
      return null;
    }

    // Defence in depth: the Redis TTL should already have removed this key, but
    // TTL expiry is not a guarantee we get to make on our own.
    if (session.exp <= Math.floor(Date.now() / 1000)) return null;

    return session;
  } catch (err: unknown) {
    console.error(`[auth] session verification failed: ${lookupFailureReason(err)}`);
    return null;
  }
}

/**
 * Guard for the top of every protected route handler:
 *
 *   const denied = await unauthorizedIfNoSession(request);
 *   if (denied) return denied;
 *
 * Returning the response (rather than a boolean) keeps the caller from having to
 * know the status code, so the 401 can never drift between routes.
 */
export async function unauthorizedIfNoSession(request: Request): Promise<NextResponse | null> {
  if (await verifySession(request)) return null;
  return NextResponse.json(
    { error: 'Unauthorized' },
    { status: 401, headers: { 'Cache-Control': 'no-store' } }
  );
}