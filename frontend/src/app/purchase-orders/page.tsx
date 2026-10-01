'use client';

import { useEffect, useState } from 'react';
import type { PurchaseOrderDTO } from '@/lib/types';

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
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
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
          Purchase Orders
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Master registry of approved purchase orders across all vendors.
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
            Loading purchase orders...
          </div>
        </div>
      ) : error ? (
        <div className="rounded-xl border border-red-200 bg-red-50 p-6 text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200">
          <div className="flex items-center justify-between">
            <div>
              <p className="font-semibold text-sm">Failed to load purchase orders</p>
              <p className="text-xs text-red-700 dark:text-red-400 mt-1">{error}</p>
            </div>
            <button
              onClick={fetchPurchaseOrders}
              className="rounded-md bg-red-100 px-3 py-1.5 text-xs font-semibold text-red-800 hover:bg-red-200 dark:bg-red-900/60 dark:text-red-200"
            >
              Retry
            </button>
          </div>
        </div>
      ) : purchaseOrders.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white p-12 text-center dark:border-zinc-800 dark:bg-zinc-900/30">
          <div className="rounded-full bg-zinc-100 p-3 text-zinc-400 dark:bg-zinc-800">
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.5"
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
          </div>
          <h3 className="mt-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            No purchase orders recorded
          </h3>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            Purchase orders will appear here once entered into the database.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200 bg-zinc-50/75 text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/90 dark:text-zinc-400">
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
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {purchaseOrders.map((po) => (
                  <tr
                    key={po.po_number}
                    className="transition-colors hover:bg-zinc-50/60 dark:hover:bg-zinc-800/40"
                  >
                    <td className="whitespace-nowrap px-5 py-4 font-mono text-xs font-semibold text-zinc-900 dark:text-zinc-100">
                      {po.po_number}
                    </td>
                    <td className="whitespace-nowrap px-5 py-4 font-medium text-zinc-900 dark:text-zinc-100">
                      {po.vendor}
                    </td>
                    <td className="whitespace-nowrap px-5 py-4 text-right font-mono text-xs font-semibold text-zinc-900 dark:text-zinc-100">
                      {formatCurrency(po.approved_amount)}
                    </td>
                    <td className="whitespace-nowrap px-5 py-4">
                      <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-800 border border-emerald-300 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800/50">
                        {po.status || 'approved'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="border-t border-zinc-200 bg-zinc-50/50 px-5 py-3 text-xs text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/50 dark:text-zinc-400">
            Showing {purchaseOrders.length} {purchaseOrders.length === 1 ? 'purchase order' : 'purchase orders'}
          </div>
        </div>
      )}
    </div>
  );
}
