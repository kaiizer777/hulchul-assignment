import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NextResponse } from 'next/server';

const BACKEND_URL = 'https://backend.test';

/**
 * The session guard is a collaborator, not the subject of this file. It is mocked
 * so the proxy's own behaviour — path mapping, traversal rejection, header
 * handling, stream passthrough — can be asserted deterministically, and so the
 * 401 path is exercised without a Redis round trip.
 */
const guard = vi.fn<() => Promise<NextResponse | null>>();

vi.mock('@/lib/auth', () => ({
  unauthorizedIfNoSession: () => guard(),
}));

vi.mock('@/lib/backend-url', () => ({
  getBackendUrl: () => 'https://backend.test',
}));

const { GET, POST } = await import('@/app/api/agent/[...path]/route');

type RouteContext = { params: Promise<{ path: string[] }> };

const ctx = (path: string[]): RouteContext => ({ params: Promise.resolve({ path }) });

const request = (path: string, init?: RequestInit) =>
  new Request(`https://frontend.test/api/agent/${path}`, init);

const encoder = new TextEncoder();

/** The exact URL and init the handler handed to the stubbed global fetch. */
const upstreamCall = (fetchMock: ReturnType<typeof vi.fn>, index = 0) => {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return { url, init, headers: new Headers(init.headers) };
};

const mockUpstream = (body: BodyInit | null, init: ResponseInit = {}) => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(body, init));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
};

beforeEach(() => {
  guard.mockReset();
  guard.mockResolvedValue(null);
  vi.unstubAllGlobals();
});

describe('path mapping', () => {
  it('maps /api/agent/run to the backend /agent/run', async () => {
    const fetchMock = mockUpstream(JSON.stringify({ run_id: 'r1' }), { status: 200 });

    await POST(request('run', { method: 'POST', body: JSON.stringify({ goal: 'g' }) }), ctx(['run']));

    const { url, init } = upstreamCall(fetchMock);
    expect(url).toBe(`${BACKEND_URL}/agent/run`);
    expect(init.method).toBe('POST');
  });

  it('maps /api/agent/runs/{id}/stream to the backend /agent/runs/{id}/stream', async () => {
    const fetchMock = mockUpstream(null, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });

    await GET(request('runs/run-12345678/stream'), ctx(['runs', 'run-12345678', 'stream']));

    expect(upstreamCall(fetchMock).url).toBe(`${BACKEND_URL}/agent/runs/run-12345678/stream`);
  });

  it('maps /api/agent/steps/{id} to the backend /agent/steps/{id}', async () => {
    const fetchMock = mockUpstream(JSON.stringify({ screenshot_b64: null }));

    await GET(request('steps/step-9'), ctx(['steps', 'step-9']));

    expect(upstreamCall(fetchMock).url).toBe(`${BACKEND_URL}/agent/steps/step-9`);
  });

  it('covers the remaining documented agent paths', async () => {
    const fetchMock = mockUpstream(JSON.stringify({}));

    await GET(request('runs/r1/verification'), ctx(['runs', 'r1', 'verification']));
    await GET(request('runs/r1/approval'), ctx(['runs', 'r1', 'approval']));
    await POST(request('runs/r1/approval'), ctx(['runs', 'r1', 'approval']));
    await POST(request('runs/r1/pause'), ctx(['runs', 'r1', 'pause']));
    await POST(request('runs/r1/resume'), ctx(['runs', 'r1', 'resume']));

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      `${BACKEND_URL}/agent/runs/r1/verification`,
      `${BACKEND_URL}/agent/runs/r1/approval`,
      `${BACKEND_URL}/agent/runs/r1/approval`,
      `${BACKEND_URL}/agent/runs/r1/pause`,
      `${BACKEND_URL}/agent/runs/r1/resume`,
    ]);
  });

  it('forwards the query string and re-encodes segments', async () => {
    const fetchMock = mockUpstream(JSON.stringify({}));

    await GET(request('steps/a b'), ctx(['steps', 'a b']));

    expect(upstreamCall(fetchMock).url).toBe(`${BACKEND_URL}/agent/steps/a%20b`);
  });
});

