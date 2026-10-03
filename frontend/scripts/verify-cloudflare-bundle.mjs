/**
 * Post-build guard for the Cloudflare Worker bundle.
 *
 * Before the same-origin `/api/agent` proxy, the client talked to the FastAPI
 * backend directly, so `NEXT_PUBLIC_BACKEND_URL` had to be inlined into the
 * client bundle at build time: a bundle built without it silently fell back to
 * http://localhost:8051 and every browser request failed with an opaque
 * "Failed to fetch" pointing at the visitor's own machine.
 *
 * That direct-talk topology is gone. The browser now only talks same-origin to
 * `/api/agent` (see `AGENT_API_BASE` in `src/app/agent/page.tsx`) and
 * `NEXT_PUBLIC_BACKEND_URL` is read server-side by `getBackendUrl`
 * (`src/lib/backend-url.ts`) for the proxy.
 *
 * Agent traffic goes through this origin's own
 * `/api/agent` proxy, which exists because `EventSource` cannot attach a session
 * cookie to a cross-origin request. Two invariants are therefore asserted on the
 * built artifact:
 *
 *   1. The client bundle contains the same-origin agent API base. Without it the
 *      deployed /agent page cannot reach the proxy at all.
 *   2. The client bundle does NOT contain the backend host. If it does, the browser
 *      is once more talking to the backend cross-origin, which silently breaks both
 *      the session cookie on the stream and the whole point of the proxy.
 *
 * Invariant 2 is checked against every host the backend could have been reached
 * through: the configured `NEXT_PUBLIC_BACKEND_URL` and the production fallback
 * compiled into src/lib/backend-url.ts.
 *
 * Usage: node scripts/verify-cloudflare-bundle.mjs
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CHUNKS_DIR = join(FRONTEND_DIR, '.open-next', 'assets');
const PROD_ENV_FILE = join(FRONTEND_DIR, '.env.production');
const BACKEND_URL_MODULE = join(FRONTEND_DIR, 'src', 'lib', 'backend-url.ts');

const CLIENT_AGENT_API_BASE = '/api/agent';

const fail = (message) => {
  console.error(`[verify-cloudflare-bundle] ${message}`);
  process.exit(1);
};

const readEnvFileValue = (key) => {
  if (!existsSync(PROD_ENV_FILE)) return undefined;
  for (const line of readFileSync(PROD_ENV_FILE, 'utf8').split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (match && match[1] === key) {
      return match[2].replace(/^["']|["']$/g, '');
    }
  }
  return undefined;
};

const hostOf = (url) => {
  try {
    return new URL(url).host;
  } catch {
    return undefined;
  }
};

/**
 * Every backend host the client bundle must stay free of.
 *
 * The configured value is optional now that only the server-side proxy needs it,
 * but the production fallback in backend-url.ts is not: it is a hardcoded
 * constant, so a stray import of that module from a client component would ship
 * the backend host to the browser even with no environment variable in play.
 */
const forbiddenHosts = new Set();

const configuredUrl =
  process.env.NEXT_PUBLIC_BACKEND_URL || readEnvFileValue('NEXT_PUBLIC_BACKEND_URL');
const configuredHost = hostOf(configuredUrl ?? '');
if (configuredHost) forbiddenHosts.add(configuredHost);

if (existsSync(BACKEND_URL_MODULE)) {
  const source = readFileSync(BACKEND_URL_MODULE, 'utf8');
  const fallback = source.match(/PRODUCTION_BACKEND_URL\s*=\s*'([^']+)'/);
  const fallbackHost = fallback ? hostOf(fallback[1]) : undefined;
  if (fallbackHost) forbiddenHosts.add(fallbackHost);
}

if (!existsSync(CHUNKS_DIR)) {
  fail(`Built bundle not found at ${CHUNKS_DIR}. Run "npm run build:cloudflare" first.`);
}

const collectJsFiles = (dir) => {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...collectJsFiles(full));
    else if (entry.name.endsWith('.js')) found.push(full);
  }
  return found;
};

const chunks = collectJsFiles(CHUNKS_DIR);
if (chunks.length === 0) {
  fail(`No JavaScript chunks found under ${CHUNKS_DIR}.`);
}

const chunkNames = [...new Set(chunks.map((f) => f.slice(CHUNKS_DIR.length + 1)))];
const sources = chunks.map((file) => readFileSync(file, 'utf8'));

const leaking = chunks
  .map((file, index) => ({
    file: file.slice(CHUNKS_DIR.length + 1),
    hosts: [...forbiddenHosts].filter((host) => sources[index].includes(host)),
  }))
  .filter((entry) => entry.hosts.length > 0);

if (leaking.length > 0) {
  fail(
    `The backend host still appears in ${leaking.length} client chunk(s): ` +
      `${leaking.map((e) => `${e.file} [${e.hosts.join(', ')}]`).join(', ')}. ` +
      'The deployed /agent page would talk to the backend cross-origin, where ' +
      'EventSource cannot send the session cookie. Repoint it at ' +
      `"${CLIENT_AGENT_API_BASE}".`
  );
}

const proxyCallSites = sources.filter((source) => source.includes(CLIENT_AGENT_API_BASE));

if (proxyCallSites.length === 0) {
  fail(
    `The same-origin agent API base "${CLIENT_AGENT_API_BASE}" does not appear in any ` +
      'built client chunk. The deployed /agent page would have no reachable proxy for ' +
      'agent runs, SSE or approval decisions.'
  );
}

console.log(
  `[verify-cloudflare-bundle] OK: "${CLIENT_AGENT_API_BASE}" inlined in ${proxyCallSites.length} ` +
    `client chunk(s) and no backend host (${[...forbiddenHosts].join(', ') || 'none known'}) ` +
    `leaks into any of the ${chunkNames.length} chunk file(s) scanned.`
);