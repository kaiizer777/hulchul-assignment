'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const NAV_ITEMS = [
  { href: '/invoices', label: 'Invoices' },
  { href: '/invoices/new', label: 'New Invoice' },
  { href: '/purchase-orders', label: 'Purchase Orders' },
  { href: '/vendors', label: 'Vendors' },
  { href: '/agent', label: 'Agent Control', isAgent: true },
];

/**
 * Navbar component renders the top navigation header with ERP operations links and live sync status indicator.
 */
export function Navbar() {
  const pathname = usePathname();

  const isItemActive = (href: string) => {
    if (!pathname) return false;
    if (href === '/invoices') {
      return pathname === '/invoices' || (pathname.startsWith('/invoices/') && pathname !== '/invoices/new');
    }
    return pathname === href || (href !== '/' && pathname.startsWith(href));
  };

  return (
    <header className="sticky top-0 z-40 w-full border-b border-zinc-200/90 bg-white/90 backdrop-blur-md shadow-[0_1px_3px_rgba(0,0,0,0.02)]">
      <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-6 sm:gap-8">
          <Link href="/invoices" className="flex items-center gap-3 group transition-opacity">
            {/* Engineered 3D Tactile ERP Mark */}
            <div className="relative flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-b from-zinc-800 via-zinc-900 to-black text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_2px_6px_rgba(0,0,0,0.3)] border-t border-t-zinc-600 border-x border-x-zinc-800 border-b border-b-black group-hover:from-zinc-750 group-hover:to-zinc-900 transition-all">
              <svg className="h-4 w-4 text-zinc-100 transition-transform group-hover:scale-105" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                {/* 3D Stack / Isometric Layers representing ERP Ledger & Autonomy */}
                <path d="M12 2L2 7l10 5 10-5-10-5z" className="text-zinc-200" fill="currentColor" fillOpacity="0.2" />
                <path d="M2 17l10 5 10-5" />
                <path d="M2 12l10 5 10-5" />
              </svg>
            </div>

            <div className="flex flex-col">
              <div className="flex items-center gap-1.5">
                <span className="text-sm font-bold tracking-tight text-zinc-900 leading-none">
                  Operations Hub
                </span>
                <span className="rounded-md bg-zinc-900 text-white font-mono text-[9px] font-bold px-1.5 py-0.5 tracking-wider border-t border-t-zinc-700 border-b border-b-black shadow-2xs">
                  ERP
                </span>
              </div>
              <span className="text-[10px] font-medium text-zinc-400 leading-tight mt-0.5 font-mono">
                Autonomous Ledger Core
              </span>
            </div>
          </Link>

          <nav className="flex items-center gap-1 sm:gap-1.5 p-1 rounded-xl bg-zinc-100/80 border border-zinc-200/60 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)]" aria-label="Main Navigation">
            {NAV_ITEMS.map((item) => {
              const active = isItemActive(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1 text-xs transition-all duration-150 active:translate-y-[0.5px] ${
                    active
                      ? 'border border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white text-zinc-900 font-semibold shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.04)]'
                      : 'text-zinc-600 hover:text-zinc-900 hover:bg-white/60 font-medium'
                  }`}
                  aria-current={active ? 'page' : undefined}
                >
                  {item.isAgent && (
                    <span className="relative flex h-1.5 w-1.5">
                      <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${active ? 'bg-emerald-400 opacity-75' : 'bg-zinc-400 opacity-40'}`}></span>
                      <span className={`relative inline-flex rounded-full h-1.5 w-1.5 ${active ? 'bg-emerald-500' : 'bg-zinc-400'}`}></span>
                    </span>
                  )}
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          <div className="hidden sm:flex items-center gap-1.5 text-[11px] font-mono text-zinc-500 rounded-lg border border-zinc-200/70 bg-zinc-50 px-2.5 py-1 shadow-2xs">
            <span className="text-zinc-400">ENV:</span>
            <span className="font-semibold text-zinc-700">PROD-MOCK</span>
          </div>

          <span className="inline-flex items-center gap-1.5 rounded-full border border-t-emerald-200 border-x-emerald-300/80 border-b-emerald-400/80 bg-gradient-to-b from-emerald-50 to-emerald-100/60 px-2.5 py-1 text-[11px] font-semibold text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)]">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)] animate-pulse" />
            Live Sync
          </span>
        </div>
      </div>
    </header>
  );
}

