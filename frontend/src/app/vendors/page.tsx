'use client';

import { useEffect, useState } from 'react';
import type { VendorDTO } from '@/lib/types';

export default function VendorsPage() {
  const [vendors, setVendors] = useState<VendorDTO[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchVendors = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/vendors', { cache: 'no-store' });
      if (!res.ok) {
        throw new Error(`Failed to load vendors (${res.status})`);
      }
      const data: VendorDTO[] = await res.json();
      setVendors(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchVendors();
  }, []);

  return (
    <div className="space-y-6">
      {/* Header section */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
          Vendors
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Directory of registered vendors and commercial counterparties.
        </p>
      </div>

      {/* Content states */}
      {isLoading ? (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60 p-8 text-center">
          <div className="inline-flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
            <svg
              className="h-4 w-4 animate-spin text-zinc-400"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v8H4z"
              />
            </svg>
            Loading vendors...
          </div>
        </div>
      ) : error ? (
        <div className="rounded-xl border border-red-200 bg-red-50 p-6 text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200">
          <div className="flex items-center justify-between">
            <div>
              <p className="font-semibold text-sm">Failed to load vendors</p>
              <p className="text-xs text-red-700 dark:text-red-400 mt-1">{error}</p>
            </div>
            <button
              onClick={fetchVendors}
              className="rounded-md bg-red-100 px-3 py-1.5 text-xs font-semibold text-red-800 hover:bg-red-200 dark:bg-red-900/60 dark:text-red-200"
            >
              Retry
            </button>
          </div>
        </div>
      ) : vendors.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white p-12 text-center dark:border-zinc-800 dark:bg-zinc-900/30">
          <div className="rounded-full bg-zinc-100 p-3 text-zinc-400 dark:bg-zinc-800">
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.5"
                d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"
              />
            </svg>
          </div>
          <h3 className="mt-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            No vendors registered
          </h3>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            Vendors will show up here once configured.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200 bg-zinc-50/75 text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/90 dark:text-zinc-400">
                <tr>
                  <th scope="col" className="px-5 py-3.5">
                    ID
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Vendor Name
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {vendors.map((v) => (
                  <tr
                    key={v.id}
                    className="transition-colors hover:bg-zinc-50/60 dark:hover:bg-zinc-800/40"
                  >
                    <td className="whitespace-nowrap px-5 py-4 font-mono text-xs text-zinc-500 dark:text-zinc-400">
                      {v.id}
                    </td>
                    <td className="whitespace-nowrap px-5 py-4 font-medium text-zinc-900 dark:text-zinc-100">
                      {v.name}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="border-t border-zinc-200 bg-zinc-50/50 px-5 py-3 text-xs text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/50 dark:text-zinc-400">
            Showing {vendors.length} {vendors.length === 1 ? 'vendor' : 'vendors'}
          </div>
        </div>
      )}
    </div>
  );
}
