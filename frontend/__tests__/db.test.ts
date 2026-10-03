import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

/**
 * `getDb()` is the single place the Neon connection string is read, and it is the
 * read that issue #35 was filed against. There was no coverage here at all, so the
 * two behaviours that matter were both unpinned: that the driver is constructed from
 * `process.env.DATABASE_URL`, and that a missing variable fails loudly instead of
 * handing the driver `undefined`.
 *
 * The driver is mocked, so nothing here connects to Neon.
 */

const neon = vi.fn();

vi.mock('@neondatabase/serverless', () => ({
  neon: (...args: unknown[]) => neon(...args),
}));

const ORIGINAL_DATABASE_URL = process.env.DATABASE_URL;

/**
 * `getDb()` memoises its client in module scope, so each case needs a fresh module
 * instance to observe a fresh first-call behaviour.
 */
const loadDb = async () => import('@/lib/db');

describe('getDb', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    neon.mockReturnValue({ tag: 'neon-client' });
  });

  afterEach(() => {
    if (ORIGINAL_DATABASE_URL === undefined) {
      delete process.env.DATABASE_URL;
    } else {
      process.env.DATABASE_URL = ORIGINAL_DATABASE_URL;
    }
  });

  it('builds the driver from exactly process.env.DATABASE_URL', async () => {
    process.env.DATABASE_URL =
      'postgresql://neonuser:s3cr3t-neon-password@ep-cool-pool.aws.neon.tech/neondb?sslmode=require';
    const { getDb } = await loadDb();

    const client = getDb();

    expect(neon).toHaveBeenCalledTimes(1);
    expect(neon).toHaveBeenCalledWith(process.env.DATABASE_URL);
    expect(client).toEqual({ tag: 'neon-client' });
  });

  it('memoises the driver so repeated calls do not re-read the environment', async () => {
    process.env.DATABASE_URL = 'postgresql://u:p@ep-x.aws.neon.tech/neondb';
    const { getDb } = await loadDb();

    const first = getDb();
    const second = getDb();

    expect(second).toBe(first);
    expect(neon).toHaveBeenCalledTimes(1);
  });

  it('throws the documented error when DATABASE_URL is absent', async () => {
    delete process.env.DATABASE_URL;
    const { getDb } = await loadDb();

    expect(() => getDb()).toThrow('DATABASE_URL environment variable is missing.');
    expect(neon).not.toHaveBeenCalled();
  });

  it('throws rather than constructing a driver from an empty DATABASE_URL', async () => {
    // An empty binding is still truthy-checked the same way as a missing one; this
    // pins that the guard is a presence check and not a truthiness check that a
    // blank string could slip through some other path.
    process.env.DATABASE_URL = '';
    const { getDb } = await loadDb();

    expect(() => getDb()).toThrow('DATABASE_URL environment variable is missing.');
    expect(neon).not.toHaveBeenCalled();
  });
});