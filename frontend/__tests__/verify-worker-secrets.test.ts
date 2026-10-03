import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

/**
 * The guard is exercised end to end at the module boundary, never against a live
 * Worker: `node:child_process` and `node:module` are both mocked, so no wrangler is
 * spawned, no credential is used and no network call happens anywhere in this file.
 *
 * Both factories return a `default` alongside the named exports. Vitest's CJS interop
 * reads builtin imports through the namespace's `default`, so a mock that only
 * overrides the named export leaves the real module reachable and the test silently
 * spawns the real wrangler instead.
 */

const execFileSync = vi.fn();

let spawnedEnv: Record<string, string> | undefined;

const spawnStub = (...args: unknown[]) => {
  // Snapshotted at spawn time: the guard scrubs its child-env copy once the
  // synchronous child has exited, so the live object no longer shows what the child
  // was actually handed.
  const options = args[2] as { env?: Record<string, string> } | undefined;
  spawnedEnv = { ...options?.env };
  return execFileSync(...args);
};

vi.mock('node:child_process', () => ({
  execFileSync: spawnStub,
  default: { execFileSync: spawnStub },
}));

const resolvePackageJson = vi.fn();

const resolveStub = (...args: unknown[]) => resolvePackageJson(...args);

vi.mock('node:module', () => ({
  createRequire: () => ({ resolve: resolveStub }),
  default: { createRequire: () => ({ resolve: resolveStub }) },
}));

const { checkWorkerSecrets, REQUIRED_SECRETS, WORKER_NAME } = await import(
  '../scripts/verify-worker-secrets.mjs'
);

const DATABASE_URL_VALUE =
  'postgresql://neonuser:s3cr3t-neon-password@ep-cool-pool.aws.neon.tech/neondb?sslmode=require';
const UPSTASH_TOKEN_VALUE = 'AXl4ZmFrZXRva2VudmFsdWV2YWx1ZXZhbHVlMTIzNDU2Nzg5';
const CREDENTIAL_VALUE = 'cf-api-token-that-must-never-be-echoed';

const listing = (...names: string[]) =>
  JSON.stringify(names.map((name) => ({ name, type: 'secret_text' })), null, 2);

/** The argv the guard passes to the wrangler CLI, as one flat string array. */
const spawnArgv = (): string[] => {
  const [, argv] = execFileSync.mock.calls[0];
  return argv as string[];
};

/** The environment the child was handed at spawn time. */
const spawnEnv = (): Record<string, string> | undefined => spawnedEnv;

