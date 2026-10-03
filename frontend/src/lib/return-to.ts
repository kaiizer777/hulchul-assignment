/**
 * Where the operator lands when no explicit, safe destination was supplied.
 * The agent console is the page the whole app exists to drive.
 */
const DEFAULT_RETURN_TO = '/agent';

/**
 * Open-redirect guard for the `?returnTo=` parameter (§11).
 *
 * A `returnTo` is fully attacker-controlled — it arrives in a link the operator
 * clicks. Anything but a single-slash-prefixed relative path must be discarded:
 * `//evil.com` and `/\evil.com` are both read by browsers as a
 * protocol-relative authority (the WHATWG URL parser normalises `\` to `/` for
 * special schemes), which would hand the operator a working phishing redirect
 * immediately after a successful login.
 */
const SAFE_RETURN_TO_PATTERN = /^\/[^/\\]/;

/** Fixed origin used to resolve the candidate and prove it cannot change origin. */
const RETURN_TO_BASE = 'https://return-to.invalid';

/**
 * Returns `candidate` when it is provably same-origin, otherwise the default.
 *
 * Two independent gates: the contract regex, then a resolution against a fixed
 * base that must keep the origin unchanged. The second is deliberately not
 * redundant — it is the net for anything the regex has not been shown to
 * reject.
 *
 * Lives here rather than in `app/login/page.tsx`: Next.js rejects page modules
 * that export anything beyond the default component and known config names, so
 * keeping this export in the page fails `next build` type checking.
 */
export const resolveReturnTo = (candidate: string | null | undefined): string => {
  if (typeof candidate !== 'string') return DEFAULT_RETURN_TO;
  if (candidate !== '/' && !SAFE_RETURN_TO_PATTERN.test(candidate)) return DEFAULT_RETURN_TO;

  try {
    const resolved = new URL(candidate, RETURN_TO_BASE);
    if (resolved.origin !== RETURN_TO_BASE) return DEFAULT_RETURN_TO;
  } catch {
    return DEFAULT_RETURN_TO;
  }

  return candidate;
};
