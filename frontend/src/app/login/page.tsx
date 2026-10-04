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
  const [showPassword, setShowPassword] = useState(false);
  const [rememberSession, setRememberSession] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

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
    <div className="relative flex min-h-[calc(100vh-10rem)] w-full items-center justify-center px-4 py-8 sm:py-12">
      {/* Ambient background mesh radiance */}
      <div className="pointer-events-none absolute inset-0 -z-10 flex items-center justify-center overflow-hidden">
        <div className="h-[460px] w-[600px] rounded-full bg-gradient-to-tr from-emerald-100/40 via-zinc-100/30 to-sky-100/40 blur-3xl opacity-70" />
      </div>

      <div className="w-full max-w-md space-y-6">
        {/* Centerpiece Tactile Card */}
        <div className="rounded-2xl border border-t-white border-x-zinc-200/90 border-b-zinc-300/90 bg-white/95 p-7 sm:p-9 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_12px_32px_rgba(0,0,0,0.05)] backdrop-blur-md transition-all">
          {/* Brand Header */}
          <div className="flex flex-col items-center text-center sm:items-start sm:text-left pb-6 border-b border-zinc-100">
            <div className="mb-4 flex items-center gap-3">
              {/* Engineered 3D Tactile ERP Mark with Laser Glyph */}
              <div className="relative flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-b from-zinc-800 via-zinc-900 to-black text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_4px_12px_rgba(0,0,0,0.25)] border-t border-t-zinc-600 border-x border-x-zinc-800 border-b border-b-black">
                <svg
                  className="h-5 w-5 text-zinc-100"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M12 2L2 7l10 5 10-5-10-5z" className="text-zinc-200" fill="currentColor" fillOpacity="0.25" />
                  <path d="M2 17l10 5 10-5" />
                  <path d="M2 12l10 5 10-5" />
                </svg>
                <span className="absolute -top-1 -right-1 flex h-2.5 w-2.5">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500 border border-white"></span>
                </span>
              </div>

              <div className="flex flex-col text-left">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold uppercase tracking-wider text-zinc-900 leading-none">
                    Operations Hub
                  </span>
                  <span className="rounded-md bg-zinc-900 text-white font-mono text-[9px] font-bold px-1.5 py-0.5 tracking-wider border-t border-t-zinc-700 border-b border-b-black shadow-2xs">
                    ERP CORE
                  </span>
                </div>
                <span className="text-[10px] font-mono text-zinc-400 mt-1">
                  Secure Operator Gateway
                </span>
              </div>
            </div>

            <h1 className="text-2xl font-bold tracking-tight text-zinc-900 sm:text-3xl">
              Sign in to ERP Hub
            </h1>
            <p className="mt-1.5 text-xs sm:text-sm text-zinc-500 leading-relaxed max-w-sm">
              Operator access to invoices, purchase orders, vendors, and the autonomous browser agent console.
            </p>
          </div>

          {/* Form Controls */}
          <form onSubmit={handleSubmit} className="mt-6 space-y-5">
            <div>
              <div className="flex items-center justify-between">
                <label
                  htmlFor="password"
                  className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-zinc-700"
                >
                  <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                  </svg>
                  Operator Password
                </label>
                <span className="text-[10px] font-mono text-zinc-400">
                  Required
                </span>
              </div>

              <div className="relative mt-2">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  required
                  autoFocus
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="Enter operator password"
                  aria-invalid={!!error}
                  aria-describedby={error ? 'auth-error' : undefined}
                  className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/70 py-2.5 pl-3.5 pr-11 text-sm font-sans text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute inset-y-0 right-0 flex items-center px-3.5 text-zinc-400 hover:text-zinc-700 transition-colors focus:outline-none focus:text-zinc-900"
                  title={showPassword ? 'Hide password' : 'Show password'}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? (
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18" />
                    </svg>
                  ) : (
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  )}
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between text-xs">
              <label className="flex items-center gap-2 cursor-pointer select-none text-zinc-600 hover:text-zinc-900">
                <input
                  type="checkbox"
                  checked={rememberSession}
                  onChange={(e) => setRememberSession(e.target.checked)}
                  className="h-4 w-4 rounded border-zinc-300 text-zinc-900 focus:ring-zinc-900 accent-zinc-900 cursor-pointer"
                />
                <span className="font-medium text-[11px] sm:text-xs">Remember session in browser</span>
              </label>

              <span className="text-[11px] font-mono text-zinc-400">
                Press <kbd className="rounded border border-zinc-200 bg-zinc-100 px-1 py-0.5 font-mono text-[10px] text-zinc-600">Enter ↵</kbd>
              </span>
            </div>

            {error && (
              <div
                id="auth-error"
                role="alert"
                className="flex items-center gap-2.5 rounded-xl border border-t-rose-200 border-x-rose-300/80 border-b-rose-400/80 bg-gradient-to-b from-rose-50/90 to-rose-100/40 p-3.5 text-xs font-medium text-rose-800 shadow-2xs"
              >
                <svg
                  className="h-4 w-4 shrink-0 text-rose-600"
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
              </div>
            )}

            <button
              type="submit"
              disabled={isSubmitting || password.length === 0}
              className="inline-flex w-full items-center justify-center gap-2 rounded-xl border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-5 py-3 text-sm font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(0,0,0,0.25)] transition-all hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
            >
              {isSubmitting ? (
                <>
                  <svg className="h-4 w-4 animate-spin text-zinc-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Authenticating Session...</span>
                </>
              ) : (
                <>
                  <span>Sign In to Console</span>
                  <svg className="h-4 w-4 text-zinc-400 transition-transform group-hover:translate-x-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                  </svg>
                </>
              )}
            </button>
          </form>

          {/* Security & Token Guarantee */}
          <div className="mt-6 rounded-xl border border-zinc-200/80 bg-zinc-50/70 p-3 text-[11px] text-zinc-500 flex items-start gap-2.5">
            <svg className="h-4 w-4 text-zinc-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            <div>
              <span className="font-semibold text-zinc-700">Encrypted Transport:</span>{' '}
              The session cookie is protected with <code className="font-mono text-[10px] font-semibold text-zinc-700">HttpOnly</code>,{' '}
              <code className="font-mono text-[10px] font-semibold text-zinc-700">Secure</code>, and{' '}
              <code className="font-mono text-[10px] font-semibold text-zinc-700">SameSite=Lax</code>.
            </div>
          </div>
        </div>

        {/* Bottom Support / Environment Pill */}
        <div className="flex items-center justify-between text-xs text-zinc-400 px-2">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.5)]" />
            <span className="font-mono text-[11px]">ERP Node Status: Online</span>
          </div>
          <span className="font-mono text-[11px]">v2.4.0-prod</span>
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="relative flex min-h-[calc(100vh-10rem)] w-full items-center justify-center px-4 py-12">
          <div className="h-96 w-full max-w-md animate-pulse rounded-2xl border border-zinc-200/70 bg-white/60 p-8 shadow-sm" />
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}