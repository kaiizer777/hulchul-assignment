'use client';

import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

import { Navbar } from './Navbar';

/**
 * Routes that own the whole viewport and must not inherit the app chrome
 * (navbar + padded page container). Auth surfaces render their own shell, so
 * they are matched exactly — never by prefix — to avoid silently swallowing
 * chrome for any future nested route.
 */
const CHROMELESS_ROUTES = ['/login'];

const isChromeless = (pathname: string | null): boolean =>
  pathname !== null && CHROMELESS_ROUTES.includes(pathname);

/**
 * Route-aware application chrome. Every route renders the navbar and the padded
 * `main` container except the chromeless routes above, which render their
 * children bare so the page can own its own full-height layout. When
 * `usePathname()` has no value yet (static render / not-found boundary) the
 * chrome is rendered, so an unresolvable path never loses its navigation.
 */
export function SiteChrome({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  if (isChromeless(pathname)) {
    return <>{children}</>;
  }

  return (
    <>
      <Navbar />
      <main className="flex-1 w-full max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {children}
      </main>
    </>
  );
}
