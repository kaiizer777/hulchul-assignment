/**
 * Post-build guard for the Cloudflare Worker bundle.
 *
 * `NEXT_PUBLIC_*` values are inlined into the client bundle by Next.js at build
 * time. A value of the same name in wrangler.toml `[vars]` is a runtime Worker
 * binding and is invisible to the browser, so a bundle built without the
 * variable silently falls back to http://localhost:8051 and every browser
 * request fails with an opaque "Failed to fetch" pointing at the visitor's own
 * machine.
 *
 * This script asserts the invariant on the built artifact rather than on the
 * build inputs, which is the only check that catches the regression.
 *
 * Usage: node scripts/verify-cloudflare-bundle.mjs
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CHUNKS_DIR = join(FRONTEND_DIR, '.open-next', 'assets');
const PROD_ENV_FILE = join(FRONTEND_DIR, '.env.production');

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

const expectedUrl =
  process.env.NEXT_PUBLIC_BACKEND_URL || readEnvFileValue('NEXT_PUBLIC_BACKEND_URL');

if (!expectedUrl) {
  fail(
    'NEXT_PUBLIC_BACKEND_URL is not set and could not be read from frontend/.env.production. ' +
      'Production builds must inline a real backend URL into the client bundle.'
  );
}

let expectedHost;
try {
  expectedHost = new URL(expectedUrl).host;
} catch {
  fail(`NEXT_PUBLIC_BACKEND_URL is not a valid absolute URL: ${expectedUrl}`);
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

const matching = chunks.filter((file) => readFileSync(file, 'utf8').includes(expectedHost));

if (matching.length === 0) {
  fail(
    `The host "${expectedHost}" does not appear in any built client chunk. ` +
      'NEXT_PUBLIC_BACKEND_URL was not inlined at build time, so the deployed /agent page ' +
      'would fall back to http://localhost:8051 in the browser.'
  );
}

console.log(
  `[verify-cloudflare-bundle] OK: "${expectedHost}" is inlined in ${matching.length} client chunk(s) ` +
    `(${[...new Set(chunks.map((f) => f.slice(CHUNKS_DIR.length + 1)))].length} chunk file(s) scanned).`
);