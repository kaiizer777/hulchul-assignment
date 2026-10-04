import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import React from 'react';

import { getBackendUrl } from '@/lib/backend-url';
import AgentControlPage from '@/app/agent/page';

const PRODUCTION_BACKEND_URL = 'https://gmruxxvvxbypxv4d74kix7l6aq0ppwmd.lambda-url.us-east-1.on.aws';

const ENV_KEY = 'NEXT_PUBLIC_BACKEND_URL';
const originalEnv = process.env[ENV_KEY];

const setBackendUrlEnv = (value: string | undefined) => {
  if (value === undefined) {
    delete process.env[ENV_KEY];
  } else {
    process.env[ENV_KEY] = value;
  }
};

/** jsdom's window.location is a getter, so swap the whole object. */
const setHostname = (hostname: string) => {
  Object.defineProperty(window, 'location', {
    value: { hostname },
    writable: true,
    configurable: true,
  });
};

afterEach(() => {
  setBackendUrlEnv(originalEnv);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('getBackendUrl', () => {
  afterEach(() => {
    setHostname('localhost');
  });

  it('returns the build-time value verbatim when the environment provides one', () => {
    setBackendUrlEnv('https://backend.example.com');
    setHostname('localhost');
    expect(getBackendUrl()).toBe('https://backend.example.com');
  });

  it('strips a single trailing slash so callers can append paths safely', () => {
    setBackendUrlEnv('https://backend.example.com/');
    setHostname('localhost');
    expect(getBackendUrl()).toBe('https://backend.example.com');
  });

  it('prefers the build-time value over the hostname based default', () => {
    setBackendUrlEnv('https://backend.example.com');
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    expect(getBackendUrl()).toBe('https://backend.example.com');
  });

  it('uses the local backend when served from localhost without configuration', () => {
    setBackendUrlEnv(undefined);
    setHostname('localhost');
    expect(getBackendUrl()).toBe('http://localhost:8051');
  });

  it('uses the local backend when served from 127.0.0.1 without configuration', () => {
    setBackendUrlEnv(undefined);
    setHostname('127.0.0.1');
    expect(getBackendUrl()).toBe('http://localhost:8051');
  });

  it('never degrades to localhost on a deployed host without configuration in production', () => {
    // This is the regression that produced an opaque "Failed to fetch": a bundle
    // built without the variable pointed the browser at the visitor's machine.
    // NODE_ENV is inlined at build time, so stub it to simulate a prod bundle.
    setBackendUrlEnv(undefined);
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    vi.stubEnv('NODE_ENV', 'production');
    expect(getBackendUrl()).toBe(PRODUCTION_BACKEND_URL);
    expect(getBackendUrl()).not.toContain('localhost');
  });

  it('resolves localhost on a deployed hostname outside production so dev never touches prod', () => {
    setBackendUrlEnv(undefined);
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    vi.stubEnv('NODE_ENV', 'development');
    expect(getBackendUrl()).toBe('http://localhost:8051');
  });
});

describe('/agent network failure reporting', () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  const renderPage = async () => {
    let result: ReturnType<typeof render> | undefined;
    await act(async () => {
      result = render(<AgentControlPage />);
    });
    return result;
  };

  it('reports the same-origin proxy when the request never reaches the server', async () => {
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    // A rejection from fetch() is what a connection refusal, DNS failure or a dead
    // Worker produces: no response was ever delivered to the browser.
    global.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(
        screen.getByText(/cannot read a response from the agent proxy at \/api\/agent/i)
      ).toBeDefined();
    });

    // The bare browser message is not what the operator should be left with.
    expect(screen.queryByText(/^Failed to fetch$/)).toBeNull();
    expect(screen.getByText(/0 steps logged/i)).toBeDefined();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('never contacts the backend host directly, whatever the environment says', async () => {
    // EventSource cannot attach the session cookie to a cross-origin request, so a
    // bundle still inlining the Lambda URL would silently come back unauthenticated
    // on the stream.
    setBackendUrlEnv(PRODUCTION_BACKEND_URL);
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: 'run-12345678', status: 'running' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    global.fetch = fetchMock;

    const eventSourceUrls: string[] = [];
    class FakeEventSource {
      onmessage: ((e: MessageEvent) => void) | null = null;
      onerror: ((e: Event) => void) | null = null;
      constructor(public url: string) {
        eventSourceUrls.push(url);
      }
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal('EventSource', FakeEventSource);

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/Run: run-1234/i)).toBeDefined();
    });

    const calledUrls = fetchMock.mock.calls.map((call) => call[0] as string);
    expect(calledUrls).toContain('/api/agent/run');
    expect(calledUrls.every((url) => url.startsWith('/'))).toBe(true);
    expect(calledUrls.join(' ')).not.toContain(PRODUCTION_BACKEND_URL);
    expect(eventSourceUrls.join(' ')).not.toContain(PRODUCTION_BACKEND_URL);
  });

  it('does not blame reachability when the response arrived but its body failed with a TypeError', async () => {
    // Regression guard: a body stream that dies mid-transfer rejects as a
    // TypeError too, exactly like a connection failure. Classifying by the
    // thrown class alone reported "no agent run was started" for a run that the
    // backend had in fact already executed.
    const res = new Response(JSON.stringify({ run_id: 'run-12345678' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
    vi.spyOn(res, 'json').mockRejectedValue(new TypeError('network error'));
    global.fetch = vi.fn().mockResolvedValue(res);

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/could not be read as JSON/i)).toBeDefined();
    });
    expect(screen.queryByText(/cannot read a response from the agent proxy at/i)).toBeNull();
  });

  it('does not blame reachability when the response body is not JSON', async () => {
    // An edge proxy answering 200 with an HTML body is a response fault, not an
    // unreachable backend.
    global.fetch = vi.fn().mockResolvedValue(
      new Response('<html><body>Bad Gateway</body></html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      })
    );

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/could not be read as JSON/i)).toBeDefined();
    });
    expect(screen.queryByText(/cannot read a response from the agent proxy at/i)).toBeNull();
  });

  it('surfaces the server detail for an HTTP error response instead of the unreachable message', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Database pool exhausted' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(screen.getByText('Database pool exhausted')).toBeDefined();
    });
    expect(screen.queryByText(/cannot read a response from the agent proxy at/i)).toBeNull();
  });

  it('starts a run and opens the step stream through the proxy', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: 'run-12345678', status: 'running' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    global.fetch = fetchMock;

    const eventSourceUrls: string[] = [];
    class FakeEventSource {
      onmessage: ((e: MessageEvent) => void) | null = null;
      onerror: ((e: Event) => void) | null = null;
      constructor(public url: string) {
        eventSourceUrls.push(url);
      }
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal('EventSource', FakeEventSource);

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/run-1234/i)).toBeDefined();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/agent/run',
      expect.objectContaining({ method: 'POST', credentials: 'include' })
    );
    // A run id means the SSE stream is opened, and same-origin is what lets the
    // session cookie ride along with it.
    expect(eventSourceUrls).toContain('/api/agent/runs/run-12345678/stream');
    expect(screen.queryByText(/cannot read a response from the agent proxy at/i)).toBeNull();
  });
});