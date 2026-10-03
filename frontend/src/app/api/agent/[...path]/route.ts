import { NextResponse } from 'next/server';
import { unauthorizedIfNoSession } from '@/lib/auth';
import { getBackendUrl } from '@/lib/backend-url';

export const dynamic = 'force-dynamic';

type AgentRouteContext = { params: Promise<{ path: string[] }> };

/**
 * A segment may only ever name one backend path component. `..`, `/`, `\` and NUL
 * are rejected rather than normalised away, so a traversal attempt can never be
 * quietly rewritten into a legitimate upstream path.
 */
const FORBIDDEN_SEGMENT = /(?:\.\.|[/\\\0])/;

/**
 * Decodes a segment exactly once, then rejects it if the decoded form could
 * escape its component.
 *
 * Decoding first is what makes `%2e%2e` and `%2E%2E` fail the same way a literal
 * `..` does. Decoding only once means a double-encoded value survives as the
 * literal text `%2e%2e`, which is re-encoded on the way out and therefore still
 * cannot traverse. A segment that is not valid percent-encoding is refused
 * outright rather than forwarded as-is.
 */
const encodeSegment = (segment: string): string | null => {
  let decoded: string;
  try {
    decoded = decodeURIComponent(segment);
  } catch {
    return null;
  }
  return FORBIDDEN_SEGMENT.test(decoded) ? null : encodeURIComponent(decoded);
};

/**
 * Rebuilds the backend path from validated segments only.
 *
 * The `/api` prefix is dropped and `/agent` restored, which is also what confines
 * this proxy to the backend's `/agent` surface: no client-supplied segment can
 * select a different prefix.
 */
const buildUpstreamPath = (segments: string[]): string | null => {
  if (segments.length === 0) return null;

  const encoded: string[] = [];
  for (const segment of segments) {
    const value = encodeSegment(segment);
    if (value === null || value === '') return null;
    encoded.push(value);
  }

  return `/agent/${encoded.join('/')}`;
};

/**
 * Proxies one agent request to the FastAPI backend.
 *
 * The browser talks to this route same-origin, which is the only reason the SSE
 * stream works: `EventSource` cannot attach a session cookie to a cross-origin
 * request, so a stream opened against the backend host directly would always come
 * back unauthenticated.
 */
const proxyToBackend = async (
  request: Request,
  context: AgentRouteContext
): Promise<Response> => {
  // Re-verified inside the handler on every request. A shared middleware or a
  // single check elsewhere would be one crafted request away from being skipped,
  // and the session is the only thing gating these routes.
  const denied = await unauthorizedIfNoSession(request);
  if (denied) return denied;

  const { path } = await context.params;

  const upstreamPath = buildUpstreamPath(path ?? []);
  if (upstreamPath === null) {
    return NextResponse.json({ error: 'Invalid path' }, { status: 400 });
  }

  // The target host comes from server-side configuration and is never influenced
  // by the request, so this is not an SSRF sink even though the path is.
  const target = `${getBackendUrl()}${upstreamPath}${new URL(request.url).search}`;

  // An explicit allowlist rather than a copy of the inbound headers: forwarding
  // everything would drag hop-by-hop names and client spoofing headers along.
  const headers = new Headers();
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('Cookie', cookie);
  const contentType = request.headers.get('content-type');
  if (contentType) headers.set('Content-Type', contentType);

  const forwardsBody = request.method !== 'GET' && request.method !== 'HEAD';

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      // Buffered rather than piped: the payloads here are small JSON documents,
      // and a piped ReadableStream body needs half-duplex support that is not
      // guaranteed on every Worker runtime.
      body: forwardsBody ? await request.arrayBuffer() : undefined,
      // Bind the upstream call to the client's lifetime so an abandoned run does
      // not keep holding the backend connection open.
      signal: request.signal,
    });
  } catch {
    console.error(
      `[agent-proxy] upstream request failed: ${request.method} ${upstreamPath}`
    );
    return NextResponse.json({ error: 'Upstream backend unavailable' }, { status: 502 });
  }

  const isEventStream =
    (upstream.headers.get('content-type') ?? '').includes('text/event-stream');
  const isStreamPath = path[path.length - 1] === 'stream';

  if (isStreamPath && isEventStream && upstream.body !== null) {
    // The body is handed over as the same ReadableStream. Anything that awaits
    // .text()/.json() here would buffer the whole run before the first event
    // reached the browser.
    //
    // Response headers are built from scratch: `Connection` is hop-by-hop and a
    // forbidden header name in Workers, so `new Response()` throws if it is set.
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-store',
        'X-Accel-Buffering': 'no',
      },
    });
  }

  const passthrough = new Headers({ 'Cache-Control': 'no-store' });
  const upstreamType = upstream.headers.get('content-type');
  if (upstreamType) passthrough.set('Content-Type', upstreamType);

  return new Response(upstream.body, {
    status: upstream.status,
    headers: passthrough,
  });
};

export async function GET(request: Request, context: AgentRouteContext) {
  return proxyToBackend(request, context);
}

export async function POST(request: Request, context: AgentRouteContext) {
  return proxyToBackend(request, context);
}