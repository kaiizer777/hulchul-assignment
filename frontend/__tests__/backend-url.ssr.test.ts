// @vitest-environment node
import { describe, it, expect, afterEach, vi } from 'vitest';

import { getBackendUrl } from '@/lib/backend-url';

/**
 * `getBackendUrl` runs during server rendering too, where there is no `window`.
 * This file uses the node environment so the `typeof window === 'undefined'`
 * branch is exercised for real rather than simulated; a jsdom environment
 * always defines `window`.
 */

const ENV_KEY = 'NEXT_PUBLIC_BACKEND_URL';
const PRODUCTION_BACKEND_URL = 'https://gmruxxvvxbypxv4d74kix7l6aq0ppwmd.lambda-url.us-east-1.on.aws';
const LOCAL_BACKEND_URL = 'http://localhost:8051';
const originalEnv = process.env[ENV_KEY];

afterEach(() => {
  if (originalEnv === undefined) {
    delete process.env[ENV_KEY];
  } else {
    process.env[ENV_KEY] = originalEnv;
  }
  vi.unstubAllEnvs();
});

describe('getBackendUrl during server rendering', () => {
  it('has no window in this environment', () => {
    expect(typeof window).toBe('undefined');
  });

  it('uses the production backend when unconfigured in production', () => {
    delete process.env[ENV_KEY];
    vi.stubEnv('NODE_ENV', 'production');
    expect(getBackendUrl()).toBe(PRODUCTION_BACKEND_URL);
  });

  it('uses localhost for unconfigured server-side calls outside production', () => {
    delete process.env[ENV_KEY];
    vi.stubEnv('NODE_ENV', 'development');
    expect(getBackendUrl()).toBe(LOCAL_BACKEND_URL);
  });

  it('still prefers the build-time value', () => {
    process.env[ENV_KEY] = 'https://backend.example.com/';
    expect(getBackendUrl()).toBe('https://backend.example.com');
  });
});