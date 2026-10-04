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
 * Session cookie names, mirroring the backend (`backend/auth.py`) and the
 * browser-hop reader (`@/lib/auth`): `__Host-` iff the hop is https.
 */
const SESSION_COOKIE_HTTPS = '__Host-hulchul_session';
const SESSION_COOKIE_HTTP = 'hulchul_session';

/**
 * True only when the browser-facing hop is positively plain HTTP. Uses the
 * same signal as the reader (`new URL(request.url).protocol`), so the relay
 * emits exactly the name the reader will look for. Unparseable URLs fail
 * toward verbatim (https behaviour): never translate on ambiguity.
 */
const isPlainHttpRequest = (request: Request): boolean => {
  try {
    return new URL(request.url).protocol === 'http:';
  } catch {
    return false;
  }
};

/**
 * Translates the session cookie for a plain-http browser hop: `__Host-`
 * prefix removed, bare `Secure` attribute dropped (it would contradict the
 * plain name, which is never Secure — same shape the backend emits over
 * plain http). Only the exact session cookie is touched; every other
 * `Set-Cookie` and every other attribute (Path, HttpOnly, SameSite, Max-Age)
 * passes through byte-for-byte, so logout clearing (`Max-Age=0`) keeps
 * working on the translated name.
 */
const forPlainHttpBrowser = (cookie: string): string => {
  const separator = cookie.indexOf(';');
  const first = separator === -1 ? cookie : cookie.slice(0, separator);
  const rest = separator === -1 ? [] : cookie.slice(separator + 1).split(';');
  const equals = first.indexOf('=');
  if (equals === -1) return cookie;
  if (first.slice(0, equals).trim() !== SESSION_COOKIE_HTTPS) return cookie;
  const kept = rest.filter((attr) => attr.trim().toLowerCase() !== 'secure');
  return [`${SESSION_COOKIE_HTTP}=${first.slice(equals + 1)}`, ...kept].join(';');
};

/**
 * Re-emits an upstream auth response on this origin, forwarding `Set-Cookie`
 * exactly once.
 *
 * The relay is the whole point of proxying login: the session cookie must land
 * on this origin rather than on the backend domain, and cookie scoping is
 * decided by the `Set-Cookie` attributes alone.
 *
 * Transport reconciliation: the backend names the cookie for *its* hop
 * (`__Host-`+Secure on https) while the browser-hop reader (`@/lib/auth`)
 * reads exactly one name for *its* hop. When both hops share a transport the
 * upstream cookie is already correct and is forwarded verbatim. When the hops
 * are crossed (plain-http browser, https backend — e.g. a local plain-http
 * frontend pointed at the production backend), the verbatim `__Host-` cookie
 * is unusable: the transport-strict reader ignores it and every navigation
 * bounces back to `/login?returnTo=…`. So for a positively plain-http
 * browser hop only, the session cookie is translated to exactly what the
 * backend itself would have emitted over plain http (plain name, no Secure);
 * everything else passes through untouched.
 */
export const relayAuthResponse = async (upstream: Response, request: Request): Promise<NextResponse> => {
  const headers = new Headers();

  upstream.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (lower === 'set-cookie') return;
    if (STRIPPED_RESPONSE_HEADERS.has(lower)) return;
    headers.append(name, value);
  });

  const plainHttp = isPlainHttpRequest(request);
  for (const cookie of readSetCookies(upstream.headers)) {
    headers.append('Set-Cookie', plainHttp ? forPlainHttpBrowser(cookie) : cookie);
  }

  headers.set('Cache-Control', 'no-store');

  const body = NULL_BODY_STATUSES.has(upstream.status) ? null : await upstream.text();

  return new NextResponse(body, { status: upstream.status, headers });
};