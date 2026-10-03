import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from 'vitest';

import {
  getSessionToken,
  hashSessionToken,
  unauthorizedIfNoSession,
  verifySession,
} from '@/lib/auth';
import { resolveReturnTo } from '@/lib/return-to';

/**
 * A syntactically valid raw session token: unpadded base64url, no reserved
 * cookie characters. Its SHA-256 digest below is a hard-coded literal so this
 * suite can never pass by re-running the implementation it is meant to pin.
 */
const RAW_TOKEN = 'kR8vQ2mZxT7pLwN4yB6cH9jK1mP3rS5tU7vX9zA1bC3dE5fG7hI9jK0';
const RAW_TOKEN_SHA256 = 'c52324e9402fc85937ae7bd3ccb1b5886b583783f597b6ef1c0f5ebb3d7f258e';
const SESSION_KEY = `hulchul:auth:session:${RAW_TOKEN_SHA256}`;

const HTTPS_URL = 'https://hulchul-frontend.sufiyanx.workers.dev';
const HTTP_URL = 'http://localhost:3051';
const REDIS_URL = 'https://mature-doberman-225309.upstash.io';
const REDIS_TOKEN = 'test-only-redis-token';
const BACKEND_URL = 'https://backend.test';

const PROD_COOKIE = '__Host-hulchul_session';
const DEV_COOKIE = 'hulchul_session';

const ENV_KEYS = [
  'UPSTASH_REDIS_REST_URL',
  'UPSTASH_REDIS_REST_TOKEN',
  'NEXT_PUBLIC_BACKEND_URL',
] as const;

const originalEnv: Record<string, string | undefined> = {};
const originalFetch = global.fetch;

const setEnv = (key: (typeof ENV_KEYS)[number], value: string | undefined) => {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
};

const configureRedis = () => {
  setEnv('UPSTASH_REDIS_REST_URL', REDIS_URL);
  setEnv('UPSTASH_REDIS_REST_TOKEN', REDIS_TOKEN);
};

const requestWithCookie = (
  cookie: string | null,
  url: string = HTTPS_URL
): Request =>
  new Request(`${url}/api/auth/session`, {
    method: 'GET',
    headers: cookie ? { Cookie: cookie } : {},
  });

const futureExp = (): number => Math.floor(Date.now() / 1000) + 3600;
const pastExp = (): number => Math.floor(Date.now() / 1000) - 1;

const redisResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

/** Upstash REST success envelope: `{"result": <string|null>}`. */
const redisGetReturning = (result: unknown) =>
  redisResponse({ result });

beforeEach(() => {
  for (const key of ENV_KEYS) originalEnv[key] = process.env[key];
  configureRedis();
  setEnv('NEXT_PUBLIC_BACKEND_URL', BACKEND_URL);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  for (const key of ENV_KEYS) setEnv(key, originalEnv[key]);
  vi.restoreAllMocks();
});

afterAll(() => {
  global.fetch = originalFetch;
});

