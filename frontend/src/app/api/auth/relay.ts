import { NextResponse } from 'next/server';

/**
 * Response headers that must not be copied from the upstream response onto the
 * one this Worker returns.
 *
 * Hop-by-hop headers describe the Worker→Lambda connection and mean nothing on
 * the Worker→browser hop (`Connection` is a forbidden header name in Workers and
 * would make the `Response` constructor throw). `content-encoding` and
 * `content-length` are excluded because the body is re-materialised from
 * `upstream.text()`: fetch has already decoded it, so relaying either would make
 * the browser decode a second time and fail.
 */
const STRIPPED_RESPONSE_HEADERS = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  'content-encoding',
  'content-length',
]);

/** Statuses the fetch spec forbids from carrying a body. */
const NULL_BODY_STATUSES = new Set([101, 204, 205, 304]);

const readSetCookies = (headers: Headers): string[] => {
  // `Headers.forEach` folds repeated set-cookie values into one comma-joined
  // string, which browsers then parse as a single broken cookie. Only the
  // dedicated accessor keeps them apart.
  if (typeof headers.getSetCookie === 'function') return headers.getSetCookie();
  const folded = headers.get('set-cookie');
  return folded ? [folded] : [];
};

/**
 * Re-emits an upstream auth response on this origin, forwarding `Set-Cookie`
 * verbatim and exactly once.
 *
 * The relay is the whole point of proxying login: the session cookie must land
 * on the `workers.dev` origin rather than on the Lambda domain, and cookie
 * scoping is decided by the `Set-Cookie` attributes alone — so rewriting it here
 * would either break `__Host-` or silently widen the cookie's scope.
 */
export const relayAuthResponse = async (upstream: Response): Promise<NextResponse> => {
  const headers = new Headers();

  upstream.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (lower === 'set-cookie') return;
    if (STRIPPED_RESPONSE_HEADERS.has(lower)) return;
    headers.append(name, value);
  });

  for (const cookie of readSetCookies(upstream.headers)) {
    headers.append('Set-Cookie', cookie);
  }

  headers.set('Cache-Control', 'no-store');

  const body = NULL_BODY_STATUSES.has(upstream.status) ? null : await upstream.text();

  return new NextResponse(body, { status: upstream.status, headers });
};