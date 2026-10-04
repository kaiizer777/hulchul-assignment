import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { NextRequest } from 'next/server';
import { middleware } from '@/middleware';
import * as auth from '@/lib/auth';

describe('Auth & Routing Middleware', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const createReq = (path: string, hasCookie = false): NextRequest => {
    const headers = new Headers();
    if (hasCookie) {
      headers.set('cookie', '__Host-hulchul_session=mock_valid_token_string_1234567890');
    }
    return new NextRequest(new URL(`https://hulchul-frontend.sufiyanx.workers.dev${path}`), {
      headers,
    });
  };

  it('redirects unauthenticated user visiting / to /login', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue(null);
    vi.spyOn(auth, 'getSessionToken').mockReturnValue(null);

    const req = createReq('/');
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe('https://hulchul-frontend.sufiyanx.workers.dev/login');
  });

  it('redirects authenticated user visiting / to /agent', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue({ sub: 'operator', exp: 9999999999 });
    vi.spyOn(auth, 'getSessionToken').mockReturnValue('mock_valid_token_string_1234567890');

    const req = createReq('/', true);
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe('https://hulchul-frontend.sufiyanx.workers.dev/agent');
  });

  it('allows unauthenticated user to access /login', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue(null);
    vi.spyOn(auth, 'getSessionToken').mockReturnValue(null);

    const req = createReq('/login');
    const res = await middleware(req);

    // Next.js NextResponse.next() returns a response without location header / 200
    expect(res.headers.get('location')).toBeNull();
  });

  it('redirects authenticated user visiting /login to /agent', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue({ sub: 'operator', exp: 9999999999 });
    vi.spyOn(auth, 'getSessionToken').mockReturnValue('mock_valid_token_string_1234567890');

    const req = createReq('/login', true);
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe('https://hulchul-frontend.sufiyanx.workers.dev/agent');
  });

  it('redirects unauthenticated user visiting /agent to /login', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue(null);
    vi.spyOn(auth, 'getSessionToken').mockReturnValue(null);

    const req = createReq('/agent');
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe('https://hulchul-frontend.sufiyanx.workers.dev/login');
  });

  it('allows authenticated user to access /agent', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue({ sub: 'operator', exp: 9999999999 });
    vi.spyOn(auth, 'getSessionToken').mockReturnValue('mock_valid_token_string_1234567890');

    const req = createReq('/agent', true);
    const res = await middleware(req);

    expect(res.headers.get('location')).toBeNull();
  });

  it('redirects unauthenticated user visiting /invoices to /login with returnTo', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue(null);
    vi.spyOn(auth, 'getSessionToken').mockReturnValue(null);

    const req = createReq('/invoices');
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe(
      'https://hulchul-frontend.sufiyanx.workers.dev/login?returnTo=%2Finvoices'
    );
  });

  it('redirects unauthenticated user visiting /purchase-orders to /login with returnTo', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue(null);
    vi.spyOn(auth, 'getSessionToken').mockReturnValue(null);

    const req = createReq('/purchase-orders');
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe(
      'https://hulchul-frontend.sufiyanx.workers.dev/login?returnTo=%2Fpurchase-orders'
    );
  });

  it('redirects unauthenticated user visiting /vendors to /login with returnTo', async () => {
    vi.spyOn(auth, 'verifySession').mockResolvedValue(null);
    vi.spyOn(auth, 'getSessionToken').mockReturnValue(null);

    const req = createReq('/vendors');
    const res = await middleware(req);

    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toBe(
      'https://hulchul-frontend.sufiyanx.workers.dev/login?returnTo=%2Fvendors'
    );
  });

  it('bypasses API routes and static files', async () => {
    const apiReq = createReq('/api/invoices');
    const apiRes = await middleware(apiReq);
    expect(apiRes.headers.get('location')).toBeNull();

    const staticReq = createReq('/_next/static/chunks/main.js');
    const staticRes = await middleware(staticReq);
    expect(staticRes.headers.get('location')).toBeNull();
  });
});