describe('checkWorkerSecrets', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    spawnedEnv = undefined;
    resolvePackageJson.mockReturnValue('/repo/frontend/node_modules/wrangler/package.json');
    // Default to output that cannot be parsed, so a test that forgets to arrange the
    // listing fails closed rather than passing on an accidental match.
    execFileSync.mockReturnValue('[]');
    delete process.env.WORKER_SECRETS_API_TOKEN;
  });

  afterEach(() => {
    delete process.env.WORKER_SECRETS_API_TOKEN;
  });

  it('passes when both required secrets are provisioned', () => {
    execFileSync.mockReturnValue(listing('DATABASE_URL', 'UPSTASH_REDIS_REST_TOKEN'));

    const result = checkWorkerSecrets();

    expect(result.ok).toBe(true);
    expect(result.missing).toEqual([]);
    expect(result.reason).toBeNull();
  });

  it('lists the Worker secrets by name only, via wrangler secret list', () => {
    execFileSync.mockReturnValue(listing('DATABASE_URL', 'UPSTASH_REDIS_REST_TOKEN'));

    checkWorkerSecrets();

    const argv = spawnArgv();
    expect(argv).toContain('secret');
    expect(argv).toContain('list');
    expect(argv).toContain('--name');
    expect(argv).toContain(WORKER_NAME);
    expect(argv).toContain('--format');
    expect(argv).toContain('json');
  });

  it("fails with exactly ['UPSTASH_REDIS_REST_TOKEN'] missing when only DATABASE_URL is provisioned", () => {
    // Pinned on purpose: this is the Worker's real state today, and the guard exists
    // precisely to stop that state from reaching a deploy unnoticed.
    execFileSync.mockReturnValue(listing('DATABASE_URL'));

    const result = checkWorkerSecrets();

    expect(result.ok).toBe(false);
    expect(result.missing).toEqual(['UPSTASH_REDIS_REST_TOKEN']);
    expect(result.missing).not.toContain('DATABASE_URL');
  });

  it('fails with both secrets missing when the Worker has none', () => {
    execFileSync.mockReturnValue('[]');

    const result = checkWorkerSecrets();

    expect(result.ok).toBe(false);
    expect(result.missing).toEqual([...REQUIRED_SECRETS]);
  });

  it('ignores unrelated secrets and still passes', () => {
    execFileSync.mockReturnValue(
      listing('SOMETHING_ELSE', 'DATABASE_URL', 'UPSTASH_REDIS_REST_TOKEN')
    );

    const result = checkWorkerSecrets();

    expect(result.ok).toBe(true);
    expect(result.missing).toEqual([]);
  });

  describe('fails closed when the listing cannot be established', () => {
    it('fails when wrangler is not installed', () => {
      resolvePackageJson.mockImplementation(() => {
        throw Object.assign(new Error("Cannot find module 'wrangler/package.json'"), {
          code: 'MODULE_NOT_FOUND',
        });
      });

      const result = checkWorkerSecrets();

      expect(result.ok).toBe(false);
      expect(result.missing).toEqual([...REQUIRED_SECRETS]);
      expect(result.reason).toMatch(/wrangler/i);
      expect(execFileSync).not.toHaveBeenCalled();
    });

    it('fails when wrangler exits non-zero', () => {
      execFileSync.mockImplementation(() => {
        throw Object.assign(new Error('Command failed'), { status: 1 });
      });

      const result = checkWorkerSecrets();

      expect(result.ok).toBe(false);
      expect(result.missing).toEqual([...REQUIRED_SECRETS]);
      expect(result.reason).toMatch(/exit 1/);
    });

    it('fails when wrangler cannot be spawned at all', () => {
      execFileSync.mockImplementation(() => {
        throw Object.assign(new Error('spawn ENOENT'), { code: 'ENOENT' });
      });

      const result = checkWorkerSecrets();

      expect(result.ok).toBe(false);
      expect(result.missing).toEqual([...REQUIRED_SECRETS]);
    });

    it.each([
      ['empty output', ''],
      ['non-JSON output', 'Error: not authenticated'],
      ['truncated JSON', '[{"name": "DATABASE_URL"'],
      ['a JSON object instead of an array', '{"name": "DATABASE_URL"}'],
      ['entries that are not secret names', '[{"type": "secret_text"}]'],
    ])('fails, and never passes, on %s', (_label, output) => {
      execFileSync.mockReturnValue(output);

      const result = checkWorkerSecrets();

      expect(result.ok).toBe(false);
      expect(result.missing).toEqual([...REQUIRED_SECRETS]);
      expect(result.reason).toBeTruthy();
    });
  });

  describe('credential handling', () => {
    it('passes an API token through the environment and never on the command line', () => {
      process.env.WORKER_SECRETS_API_TOKEN = CREDENTIAL_VALUE;
      execFileSync.mockReturnValue(listing('DATABASE_URL', 'UPSTASH_REDIS_REST_TOKEN'));

      checkWorkerSecrets();

      expect(spawnEnv()?.CLOUDFLARE_API_TOKEN).toBe(CREDENTIAL_VALUE);
      expect(JSON.stringify(spawnArgv())).not.toContain(CREDENTIAL_VALUE);
    });

    it('scrubs the credential once the check is done', () => {
      process.env.WORKER_SECRETS_API_TOKEN = CREDENTIAL_VALUE;
      execFileSync.mockReturnValue(listing('DATABASE_URL', 'UPSTASH_REDIS_REST_TOKEN'));

      checkWorkerSecrets();

      expect(process.env.WORKER_SECRETS_API_TOKEN).toBeUndefined();
    });

    it('scrubs the credential even when the listing fails', () => {
      process.env.WORKER_SECRETS_API_TOKEN = CREDENTIAL_VALUE;
      execFileSync.mockImplementation(() => {
        throw Object.assign(new Error('Command failed'), { status: 1 });
      });

      checkWorkerSecrets();

      expect(process.env.WORKER_SECRETS_API_TOKEN).toBeUndefined();
    });
  });

  describe('no secret value can escape through the result', () => {
    it('does not leak a value that wrangler returned alongside the names', () => {
      execFileSync.mockReturnValue(
        JSON.stringify([
          { name: 'DATABASE_URL', type: 'secret_text', value: DATABASE_URL_VALUE },
          { name: 'UPSTASH_REDIS_REST_TOKEN', type: 'secret_text', value: UPSTASH_TOKEN_VALUE },
        ])
      );

      const result = checkWorkerSecrets();
      const serialized = JSON.stringify(result);

      expect(result.ok).toBe(true);
      expect(serialized).not.toContain('postgresql://');
      expect(serialized).not.toContain(UPSTASH_TOKEN_VALUE);
      expect(serialized).not.toContain(DATABASE_URL_VALUE);
    });

    it('does not leak a value through the failure reason', () => {
      execFileSync.mockReturnValue(
        `${DATABASE_URL_VALUE} ${UPSTASH_TOKEN_VALUE} is not JSON at all`
      );

      const result = checkWorkerSecrets();
      const serialized = JSON.stringify(result);

      expect(result.ok).toBe(false);
      expect(serialized).not.toContain('postgresql://');
      expect(serialized).not.toContain(UPSTASH_TOKEN_VALUE);
    });

    it('does not leak the credential through the result', () => {
      process.env.WORKER_SECRETS_API_TOKEN = CREDENTIAL_VALUE;
      execFileSync.mockImplementation(() => {
        throw Object.assign(new Error('Command failed'), { status: 1 });
      });

      const result = checkWorkerSecrets();

      expect(JSON.stringify(result)).not.toContain(CREDENTIAL_VALUE);
    });
  });
});