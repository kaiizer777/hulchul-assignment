const PRODUCTION_BACKEND_URL =
  'https://gmruxxvvxbypxv4d74kix7l6aq0ppwmd.lambda-url.us-east-1.on.aws';
const LOCAL_BACKEND_URL = 'http://localhost:8051';
const LOCAL_HOSTNAMES = ['localhost', '127.0.0.1'];

/**
 * Resolves the backend base URL used by every agent control request.
 *
 * Resolution order:
 *   1. `NEXT_PUBLIC_BACKEND_URL`, inlined into the client bundle by Next.js at
 *      build time. This is the only tier that works for a deployed bundle.
 *   2. `http://localhost:8051`, but only when the page is actually being served
 *      from a local development host.
 *   3. The production Lambda Function URL, as an explicit default.
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
  return PRODUCTION_BACKEND_URL;
};

/**
 * Builds the user-facing message for a request that failed below the HTTP layer
 * (connection refused, DNS failure, TLS failure or a CORS rejection), so the UI
 * reports which backend was unreachable instead of a bare "Failed to fetch".
 *
 * The message deliberately does not claim the server never saw the request. A
 * rejected fetch() cannot distinguish "never arrived" from "arrived, but the
 * browser refused the response" (CORS), and POST /agent/run only returns a run
 * id after the agent loop has already executed on the server. Asserting "no run
 * started" would invite a duplicate submission of side-effecting work, so the
 * operator is told to check the run status instead.
 *
 * Credentials embedded in the URL are stripped: this string is rendered on
 * screen and must never carry a secret.
 */
export const unreachableBackendMessage = (backendUrl: string): string => {
  let displayed = backendUrl;
  try {
    const parsed = new URL(backendUrl);
    if (parsed.username || parsed.password) {
      parsed.username = '';
      parsed.password = '';
      displayed = parsed.toString().replace(/\/$/, '');
    }
  } catch {
    displayed = backendUrl;
  }
  return (
    `Cannot read a response from the backend at ${displayed}. ` +
    'The request may have reached the server, so the run status may be unknown: ' +
    'check it before retrying. The backend may be down, or this origin may not be ' +
    'allowed by its CORS policy.'
  );
};