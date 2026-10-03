import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import React from 'react';

import AgentControlPage from '@/app/agent/page';

type FrameListener = (event: MessageEvent) => void;

const openedStreamUrls: string[] = [];
const frameListeners = new Map<string, FrameListener>();

class FakeEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string) {
    openedStreamUrls.push(url);
  }
  addEventListener(type: string, listener: FrameListener) {
    frameListeners.set(type, listener);
  }
  close() {}
}

const jsonResponse = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });

describe('/agent SSE frame handling', () => {
  let consoleWarnSpy: ReturnType<typeof vi.spyOn>;
  let approvalPollShouldFail: 'none' | 'network' | 'abort';

  beforeEach(() => {
    consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    openedStreamUrls.length = 0;
    frameListeners.clear();
    approvalPollShouldFail = 'none';
    // Only the interval is faked so the approval poll can be driven explicitly
    // while waitFor keeps using real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.stubGlobal('EventSource', FakeEventSource);

    global.fetch = vi.fn().mockImplementation((url: string | URL | Request) => {
      const urlStr = typeof url === 'string' ? url : url.toString();
      if (urlStr.endsWith('/run')) {
        return Promise.resolve(jsonResponse({ run_id: 'run-12345678', status: 'running' }));
      }
      if (approvalPollShouldFail === 'network') {
        return Promise.reject(new TypeError('Failed to fetch'));
      }
      if (approvalPollShouldFail === 'abort') {
        const abortError = new Error('The operation was aborted.');
        abortError.name = 'AbortError';
        return Promise.reject(abortError);
      }
      return Promise.resolve(jsonResponse({ pending: false }));
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const startRun = async () => {
    await act(async () => {
      render(<AgentControlPage />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run agent/i }));
    });
    await waitFor(() => {
      expect(openedStreamUrls).toContain('/api/agent/runs/run-12345678/stream');
    });
  };

  const emitFrame = async (type: string, data: string) => {
    const listener = frameListeners.get(type);
    if (!listener) throw new Error(`no ${type} listener was registered on the stream`);
    await act(async () => {
      listener(new MessageEvent(type, { data }));
    });
  };

  it('applies a well-formed status_change frame so the malformed case is provably reachable', async () => {
    await startRun();

    await emitFrame('status_change', JSON.stringify({ type: 'status_change', status: 'paused' }));

    expect(screen.getByText('paused')).toBeDefined();
  });

  it('warns and keeps the previous status when a status_change frame is not JSON', async () => {
    await startRun();

    await emitFrame('status_change', '{ "type": "status_change", ');

    expect(consoleWarnSpy).toHaveBeenCalledWith(
      'Failed to parse status_change SSE frame, ignoring',
      expect.any(SyntaxError)
    );
    expect(screen.getByText('running')).toBeDefined();
  });

  it('warns and logs no step when a step_complete frame is not JSON', async () => {
    await startRun();

    await emitFrame('step_complete', 'not json at all');

    expect(consoleWarnSpy).toHaveBeenCalledWith(
      'Failed to parse step_complete SSE frame, ignoring',
      expect.any(SyntaxError)
    );
    expect(screen.getByText(/0 steps logged/i)).toBeDefined();
  });

  it('warns and logs no step when a step_failed frame is not JSON', async () => {
    await startRun();

    await emitFrame('step_failed', '{"type":"step_failed",');

    expect(consoleWarnSpy).toHaveBeenCalledWith(
      'Failed to parse step_failed SSE frame, ignoring',
      expect.any(SyntaxError)
    );
    expect(screen.getByText(/0 steps logged/i)).toBeDefined();
  });

  it('warns and never opens the approval gate when a needs_approval frame is not JSON', async () => {
    await startRun();

    await emitFrame('needs_approval', '{"nonce":');

    expect(consoleWarnSpy).toHaveBeenCalledWith(
      'Failed to parse needs_approval SSE frame, ignoring',
      expect.any(SyntaxError)
    );
    // A frame the page cannot read must not block the run behind a gate it
    // cannot describe, so the modal stays closed.
    expect(screen.queryByRole('heading', { name: /human approval required/i })).toBeNull();
    expect(screen.getByText('running')).toBeDefined();
  });

  it('warns when the approval status poll fails instead of dropping the error', async () => {
    await startRun();
    approvalPollShouldFail = 'network';

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });

    expect(consoleWarnSpy).toHaveBeenCalledWith('Approval status poll failed', expect.any(TypeError));
  });

  it('stays silent when the approval status poll is aborted by teardown', async () => {
    await startRun();
    approvalPollShouldFail = 'abort';

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });

    expect(consoleWarnSpy).not.toHaveBeenCalledWith('Approval status poll failed', expect.anything());
  });
});