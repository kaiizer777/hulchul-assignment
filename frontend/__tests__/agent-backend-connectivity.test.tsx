import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import React from 'react';

import { getBackendUrl, unreachableBackendMessage } from '@/lib/backend-url';
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

  it('never degrades to localhost on a deployed host without configuration', () => {
    // This is the regression that produced an opaque "Failed to fetch": a bundle
    // built without the variable pointed the browser at the visitor's machine.
    setBackendUrlEnv(undefined);
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    expect(getBackendUrl()).toBe(PRODUCTION_BACKEND_URL);
    expect(getBackendUrl()).not.toContain('localhost');
  });
});

describe('unreachableBackendMessage', () => {
  it('names the backend that could not be reached', () => {
    const message = unreachableBackendMessage(PRODUCTION_BACKEND_URL);
    expect(message).toContain(PRODUCTION_BACKEND_URL);
    expect(message).toMatch(/cannot reach/i);
  });

  it('strips credentials embedded in the URL', () => {
    const message = unreachableBackendMessage('https://user:hunter2@backend.example.com');
    expect(message).toContain('backend.example.com');
    expect(message).not.toContain('hunter2');
    expect(message).not.toContain('user');
  });

  it('falls back to the raw value for an unparseable URL', () => {
    const message = unreachableBackendMessage('not a url');
    expect(message).toContain('not a url');
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

  it('reports the attempted backend URL when the request never reaches the server', async () => {
    setBackendUrlEnv('https://backend.example.com');
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    // A TypeError is what fetch() rejects with for a connection refusal, DNS
    // failure or CORS rejection: no response was ever produced.
    global.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/cannot reach the backend at https:\/\/backend\.example\.com/i)).toBeDefined();
    });

    // The bare browser message is not what the operator should be left with.
    expect(screen.queryByText(/^Failed to fetch$/)).toBeNull();
    expect(screen.getByText(/0 steps logged/i)).toBeDefined();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('reports the build-time default when no backend URL is configured', async () => {
    setBackendUrlEnv(undefined);
    setHostname('hulchul-frontend.sufiyanx.workers.dev');
    global.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });

    await waitFor(() => {
      expect(
        screen.getByText(new RegExp(`cannot reach the backend at ${PRODUCTION_BACKEND_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'i'))
      ).toBeDefined();
    });
  });

  it('surfaces the server detail for an HTTP error response instead of the unreachable message', async () => {
    setBackendUrlEnv('https://backend.example.com');
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
    expect(screen.queryByText(/cannot reach the backend/i)).toBeNull();
  });

  it('starts a run and opens the step stream when the backend responds', async () => {
    setBackendUrlEnv('https://backend.example.com');
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

    expect(fetchMock).toHaveBeenCalledWith(
      'https://backend.example.com/agent/run',
      expect.objectContaining({ method: 'POST' })
    );
    // A run id means the SSE stream is opened, which is what never happened when
    // the bundle pointed the browser at localhost.
    expect(eventSourceUrls).toContain('https://backend.example.com/agent/runs/run-12345678/stream');
    expect(screen.queryByText(/cannot reach the backend/i)).toBeNull();
  });
});