'use client';

import { useEffect, useState } from 'react';
import type { PurchaseOrderDTO } from '@/lib/types';

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

export default function PurchaseOrdersPage() {
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrderDTO[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchPurchaseOrders = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/purchase-orders', { cache: 'no-store' });
      if (!res.ok) {
        throw new Error(`Failed to load purchase orders (${res.status})`);
      }
      const data: PurchaseOrderDTO[] = await res.json();
      setPurchaseOrders(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchPurchaseOrders();
  }, []);

  return (
    <div className="space-y-6">
      {/* Header section */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
              Purchase Orders
            </h1>
            <span className="inline-flex items-center rounded-md border border-zinc-200 bg-zinc-100/80 px-2 py-0.5 font-mono text-xs font-medium text-zinc-600 dark:border-zinc-800 dark:bg-zinc-800/60 dark:text-zinc-400">
              {purchaseOrders.length} {purchaseOrders.length === 1 ? 'record' : 'records'}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            Master registry of approved purchase orders across all enterprise vendors.
          </p>
        </div>

        <button
          onClick={fetchPurchaseOrders}
          disabled={isLoading}
          title="Refresh purchase orders"
          aria-label="Refresh purchase orders"
          className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-zinc-200/90 bg-white text-zinc-600 shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:border-zinc-300 hover:bg-zinc-50 active:translate-y-[0.5px] disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:border-zinc-700 dark:hover:bg-zinc-800/80"
        >
          <svg
            className={`h-4 w-4 ${isLoading ? 'animate-spin text-zinc-900 dark:text-zinc-100' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth="2"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
        </button>
      </div>

      {/* Content states */}
      {isLoading ? (
        <div className="overflow-hidden rounded-xl border border-zinc-200/80 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02)] dark:border-zinc-800 dark:bg-zinc-900/60 p-8 text-center">
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
            Loading purchase orders...
          </div>
        </div>
      ) : error ? (
        <div className="rounded-xl border border-red-200 bg-red-50/80 p-6 text-red-900 shadow-xs dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200">
          <div className="flex items-center justify-between">
            <div className="flex items-start gap-3">
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-red-100 text-red-600 dark:bg-red-900/60 dark:text-red-300">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div>
                <p className="font-semibold text-sm">Failed to load purchase orders</p>
                <p className="text-xs text-red-700 dark:text-red-400 mt-0.5">{error}</p>
              </div>
            </div>
            <button
              onClick={fetchPurchaseOrders}
              className="rounded-lg border border-red-300 bg-white px-3.5 py-1.5 text-xs font-semibold text-red-800 shadow-xs hover:bg-red-50 dark:border-red-800 dark:bg-red-900/40 dark:text-red-200 dark:hover:bg-red-900/60"
            >
              Retry
            </button>
          </div>
        </div>
      ) : purchaseOrders.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/60 p-12 text-center backdrop-blur-xs dark:border-zinc-800 dark:bg-zinc-900/30">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-zinc-200/80 bg-zinc-100 text-zinc-500 shadow-xs dark:border-zinc-700/60 dark:bg-zinc-800/80 dark:text-zinc-400">
            <svg className="h-7 w-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
          </div>
          <h3 className="mt-4 text-base font-semibold text-zinc-900 dark:text-zinc-100">
            No purchase orders recorded
          </h3>
          <p className="mt-1.5 max-w-sm text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
            Purchase orders will appear here once entered into the enterprise database.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-zinc-200/80 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02)] dark:border-zinc-800 dark:bg-zinc-900/60">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200/80 bg-zinc-50/75 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/90 dark:text-zinc-400">
                <tr>
                  <th scope="col" className="px-5 py-3.5">
                    PO Number
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Vendor
                  </th>
                  <th scope="col" className="px-5 py-3.5 text-right">
                    Approved Amount
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Status
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/70 dark:divide-zinc-800/80">
                {purchaseOrders.map((po) => (
                  <tr
                    key={po.po_number}
                    className="group transition-colors duration-150 hover:bg-zinc-50/70 dark:hover:bg-zinc-800/35"
                  >
                    <td className="whitespace-nowrap px-5 py-3.5 font-mono text-xs font-semibold text-zinc-900 dark:text-zinc-100">
                      <span className="inline-flex items-center rounded border border-zinc-200/80 bg-zinc-50 px-2 py-0.5 text-xs font-medium text-zinc-800 dark:border-zinc-800 dark:bg-zinc-800/60 dark:text-zinc-200">
                        {po.po_number}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-5 py-3.5 font-medium text-zinc-900 dark:text-zinc-100">
                      {po.vendor}
                    </td>
                    <td className="whitespace-nowrap px-5 py-3.5 text-right font-mono text-xs font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">
                      {formatCurrency(po.approved_amount)}
                    </td>
                    <td className="whitespace-nowrap px-5 py-3.5">
                      <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-300/70 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-medium text-emerald-700 shadow-2xs dark:border-emerald-700/60 dark:bg-emerald-500/15 dark:text-emerald-300">
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.5)]" />
                        {po.status || 'Approved'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="border-t border-zinc-200/80 bg-zinc-50/50 px-5 py-3 text-xs text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/50 dark:text-zinc-400">
            Showing <span className="font-semibold text-zinc-700 dark:text-zinc-200">{purchaseOrders.length}</span> {purchaseOrders.length === 1 ? 'purchase order' : 'purchase orders'}
          </div>
        </div>
      )}
    </div>
  );
}