describe('session guard', () => {
  it('returns 401 and never calls upstream when unauthenticated', async () => {
    const fetchMock = mockUpstream(JSON.stringify({}));
    guard.mockResolvedValue(NextResponse.json({ error: 'Unauthorized' }, { status: 401 }));

    const res = await GET(request('runs/r1/verification'), ctx(['runs', 'r1', 'verification']));

    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects an unauthenticated request before validating the path', async () => {
    // Authentication must not depend on the request being otherwise well formed.
    const fetchMock = mockUpstream(JSON.stringify({}));
    guard.mockResolvedValue(NextResponse.json({ error: 'Unauthorized' }, { status: 401 }));

    const res = await POST(request('..'), ctx(['..']));

    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('path traversal guard', () => {
  it.each([
    ['literal dot-dot', ['..', 'run']],
    ['percent-encoded dot-dot', ['%2e%2e', 'run']],
    ['uppercase percent-encoded dot-dot', ['%2E%2E', 'run']],
    ['dot-dot inside a segment', ['runs', '..', 'stream']],
    ['embedded slash', ['runs/../..', 'stream']],
    ['backslash', ['runs\\..\\..', 'stream']],
    ['null byte', ['run%00.json']],
    ['empty segment', ['']],
    ['no segment at all', []],
  ])('returns 400 and never calls upstream for %s', async (_label, path) => {
    const fetchMock = mockUpstream(JSON.stringify({}));

    const res = await POST(request('run'), ctx(path));

    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: 'Invalid path' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('cannot be double-encoded into a traversal', async () => {
    // Segments are decoded exactly once, so `%252e%252e` reaches the backend as the
    // literal text `%2e%2e` after the backend's own single decode -- never as `..`.
    // Asserting the *forwarded* path is what keeps that true.
    const fetchMock = mockUpstream(JSON.stringify({}));

    await POST(request('run'), ctx(['runs', '%252e%252e', 'stream']));

    expect(upstreamCall(fetchMock).url).toBe(`${BACKEND_URL}/agent/runs/%252e%252e/stream`);
    const forwarded = new URL(upstreamCall(fetchMock).url).pathname.split('/').slice(2);
    expect(forwarded.map(decodeURIComponent)).not.toContain('..');
  });

  it('refuses a segment that is not valid percent-encoding', async () => {
    // `%c0%ae%c0%ae` is an overlong UTF-8 encoding of `..`. decodeURIComponent
    // throws on it, and forwarding it undecoded would hand the backend bytes that
    // some other decoder could turn back into a traversal.
    const fetchMock = mockUpstream(JSON.stringify({}));

    const res = await POST(request('run'), ctx(['runs', '%c0%ae%c0%ae', 'stream']));

    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('SSE passthrough', () => {
  const sseBody = () =>
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: ping\ndata: {}\n\n'));
        controller.enqueue(
          encoder.encode('event: step_complete\ndata: {"step_index":1}\n\n')
        );
        controller.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
        controller.close();
      },
    });

  it('delivers the upstream chunks intact and strips Connection', async () => {
    const body = sseBody();
    const fetchMock = mockUpstream(body, {
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'yes',
      },
    });

    const res = await GET(request('runs/r1/stream'), ctx(['runs', 'r1', 'stream']));

    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/event-stream');
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(res.headers.get('x-accel-buffering')).toBe('no');
    // Hop-by-hop, and a forbidden header name in Workers: constructing a Response
    // with it throws outright.
    expect(res.headers.get('connection')).toBeNull();

    const received: string[] = [];
    const reader = res.body!.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received.push(new TextDecoder().decode(value));
    }

    expect(received.length).toBe(3);
    expect(received.join('')).toBe(
      'event: ping\ndata: {}\n\n' +
        'event: step_complete\ndata: {"step_index":1}\n\n' +
        'event: done\ndata: {}\n\n'
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('responds without waiting for the stream to finish', async () => {
    // A handler that awaited .text()/.json() on this would never resolve, so the
    // test failing by timeout is the regression signal.
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: ping\ndata: {}\n\n'));
      },
      pull() {
        // Deliberately never closes: the run is still going.
      },
    });
    mockUpstream(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });

    const res = await GET(request('runs/r1/stream'), ctx(['runs', 'r1', 'stream']));

    expect(res.headers.get('content-type')).toBe('text/event-stream');
    expect(await res.body!.getReader().read()).toMatchObject({ done: false });
  });

  it('does not treat a stream path answering with JSON as an event stream', async () => {
    mockUpstream(JSON.stringify({ detail: 'Not authenticated' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });

    const res = await GET(request('runs/r1/stream'), ctx(['runs', 'r1', 'stream']));

    expect(res.status).toBe(401);
    expect(res.headers.get('content-type')).toBe('application/json');
    expect(await res.json()).toEqual({ detail: 'Not authenticated' });
  });
});

describe('non-SSE passthrough', () => {
  it('forwards the upstream status and body', async () => {
    mockUpstream(JSON.stringify({ run_id: 'run-12345678', status: 'running' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

    const res = await POST(request('run', { method: 'POST', body: '{}' }), ctx(['run']));

    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/json');
    expect(await res.json()).toEqual({ run_id: 'run-12345678', status: 'running' });
  });

  it('relays an upstream error status verbatim', async () => {
    mockUpstream(JSON.stringify({ detail: 'Run not found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });

    const res = await GET(request('runs/missing/verification'), ctx(['runs', 'missing', 'verification']));

    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: 'Run not found' });
  });

  it('reports an unreachable backend as 502 without leaking the cause', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')));
    vi.spyOn(console, 'error').mockImplementation(() => {});

    const res = await POST(request('run', { method: 'POST', body: '{}' }), ctx(['run']));

    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body).toEqual({ error: 'Upstream backend unavailable' });
    expect(JSON.stringify(body)).not.toContain('fetch failed');
  });
});

describe('upstream request construction', () => {
  it('forwards the session cookie', async () => {
    const fetchMock = mockUpstream(JSON.stringify({}));

    await GET(request('runs/r1/approval', { headers: { Cookie: 'hulchul_session=raw-token' } }), ctx([
      'runs',
      'r1',
      'approval',
    ]));

    expect(upstreamCall(fetchMock).headers.get('cookie')).toBe('hulchul_session=raw-token');
  });

  it('forwards the content type and body on POST', async () => {
    const fetchMock = mockUpstream(JSON.stringify({ run_id: 'r1' }), { status: 200 });

    await POST(
      request('run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: 'process invoices' }),
      }),
      ctx(['run'])
    );

    const { init, headers } = upstreamCall(fetchMock);
    expect(headers.get('content-type')).toBe('application/json');
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe(
      JSON.stringify({ goal: 'process invoices' })
    );
  });

  it('forwards only the allowlisted headers', async () => {
    const fetchMock = mockUpstream(JSON.stringify({}));

    await GET(
      request('runs/r1/verification', {
        headers: {
          'X-Forwarded-For': '203.0.113.7',
          'X-Test-Mode': 'true',
          Host: 'evil.example.com',
        },
      }),
      ctx(['runs', 'r1', 'verification'])
    );

    const { headers } = upstreamCall(fetchMock);
    expect(headers.get('x-forwarded-for')).toBeNull();
    expect(headers.get('x-test-mode')).toBeNull();
    expect(headers.get('host')).toBeNull();
    expect([...headers.keys()]).toEqual([]);
  });

  it('binds the upstream fetch to the client abort signal', async () => {
    const fetchMock = mockUpstream(JSON.stringify({}));
    const controller = new AbortController();

    await GET(request('runs/r1/approval', { signal: controller.signal }), ctx([
      'runs',
      'r1',
      'approval',
    ]));

    // Request clones the signal it is constructed with, so identity is not
    // preserved; what matters is that a client disconnect reaches the upstream
    // call and stops the work rather than orphaning it.
    const upstreamSignal = upstreamCall(fetchMock).init.signal as AbortSignal;
    expect(upstreamSignal).toBeDefined();
    expect(upstreamSignal.aborted).toBe(false);

    controller.abort();
    expect(upstreamSignal.aborted).toBe(true);
  });
});