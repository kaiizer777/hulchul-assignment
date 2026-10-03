/**
 * Deploy-time secret-presence guard for the Cloudflare Worker.
 *
 * `getDb()` (src/lib/db.ts) and `verifySession()` (src/lib/auth.ts) read
 * `process.env.DATABASE_URL` and `process.env.UPSTASH_REDIS_REST_TOKEN`. Both are
 * Wrangler *secrets*, so neither can live in `wrangler.toml` ([vars] is committed
 * to git) — they must be provisioned out of band per Worker:
 *
 *   npx wrangler secret put DATABASE_URL
 *   npx wrangler secret put UPSTASH_REDIS_REST_TOKEN
 *
 * A missing secret is not a build error and not a deploy error, so the omission is
 * only ever discovered at request time: the Worker builds, deploys, and then every
 * `/api/*` route either 500s (`getDb()` throws) or 401s (`verifySession()` returns
 * null, which the routes turn into `unauthorizedIfNoSession`). That is a 401 storm
 * against a healthy-looking deploy, discovered by whoever happens to be using the
 * app first. This guard moves the discovery to the deploy, where it costs nothing
 * but a refused upload.
 *
 * Scope and safety properties, deliberately:
 *
 *   1. NAMES ONLY. `wrangler secret list` returns names and types, never values, and
 *      this script never prints, logs or persists raw wrangler output. Not even on
 *      the failure path, where the output is the one thing most likely to contain
 *      something sensitive.
 *   2. FAIL CLOSED. An absent wrangler, a non-zero exit, or unparseable output all
 *      produce `ok: false` with every secret reported missing. Warn-and-continue
 *      would ship exactly the broken prod this guard exists to prevent, so "I could
 *      not check" is never allowed to read as "it is fine".
 *   3. CREDENTIALS BY ENVIRONMENT ONLY. An optional Cloudflare API token is passed
 *      to the child through `env`, never on a command line where it would show up in
 *      a process listing, and the scratch copy is scrubbed in a `finally` so it does
 *      not outlive the call (same handling as `AUTH_PASSWORD_HASH_CANDIDATE` in
 *      infra/aws/deploy.ps1).
 *
 * This runs from `npm run deploy:cloudflare` and deliberately NOT from CI: it needs
 * a Cloudflare credential and live Worker state, neither of which CI has. Same
 * placement rationale as the `auth_password_hash` guard in infra/aws/deploy.ps1.
 *
 * Usage: node scripts/verify-worker-secrets.mjs
 */

import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const WORKER_NAME = 'hulchul-frontend';

const REQUIRED_SECRETS = ['DATABASE_URL', 'UPSTASH_REDIS_REST_TOKEN'];

const CREDENTIAL_ENV_KEY = 'WORKER_SECRETS_API_TOKEN';

const fail = (message) => {
  console.error(`[verify-worker-secrets] ${message}`);
  process.exit(1);
};

/**
 * Absolute path to wrangler's CLI entry point.
 *
 * Spawned as `node <bin>` rather than through `npx` or a shell: on Windows `npx` is
 * a `.cmd` shim that `execFileSync` cannot launch without `shell: true`, and a shell
 * is not worth it for a deploy gate. `wrangler/package.json` is used as the anchor
 * because wrangler's `exports` map does not expose `bin/wrangler.js` for resolution;
 * the path is then joined off that package directory, so a hoisted install resolves
 * the same way.
 */
const resolveWranglerBin = () => {
  const require = createRequire(import.meta.url);
  return join(dirname(require.resolve('wrangler/package.json')), 'bin', 'wrangler.js');
};

/**
 * Secret names out of `wrangler secret list --format json` output.
 *
 * Anything that is not a JSON array of `{ name: string }` is a hard failure rather
 * than a best-effort read: guessing at the shape would turn a wrangler version bump
 * into a silently passing gate.
 */