describe('getSessionToken', () => {
  it('reads the __Host- cookie over HTTPS', () => {
    expect(getSessionToken(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).toBe(RAW_TOKEN);
  });

  it('reads the plain dev cookie over plain HTTP', () => {
    expect(
      getSessionToken(requestWithCookie(`${DEV_COOKIE}=${RAW_TOKEN}`, HTTP_URL))
    ).toBe(RAW_TOKEN);
  });

  it('returns null when no cookie header is present', () => {
    expect(getSessionToken(requestWithCookie(null))).toBeNull();
  });

  it('returns null when the header carries unrelated cookies only', () => {
    expect(getSessionToken(requestWithCookie('theme=dark; locale=en-GB'))).toBeNull();
  });

  it('returns null for an empty cookie value', () => {
    expect(getSessionToken(requestWithCookie(`${PROD_COOKIE}=`))).toBeNull();
  });

  it('does not accept the non-Secure dev name over HTTPS', () => {
    // Accepting it would let a cookie set on a downgraded origin authenticate.
    expect(getSessionToken(requestWithCookie(`${DEV_COOKIE}=${RAW_TOKEN}`))).toBeNull();
  });

  it('does not accept the __Host- name over plain HTTP', () => {
    expect(getSessionToken(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`, HTTP_URL))).toBeNull();
  });

  it('tolerates other cookies around the session cookie', () => {
    expect(
      getSessionToken(requestWithCookie(`theme=dark; ${PROD_COOKIE}=${RAW_TOKEN}; locale=en-GB`))
    ).toBe(RAW_TOKEN);
  });

  it('takes the first value when the name repeats, so a scoped cookie cannot shadow', () => {
    // Browsers order same-name cookies longest-path-first, so the first value is
    // the most specific one.
    const header = `${PROD_COOKIE}=${RAW_TOKEN}; ${PROD_COOKIE}=attacker-value`;
    expect(getSessionToken(requestWithCookie(header))).toBe(RAW_TOKEN);
  });

  it('rejects a value that is not unpadded base64url before any hashing cost', () => {
    expect(
      getSessionToken(requestWithCookie(`${PROD_COOKIE}=has spaces and; separators`))
    ).toBeNull();
  });
});

describe('hashSessionToken', () => {
  it('matches the published SHA-256("abc") vector', () => {
    // NIST vector: the digest is a constant of the algorithm, not of this code.
    return expect(hashSessionToken('abc')).resolves.toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    );
  });

  it('produces the exact digest of a real raw token', async () => {
    await expect(hashSessionToken(RAW_TOKEN)).resolves.toBe(RAW_TOKEN_SHA256);
  });

  it('returns lowercase hex of 64 characters', async () => {
    const digest = await hashSessionToken(RAW_TOKEN);
    expect(digest).toMatch(/^[0-9a-f]{64}$/);
  });

  it('is not stable across different tokens', async () => {
    await expect(hashSessionToken(`${RAW_TOKEN}x`)).resolves.not.toBe(RAW_TOKEN_SHA256);
  });
});

describe('verifySession', () => {
  it('returns the session on a happy-path Redis GET', async () => {
    const exp = futureExp();
    global.fetch = vi.fn().mockResolvedValue(
      redisGetReturning(JSON.stringify({ sub: 'operator', iat: exp - 3600, exp }))
    );

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toEqual({
      sub: 'operator',
      exp,
    });
  });

  it('issues the Upstash REST wire format the backend expects', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      redisGetReturning(JSON.stringify({ sub: 'operator', exp: futureExp() }))
    );

    await verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];

    expect(url).toBe(REDIS_URL);
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({
      Authorization: `Bearer ${REDIS_TOKEN}`,
      'Content-Type': 'application/json',
    });
    expect(JSON.parse(init.body as string)).toEqual(['GET', SESSION_KEY]);
  });

  it('returns null when the cookie is absent without touching Redis', async () => {
    global.fetch = vi.fn();

    await expect(verifySession(requestWithCookie(null))).resolves.toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns null when Redis reports a command error', async () => {
    global.fetch = vi.fn().mockResolvedValue(redisResponse({ error: 'ERR unexpected' }));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when fetch rejects', async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError('network error'));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when the result is null (no such key)', async () => {
    global.fetch = vi.fn().mockResolvedValue(redisGetReturning(null));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when exp is in the past even though the key still exists', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      redisGetReturning(JSON.stringify({ sub: 'operator', exp: pastExp() }))
    );

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when exp is exactly now', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      redisGetReturning(JSON.stringify({ sub: 'operator', exp: Math.floor(Date.now() / 1000) }))
    );

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when both Redis bindings are missing', async () => {
    setEnv('UPSTASH_REDIS_REST_URL', undefined);
    setEnv('UPSTASH_REDIS_REST_TOKEN', undefined);
    global.fetch = vi.fn();

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
    // Nothing to call, and no accidental "unconfigured means allow".
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns null when only the token binding is missing', async () => {
    setEnv('UPSTASH_REDIS_REST_TOKEN', undefined);
    global.fetch = vi.fn();

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns null when only the URL binding is missing', async () => {
    setEnv('UPSTASH_REDIS_REST_URL', undefined);
    global.fetch = vi.fn();

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns null on a non-200 response', async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(new Response('upstream exploded', { status: 500 }));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when the response body is not JSON', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response('<html>gateway</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      })
    );

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when the envelope is a JSON array', async () => {
    global.fetch = vi.fn().mockResolvedValue(redisResponse(['GET', SESSION_KEY]));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('returns null when the stored payload is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValue(redisGetReturning('{not json'));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it.each([
    ['sub missing', { exp: 4102444800 }],
    ['sub not a string', { sub: 42, exp: 4102444800 }],
    ['sub empty', { sub: '', exp: 4102444800 }],
    ['exp missing', { sub: 'operator' }],
    ['exp not a number', { sub: 'operator', exp: '4102444800' }],
    ['exp fractional', { sub: 'operator', exp: 4102444800.5 }],
    ['exp NaN-producing', { sub: 'operator', exp: null }],
  ])('returns null when the stored session has %s', async (_label, payload) => {
    global.fetch = vi.fn().mockResolvedValue(redisGetReturning(JSON.stringify(payload)));

    await expect(verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))).resolves.toBeNull();
  });

  it('never throws, whatever Redis does', async () => {
    global.fetch = vi.fn().mockImplementation(() => {
      throw new Error('synchronous explosion');
    });

    await expect(
      verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))
    ).resolves.toBeNull();
  });

  it('never writes the token, its digest or the Redis credential to the log', async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError('network error'));

    await verifySession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    const logged = (console.error as unknown as ReturnType<typeof vi.fn>).mock.calls
      .flat()
      .map(String)
      .join(' ');
    expect(logged).not.toContain(RAW_TOKEN);
    expect(logged).not.toContain(RAW_TOKEN_SHA256);
    expect(logged).not.toContain(REDIS_TOKEN);
  });
});

describe('unauthorizedIfNoSession', () => {
  it('returns a 401 NextResponse when the session is invalid', async () => {
    global.fetch = vi.fn().mockResolvedValue(redisGetReturning(null));

    const denied = await unauthorizedIfNoSession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    expect(denied).not.toBeNull();
    expect(denied?.status).toBe(401);
    await expect(denied?.json()).resolves.toEqual({ error: 'Unauthorized' });
    expect(denied?.headers.get('Cache-Control')).toBe('no-store');
  });

  it('returns a 401 when no cookie is presented at all', async () => {
    global.fetch = vi.fn();

    const denied = await unauthorizedIfNoSession(requestWithCookie(null));

    expect(denied?.status).toBe(401);
  });

  it('returns null when the session verifies', async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(redisGetReturning(JSON.stringify({ sub: 'operator', exp: futureExp() })));

    await expect(
      unauthorizedIfNoSession(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`))
    ).resolves.toBeNull();
  });
});

