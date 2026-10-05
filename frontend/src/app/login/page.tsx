'use client';

import { Suspense, useState, useEffect, type FormEvent } from 'react';
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
 * Vertical rhythm is fluid in viewport height rather than fixed per breakpoint.
 * The auth surface has to fit a 600px laptop and a 667px phone without a
 * scrollbar, so group gaps and card padding shrink together as the viewport
 * shortens instead of letting the page grow.
 */
const GAP = 'mt-[clamp(0.875rem,2.2vh,1.25rem)]';

/**
 * Submit button treatments. Dark neutral is the product's primary action
 * colour (see the Agent Control primary action and the ERP_CORE mark), so the
 * engaged state keeps the established three-stop gradient, split bevel and
 * neutral cast shadow. The locked state deliberately drops the whole depth
 * recipe rather than fading it: an empty form must read as an inactive
 * control, not as a broken button.
 */
const SUBMIT_BASE =
  'group inline-flex h-12 w-full items-center justify-center gap-2 rounded-xl px-5 text-sm font-semibold tracking-tight transition-all duration-150 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40 focus-visible:ring-offset-2 focus-visible:ring-offset-white motion-reduce:transition-none';

const SUBMIT_ENGAGED =
  'cursor-pointer border border-t-zinc-600 border-x-zinc-800 border-b-black bg-gradient-to-b from-zinc-700 via-zinc-800 to-zinc-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.22),0_2px_6px_rgba(0,0,0,0.28)] hover:from-zinc-600 hover:via-zinc-700 hover:to-zinc-900 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.45),0_1px_2px_rgba(0,0,0,0.3)]';

const SUBMIT_LOCKED =
  'cursor-not-allowed border border-zinc-200 bg-zinc-100 text-zinc-400 shadow-none';

/**
 * Password in, session cookie out. The value lives only in this component's
 * state for the lifetime of the input: no `localStorage`, no `sessionStorage`,
 * no cookie, no query string.
 */
