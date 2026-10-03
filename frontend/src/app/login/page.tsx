'use client';

import { Suspense, useState, type FormEvent } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

import { resolveReturnTo } from '@/lib/return-to';

/**
 * `Retry-After` is either a delay in seconds or an HTTP date. Only the numeric
 * form is rendered; anything else falls back to a generic message rather than
 * inventing a wait the server did not ask for.
 */
const retryAfterSeconds = (header: string | null): number | null => {
  if (!header) return null;
  const seconds = Number(header.trim());
  return Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : null;
};

/**
 * Password in, session cookie out. The value lives only in this component's
 * state for the lifetime of the input: no `localStorage`, no `sessionStorage`,
 * no cookie, no query string.
 */
function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting) return;

    setError(null);
    setIsSubmitting(true);

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
        cache: 'no-store',
      });

      if (response.status === 200) {
        router.push(resolveReturnTo(searchParams.get('returnTo')));
        // The cookie was just set, so server-rendered pages must re-render
        // against the new session.
        router.refresh();
        return;
      }

      if (response.status === 401) {
        setError('Incorrect password.');
      } else if (response.status === 429) {
        const seconds = retryAfterSeconds(response.headers.get('Retry-After'));
        setError(
          seconds
            ? `Too many failed attempts. Try again in ${seconds} seconds.`
            : 'Too many failed attempts. Try again later.'
        );
      } else if (response.status === 503) {
        setError('Sign-in is temporarily unavailable. Try again shortly.');
      } else {
        setError('Sign-in failed. Try again.');
      }
    } catch {
      setError('Cannot reach the sign-in service. Check your connection and try again.');
    } finally {
      // Drop the plaintext regardless of outcome; a failed attempt must not
      // leave the previous guess sitting in the DOM.
      setPassword('');
      setIsSubmitting(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-md space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
          Sign in
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Operator access to invoices, purchase orders, vendors and the agent console.
        </p>
      </div>

      <form
        onSubmit={handleSubmit}
        className="space-y-5 rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60"
      >
        <div>
          <label
            htmlFor="password"
            className="block text-xs font-semibold uppercase tracking-wider text-zinc-700 dark:text-zinc-300"
          >
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            autoFocus
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Enter the operator password"
            className="mt-1.5 block w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 shadow-sm placeholder-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:placeholder-zinc-500 dark:focus:border-zinc-400 dark:focus:ring-zinc-400"
          />
        </div>

        {error && (
          <p
            role="alert"
            className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-xs font-medium text-red-700 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300"
          >
            <svg
              className="h-4 w-4 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth="2"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            <span>{error}</span>
          </p>
        )}

        <button
          type="submit"
          disabled={isSubmitting || password.length === 0}
          className="inline-flex w-full items-center justify-center rounded-lg bg-zinc-900 px-5 py-2 text-sm font-medium text-white shadow-sm transition-all hover:bg-zinc-800 active:translate-y-px disabled:pointer-events-none disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
        >
          {isSubmitting ? (
            <span className="flex items-center gap-2">
              <svg className="h-4 w-4 animate-spin text-current" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              Signing in
            </span>
          ) : (
            <span>Sign in</span>
          )}
        </button>
      </form>

      <p className="text-center text-xs text-zinc-400 dark:text-zinc-500">
        The session cookie is HttpOnly, Secure and SameSite=Lax. Closing the browser ends the session.
      </p>
    </div>
  );
}

export default function LoginPage() {
  // `useSearchParams` opts a page out of static prerendering unless it is read
  // inside a Suspense boundary.
  return (
    <Suspense
      fallback={
        <div className="mx-auto h-72 w-full max-w-md animate-pulse rounded-xl border border-zinc-200/70 bg-white/60 p-6 dark:border-zinc-800 dark:bg-zinc-900/40" />
      }
    >
      <LoginForm />
    </Suspense>
  );
}