const parseSecretNames = (output) => {
  const trimmed = output.trim();
  if (trimmed.length === 0) {
    return { ok: false, reason: 'wrangler returned no output' };
  }

  const start = trimmed.indexOf('[');
  const end = trimmed.lastIndexOf(']');
  if (start === -1 || end < start) {
    return { ok: false, reason: 'wrangler output did not contain a JSON array' };
  }

  let parsed;
  try {
    parsed = JSON.parse(trimmed.slice(start, end + 1));
  } catch {
    return { ok: false, reason: 'wrangler output was not valid JSON' };
  }

  if (!Array.isArray(parsed) || !parsed.every((entry) => entry && typeof entry.name === 'string')) {
    return { ok: false, reason: 'wrangler output did not list secret names' };
  }

  return { ok: true, names: parsed.map((entry) => entry.name) };
};

/**
 * @typedef {object} SecretCheckResult
 * @property {boolean} ok True only when every required secret is present AND the
 *   listing could be read and parsed. Never true on an inconclusive check.
 * @property {string[]} missing Required secrets not seen in the listing. Every
 *   required secret when the listing itself could not be established.
 * @property {string | null} reason Human-readable cause of a failure, or null on
 *   success. Contains no wrangler output and no credential.
 */

/**
 * Check that the Worker's required secrets are provisioned.
 *
 * Spawns wrangler; returns a verdict instead of exiting, so it is directly testable
 * without a live Worker and without the test having to intercept `process.exit`.
 *
 * @returns {SecretCheckResult}
 */
export function checkWorkerSecrets() {
  const credential = process.env[CREDENTIAL_ENV_KEY];
  const childEnv = { ...process.env };

  /** @type {string} */
  let raw = '';

  try {
    if (credential) {
      childEnv.CLOUDFLARE_API_TOKEN = credential;
    }
    raw = execFileSync(
      process.execPath,
      [resolveWranglerBin(), 'secret', 'list', '--name', WORKER_NAME, '--format', 'json'],
      {
        encoding: 'utf8',
        env: childEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );
  } catch (error) {
    const status = typeof error?.status === 'number' ? `exit ${error.status}` : 'a spawn failure';
    return {
      ok: false,
      missing: [...REQUIRED_SECRETS],
      reason:
        `could not list secrets for Worker "${WORKER_NAME}" (wrangler reported ${status}). ` +
        'Refusing to deploy on an unverifiable check: install wrangler and authenticate ' +
        '(`npx wrangler whoami`) or set WORKER_SECRETS_API_TOKEN.',
    };
  } finally {
    delete childEnv.CLOUDFLARE_API_TOKEN;
    delete process.env[CREDENTIAL_ENV_KEY];
  }

  const parsed = parseSecretNames(raw);
  raw = '';

  if (!parsed.ok) {
    return {
      ok: false,
      missing: [...REQUIRED_SECRETS],
      reason: `could not read the secret listing for Worker "${WORKER_NAME}": ${parsed.reason}.`,
    };
  }

  const present = new Set(parsed.names);
  const missing = REQUIRED_SECRETS.filter((name) => !present.has(name));

  if (missing.length > 0) {
    return {
      ok: false,
      missing,
      reason: `Worker "${WORKER_NAME}" has no ${missing.join(' or ')} secret provisioned.`,
    };
  }

  return { ok: true, missing: [], reason: null };
}

const invokedDirectly =
  process.argv[1] !== undefined && pathToFileURL(process.argv[1]).href === import.meta.url;

if (invokedDirectly) {
  const result = checkWorkerSecrets();

  if (!result.ok) {
    fail(
      `${result.reason}\n` +
        `[verify-worker-secrets] Refusing to deploy Worker "${WORKER_NAME}". ` +
        `Missing or unverifiable: ${result.missing.join(', ')}. ` +
        'Provision each one (values are never stored in this repo):\n' +
        REQUIRED_SECRETS.map((name) => `  npx wrangler secret put ${name}`).join('\n')
    );
  }

  console.log(
    `[verify-worker-secrets] OK: ${REQUIRED_SECRETS.join(', ')} are all provisioned on ` +
      `Worker "${WORKER_NAME}".`
  );
}

export { REQUIRED_SECRETS, WORKER_NAME };