function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [rememberSession, setRememberSession] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    fetch('/api/auth/session', { cache: 'no-store' })
      .then((res) => {
        if (res.ok) {
          router.push('/agent');
        }
      })
      .catch(() => {});
  }, [router]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting || password.length === 0) return;

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
        // Navigation re-renders the destination from the server with the fresh
        // session cookie, so no refresh() here: issued after push() it
        // revalidates /login and supersedes the pending navigation.
        router.push(resolveReturnTo(searchParams.get('returnTo')));
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

  const isLocked = password.length === 0;
  const submitClassName = `${SUBMIT_BASE} ${
    isLocked ? SUBMIT_LOCKED : SUBMIT_ENGAGED
  }`;

  return (
    <div className="relative flex h-dvh w-full flex-col overflow-hidden">
      {/*
        Ambient surface wash. Neutral on purpose: a tinted radiance behind the
        card competes with the card's own lift, so this stays a near-white to
        slate lift that only separates the card from the page.
      */}
      <div className="pointer-events-none absolute inset-0 -z-10 overflow-hidden" aria-hidden="true">
        <div className="absolute left-1/2 top-1/2 h-[480px] w-[680px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-gradient-to-tr from-sky-100/50 via-white to-zinc-200/50 blur-3xl" />
      </div>

      <div className="flex min-h-0 w-full flex-1 flex-col items-center justify-center gap-[clamp(0.75rem,2vh,1rem)] overflow-hidden px-4 py-[clamp(0.75rem,2.5vh,1.5rem)] sm:px-6">
        <div className="w-full max-w-md">
          {/*
            Auth card. Light-mode depth: split bevel (white top / mid sides /
            darker bottom) reads as thickness under a single light from above,
            a soft neutral cast shadow lifts it off the page, and the fill is
            the faintest possible top-to-bottom gradient.
          */}
          <div className="rounded-2xl border border-t-white border-x-zinc-200/90 border-b-zinc-300/90 bg-gradient-to-b from-white to-zinc-50/60 p-[clamp(1.125rem,3vh,1.75rem)] shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03),0_16px_32px_-12px_rgba(15,23,42,0.12)]">
            {/* Identity block */}
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-t-zinc-600 border-x-zinc-800 border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.28),0_2px_6px_rgba(0,0,0,0.28)]">
                <svg
                  className="h-[18px] w-[18px]"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path
                    d="M12 2L2 7l10 5 10-5-10-5z"
                    className="text-zinc-300"
                    fill="currentColor"
                    fillOpacity="0.22"
                  />
                  <path d="M2 17l10 5 10-5" />
                  <path d="M2 12l10 5 10-5" />
                </svg>
              </div>

              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-bold uppercase leading-none tracking-[0.14em] text-zinc-900">
                    Operations Hub
                  </span>
                  <span className="rounded border border-zinc-200 bg-zinc-100/80 px-1.5 py-px font-mono text-[9px] font-semibold leading-none tracking-[0.12em] text-zinc-500">
                    ERP CORE
                  </span>
                </div>
                <p className="mt-1.5 font-mono text-[10px] leading-none tracking-wide text-zinc-400">
                  Secure Operator Gateway
                </p>
              </div>
            </div>

            <h1 className={`text-balance text-xl font-bold tracking-tight text-zinc-900 sm:text-2xl ${GAP}`}>
              Sign in to ERP Hub
            </h1>
            <p className={`text-pretty mt-1.5 max-w-sm text-[13px] leading-relaxed text-zinc-500 [@media(max-height:520px)]:hidden`}>
              Operator access to invoices, purchase orders, vendors, and the autonomous
              browser agent console.
            </p>

            {/* Form */}
            <form
              onSubmit={handleSubmit}
              className={`flex flex-col gap-[clamp(0.75rem,2vh,1.25rem)] ${GAP} border-t border-zinc-200/80 pt-[clamp(0.875rem,2.2vh,1.25rem)]`}
            >
              <div>
                <div className="flex items-baseline justify-between gap-3">
                  <label
                    htmlFor="password"
                    className="text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-600"
                  >
                    Operator Password
                  </label>
                  <span className="font-mono text-[10px] leading-none text-zinc-400">
                    Required
                  </span>
                </div>

                <div className="relative mt-[clamp(0.375rem,1.2vh,0.5rem)]">
                  <input
                    id="password"
                    name="password"
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    required
                    autoFocus
                    suppressHydrationWarning
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="Enter operator password"
                    aria-invalid={!!error}
                    aria-describedby={error ? 'auth-error' : undefined}
                    className="block h-12 w-full rounded-xl border border-zinc-300/90 bg-zinc-50/70 pl-3.5 pr-12 text-sm text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 ease-out focus:border-zinc-900 focus:bg-white focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    suppressHydrationWarning
                    className="absolute inset-y-1 right-1 flex w-9 items-center justify-center rounded-lg text-zinc-400 transition-colors duration-150 ease-out hover:text-zinc-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900"
                    title={showPassword ? 'Hide password' : 'Show password'}
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    aria-pressed={showPassword}
                  >
                    {showPassword ? (
                      <svg
                        className="h-4 w-4"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                        <path strokeLinecap="round" strokeLinejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                      </svg>
                    ) : (
                      <svg
                        className="h-4 w-4"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18" />
                      </svg>
                    )}
                  </button>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2.5">
                <label className="group/checkbox inline-flex cursor-pointer select-none items-center gap-2.5">
                  <input
                    type="checkbox"
                    checked={rememberSession}
                    onChange={(e) => setRememberSession(e.target.checked)}
                    className="h-4 w-4 shrink-0 cursor-pointer rounded-[4px] border-zinc-300 accent-zinc-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-zinc-900"
                  />
                  <span className="text-[12px] font-medium text-zinc-600 transition-colors duration-150 ease-out group-hover/checkbox:text-zinc-900">
                    Remember session in browser
                  </span>
                </label>

                <span className="hidden items-center gap-1.5 text-[10px] font-medium text-zinc-400 sm:inline-flex">
                  Press
                  <kbd className="rounded border border-zinc-200 bg-zinc-50 px-1 py-px font-mono text-[10px] leading-[14px] text-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.9)]">
                    Enter ↵
                  </kbd>
                  to submit
                </span>
              </div>

              {error && (
                <div
                  id="auth-error"
                  role="alert"
                  className="flex items-start gap-2.5 rounded-lg border border-t-rose-200 border-x-rose-300/80 border-b-rose-400/80 bg-gradient-to-b from-rose-50 to-rose-100/40 px-3 py-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]"
                >
                  <svg
                    className="mt-px h-3.5 w-3.5 shrink-0 text-rose-600"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <p className="text-[12px] font-medium leading-relaxed text-rose-800">{error}</p>
                </div>
              )}

              <button
                type="submit"
                disabled={isSubmitting || isLocked}
                aria-busy={isSubmitting}
                className={submitClassName}
              >
                {isSubmitting ? (
                  <>
                    <svg
                      className="h-4 w-4 animate-spin text-zinc-300 motion-reduce:animate-none"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      aria-hidden="true"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                    <span>Authenticating...</span>
                  </>
                ) : (
                  <>
                    <span>Sign In to Console</span>
                    <svg
                      className="h-4 w-4 transition-transform duration-150 ease-out group-hover:translate-x-0.5 group-focus-visible:translate-x-0.5 motion-reduce:transform-none"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M14 5l7 7m0 0l-7 7m7-7H3" />
                    </svg>
                  </>
                )}
              </button>
            </form>

            {/* Transport guarantee */}
            <div
              className={`flex items-start gap-2.5 rounded-lg border border-zinc-200/80 bg-zinc-50/80 px-3 py-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.7)] ${GAP}`}
            >
              <svg
                className="mt-px h-3.5 w-3.5 shrink-0 text-zinc-400"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
              </svg>
              <p className="text-pretty text-[11px] leading-relaxed text-zinc-500">
                <span className="font-semibold text-zinc-700">Encrypted Transport:</span>{' '}
                The session cookie is protected with{' '}
                <code className="rounded border border-zinc-200 bg-white px-1 py-px font-mono text-[10px] font-medium leading-[14px] text-zinc-600">
                  HttpOnly
                </code>
                ,{' '}
                <code className="rounded border border-zinc-200 bg-white px-1 py-px font-mono text-[10px] font-medium leading-[14px] text-zinc-600">
                  Secure
                </code>
                , and{' '}
                <code className="rounded border border-zinc-200 bg-white px-1 py-px font-mono text-[10px] font-medium leading-[14px] text-zinc-600">
                  SameSite=Lax
                </code>
                .
              </p>
            </div>
          </div>
        </div>

        {/* Footer strip: quiet, centred as a pair, never pushed by the card. */}
        <div className="flex shrink-0 flex-wrap items-center justify-center gap-x-2.5 gap-y-1">
          <span className="font-mono text-[10px] leading-none text-zinc-400">
            ERP Node Status: Online
          </span>
          <span className="h-2.5 w-px bg-zinc-300/80" aria-hidden="true" />
          <span className="font-mono text-[10px] leading-none text-zinc-400">v2.4.0-prod</span>
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="relative flex h-dvh w-full flex-col overflow-hidden">
          <div className="flex min-h-0 w-full flex-1 flex-col items-center justify-center gap-[clamp(0.75rem,2vh,1rem)] overflow-hidden px-4 py-[clamp(0.75rem,2.5vh,1.5rem)] sm:px-6">
            <div className="w-full max-w-md">
              <div className="animate-pulse rounded-2xl border border-zinc-200/70 bg-white/60 p-[clamp(1.125rem,3vh,1.75rem)] shadow-sm" />
            </div>
            <div className="shrink-0 font-mono text-[10px] leading-none text-zinc-300">
              v2.4.0-prod
            </div>
          </div>
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