describe('POST /api/auth/login', () => {
  const loginRequest = (payload: unknown): Request =>
    new Request(`${HTTPS_URL}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: typeof payload === 'string' ? payload : JSON.stringify(payload),
    });

const upstreamLogin = (status: number, body: unknown, cookies: string[]) => {
      const upstream = new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
      cookies.forEach((cookie) => upstream.headers.append('Set-Cookie', cookie));
      return upstream;
    };

  it('relays the upstream Set-Cookie verbatim, exactly once, and the status', async () => {
    const cookie = `${PROD_COOKIE}=${RAW_TOKEN}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=86400`;
    global.fetch = vi.fn().mockResolvedValue(upstreamLogin(200, { sub: 'operator', exp: futureExp() }, [cookie]));

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest({ password: 'correct horse battery staple' }));

    expect(response.status).toBe(200);
    expect(response.headers.getSetCookie()).toEqual([cookie]);
    await expect(response.json()).resolves.toEqual({ sub: 'operator', exp: expect.any(Number) });
  });

  it('keeps multiple Set-Cookie headers separate instead of folding them', async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(upstreamLogin(200, { sub: 'operator', exp: 1 }, ['a=1; Path=/', 'b=2; Path=/']));

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest({ password: 'pw' }));

    expect(response.headers.getSetCookie()).toEqual(['a=1; Path=/', 'b=2; Path=/']);
  });

  it('strips hop-by-hop and body-encoding headers from the relayed response', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ sub: 'operator', exp: 1 }), {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          Connection: 'keep-alive',
          'Transfer-Encoding': 'chunked',
          'Content-Encoding': 'gzip',
          'Content-Length': '999',
        },
      })
    );

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest({ password: 'pw' }));

    expect(response.headers.get('connection')).toBeNull();
    expect(response.headers.get('transfer-encoding')).toBeNull();
    expect(response.headers.get('content-encoding')).toBeNull();
    expect(response.headers.get('content-length')).toBeNull();
    expect(response.headers.get('content-type')).toBe('application/json');
  });

  it('forwards the credential to the backend as JSON', async () => {
    global.fetch = vi.fn().mockResolvedValue(upstreamLogin(200, { sub: 'operator', exp: 1 }, []));

    const { POST } = await import('@/app/api/auth/login/route');
    await POST(loginRequest({ password: 'correct horse battery staple' }));

    const [url, init] = (global.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe(`${BACKEND_URL}/auth/login`);
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body as string)).toEqual({ password: 'correct horse battery staple' });
  });

  it('relays a 429 with its Retry-After intact', async () => {
    const upstream = new Response(JSON.stringify({ detail: 'Too many failures' }), {
      status: 429,
      headers: { 'Content-Type': 'application/json', 'Retry-After': '900' },
    });
    global.fetch = vi.fn().mockResolvedValue(upstream);

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest({ password: 'pw' }));

    expect(response.status).toBe(429);
    expect(response.headers.get('Retry-After')).toBe('900');
  });

  it.each([
    ['a non-object body', JSON.stringify('password')],
    ['a missing password', JSON.stringify({})],
    ['an empty password', JSON.stringify({ password: '' })],
    ['a non-string password', JSON.stringify({ password: 1234 })],
    ['unknown extra fields', JSON.stringify({ password: 'pw', role: 'admin' })],
  ])('rejects %s with 400 and never calls the backend', async (_label, body) => {
    global.fetch = vi.fn();

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest(body));

    expect(response.status).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects an unparseable JSON body with 400', async () => {
    global.fetch = vi.fn();

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest('{ not json'));

    expect(response.status).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('bounds the password length so argon2 cannot be used as a CPU amplifier', async () => {
    global.fetch = vi.fn();

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest({ password: 'a'.repeat(5000) }));

    expect(response.status).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 503 without leaking the submitted password when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError('network error'));

    const { POST } = await import('@/app/api/auth/login/route');
    const response = await POST(loginRequest({ password: 'super-secret-guess' }));

    expect(response.status).toBe(503);
    const body = await response.text();
    expect(body).not.toContain('super-secret-guess');

    const logged = (console.error as unknown as ReturnType<typeof vi.fn>).mock.calls
      .flat()
      .map(String)
      .join(' ');
    expect(logged).not.toContain('super-secret-guess');
  });
});

describe('POST /api/auth/logout', () => {
  it('forwards the cookie so the backend can revoke, and relays the clearing Set-Cookie', async () => {
    const clearing = `${PROD_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;
    global.fetch = vi.fn().mockResolvedValue(
      new Response(null, { status: 204, headers: { 'Set-Cookie': clearing } })
    );

    const { POST } = await import('@/app/api/auth/logout/route');
    const response = await POST(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    expect(response.status).toBe(204);
    expect(response.headers.getSetCookie()).toEqual([clearing]);
    expect(await response.text()).toBe('');

    const [url, init] = (global.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe(`${BACKEND_URL}/auth/logout`);
    expect(init.method).toBe('POST');
    expect(init.headers.Cookie).toBe(`${PROD_COOKIE}=${RAW_TOKEN}`);
  });

  it('omits the Cookie header when the browser has none, and still forwards', async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));

    const { POST } = await import('@/app/api/auth/logout/route');
    const response = await POST(requestWithCookie(null));

    expect(response.status).toBe(204);
    const [, init] = (global.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers.Cookie).toBeUndefined();
  });

  it('never logs the presented token when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError('network error'));

    const { POST } = await import('@/app/api/auth/logout/route');
    const response = await POST(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    // No local cookie clear: the Redis key would survive, so clearing would only
    // hide a live session.
    expect(response.headers.getSetCookie()).toEqual([]);
    expect(response.status).toBe(503);

    const logged = (console.error as unknown as ReturnType<typeof vi.fn>).mock.calls
      .flat()
      .map(String)
      .join(' ');
    expect(logged).not.toContain(RAW_TOKEN);
    expect(logged).not.toContain(RAW_TOKEN_SHA256);
  });
});

