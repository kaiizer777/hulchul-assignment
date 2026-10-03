const PRODUCTION_BACKEND_URL =
  'https://gmruxxvvxbypxv4d74kix7l6aq0ppwmd.lambda-url.us-east-1.on.aws';
const LOCAL_BACKEND_URL = 'http://localhost:8051';
const LOCAL_HOSTNAMES = ['localhost', '127.0.0.1'];

/**
 * Server-only backend base URL resolver for the same-origin proxy.
 *
 * Only imported by server routes (`src/app/api/agent/[...path]/route.ts`,
 * `src/app/api/auth/login/route.ts`, `src/app/api/auth/logout/route.ts`).
 * Never import this module from a client component: the browser talks to the
 * same-origin `/api/agent` proxy (see `AGENT_API_BASE` in
 * `src/app/agent/page.tsx`), never to the backend host directly, because
 * `EventSource` cannot attach the session cookie cross-origin.
 *
 * Resolution order (unchanged; do not reorder):
 *   1. `NEXT_PUBLIC_BACKEND_URL`, read server-side at runtime. The name is
 *      intentional and must not be renamed: live server routes and the deploy
 *      read this exact variable. Note the `NEXT_PUBLIC_` prefix still inlines
 *      into any client bundle that imports this module, so the client import
 *      ban above is load-bearing — `scripts/verify-cloudflare-bundle.mjs`
 *      fails the build if the backend host leaks into client chunks.
 *   2. `http://localhost:8051`, but only when `window` exists and the page is
 *      actually served from a local development host (`localhost`/`127.0.0.1`).
 *      Dormant on the server (no `window`) and in the proxy routes, retained
 *      for local browser contexts and covered by jsdom tests — do not delete.
 *   3. `http://localhost:8051` for server-side calls outside production
 *      (`NODE_ENV !== 'production'`), so a local dev login never POSTs a real
 *      password to the production backend.
 *   4. The production Lambda Function URL, as an explicit default. The
 *      `PRODUCTION_BACKEND_URL` const name is coupled to the literal regex in
 *      `scripts/verify-cloudflare-bundle.mjs` — renaming it silently disables
 *      the leak scan, so do not rename.
 *
 * The last tier is deliberately not `localhost`. A browser that resolves to
 * `http://localhost:8051` sends the request to the visitor's own machine, which
 * always fails with an opaque "Failed to fetch" and looks like an application
 * bug. Preferring the production default means a bundle built without the
 * variable still reaches the real backend.
 */
export const getBackendUrl = (): string => {
  const configured = process.env.NEXT_PUBLIC_BACKEND_URL;
  if (configured) {
    return configured.replace(/\/$/, '');
  }
  if (typeof window !== 'undefined' && LOCAL_HOSTNAMES.includes(window.location.hostname)) {
    return LOCAL_BACKEND_URL;
  }
  if (process.env.NODE_ENV !== 'production') {
    return LOCAL_BACKEND_URL;
  }
  return PRODUCTION_BACKEND_URL;
};