describe('GET /api/auth/session', () => {
  it('returns the session DTO with no upstream hop', async () => {
    const exp = futureExp();
    global.fetch = vi
      .fn()
      .mockResolvedValue(redisGetReturning(JSON.stringify({ sub: 'operator', iat: exp - 60, exp })));

    const { GET } = await import('@/app/api/auth/session/route');
    const response = await GET(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    expect(response.status).toBe(200);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    await expect(response.json()).resolves.toEqual({ sub: 'operator', exp });
    // Exactly one call: the Redis GET. Nothing is proxied.
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledWith(REDIS_URL, expect.objectContaining({ method: 'POST' }));
  });

  it('returns exactly the two documented fields and no stored extras', async () => {
    const exp = futureExp();
    global.fetch = vi.fn().mockResolvedValue(
      redisGetReturning(
        JSON.stringify({ sub: 'operator', iat: exp - 60, exp, password_hash: 'leak-me' })
      )
    );

    const { GET } = await import('@/app/api/auth/session/route');
    const response = await GET(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    await expect(response.json()).resolves.toEqual({ sub: 'operator', exp });
  });

  it('returns 401 when there is no session', async () => {
    global.fetch = vi.fn().mockResolvedValue(redisGetReturning(null));

    const { GET } = await import('@/app/api/auth/session/route');
    const response = await GET(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ error: 'Unauthorized' });
  });

  it('returns 401 when Redis is unconfigured rather than granting access', async () => {
    setEnv('UPSTASH_REDIS_REST_TOKEN', undefined);
    global.fetch = vi.fn();

    const { GET } = await import('@/app/api/auth/session/route');
    const response = await GET(requestWithCookie(`${PROD_COOKIE}=${RAW_TOKEN}`));

    expect(response.status).toBe(401);
  });
});

describe('login returnTo open-redirect guard', () => {
  const ORIGIN = 'https://app.invalid';

  it.each([
    ['/agent', '/agent'],
    ['/', '/'],
    ['/agent/runs/run-1234', '/agent/runs/run-1234'],
    ['/invoices?status=pending', '/invoices?status=pending'],
  ])('accepts %s', (candidate, expected) => {
    expect(resolveReturnTo(candidate)).toBe(expected);
  });

  it.each([
    ['a protocol-relative authority', '//evil.com'],
    ['a backslash authority', '/\\evil.com'],
    ['a backslash-prefixed authority', '\\\\evil.com'],
    ['a mixed backslash authority', '/\\/evil.com'],
    ['a bare double slash', '//'],
    ['a triple slash', '///evil.com'],
    ['an absolute https URL', 'https://evil.com'],
    ['an absolute http URL', 'http://evil.com'],
    ['a scheme-relative backslash', '\\\\evil.com/path'],
    ['a bare relative path', 'agent'],
    ['an empty string', ''],
    ['whitespace only', ' '],
    ['a leading space then a slash', ' //evil.com'],
    ['a javascript URL', 'javascript:alert(1)'],
    ['a data URL', 'data:text/html,<script>alert(1)</script>'],
  ])('rejects %s', (_label, candidate) => {
    expect(resolveReturnTo(candidate)).toBe('/agent');
  });

  it.each([[null], [undefined]])('falls back to the default for %s', (candidate) => {
    expect(resolveReturnTo(candidate as string | null | undefined)).toBe('/agent');
  });

  it('never resolves to an off-origin URL, whatever the input', () => {
    const hostile = [
      '//evil.com',
      '/\\evil.com',
      '\\\\evil.com',
      '/\\/evil.com',
      '//',
      '///evil.com',
      'https://evil.com',
      'http://evil.com',
      'agent',
      '',
      'javascript:alert(1)',
      '/%2f%2fevil.com',
      '/%5c%5cevil.com',
      '/\t/evil.com',
      '/\n/evil.com',
      '/ /evil.com',
      '/..//evil.com',
    ];

    for (const candidate of hostile) {
      const resolved = resolveReturnTo(candidate);
      expect(new URL(resolved, ORIGIN).origin).toBe(ORIGIN);
      expect(resolved.startsWith('/')).toBe(true);
    }
  });

  it('keeps the accepted value byte-for-byte rather than a normalised form', () => {
    expect(resolveReturnTo('/agent?a=1&b=%20')).toBe('/agent?a=1&b=%20');
  });
});