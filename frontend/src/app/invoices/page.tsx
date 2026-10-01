'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { StatusBadge } from '../components/StatusBadge';
import type { InvoiceDTO } from '@/lib/types';

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

type StatusFilter = 'all' | 'pending' | 'completed' | 'flagged' | 'failed' | 'skipped';

export default function InvoicesPage() {
  const [invoices, setInvoices] = useState<InvoiceDTO[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Search and filter states
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  // Clipboard copy state
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const fetchInvoices = async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setIsLoading(true);
    }
    setError(null);
    try {
      const res = await fetch('/api/invoices', { cache: 'no-store' });
      if (!res.ok) {
        throw new Error(`Failed to load invoices (${res.status})`);
      }
      const data: InvoiceDTO[] = await res.json();
      setInvoices(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred');
    } finally {
      setIsLoading(false);
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    fetchInvoices();
  }, []);

  const handleCopyId = async (id: string) => {
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard) {
        await navigator.clipboard.writeText(id);
        setCopiedId(id);
        setTimeout(() => {
          setCopiedId((current) => (current === id ? null : current));
        }, 1500);
      }
    } catch (err) {
      console.error('Failed to copy ID to clipboard', err);
    }
  };

  // Metrics computation
  const metrics = useMemo(() => {
    const totalInvoices = invoices.length;
    const totalValue = invoices.reduce((acc, inv) => acc + (inv.amount || 0), 0);
    const pendingCount = invoices.filter(
      (inv) => inv.status.toLowerCase() === 'pending'
    ).length;
    const flaggedOrFailedCount = invoices.filter((inv) => {
      const s = inv.status.toLowerCase();
      return s === 'flagged' || s === 'failed' || s === 'rejected';
    }).length;

    return {
      totalInvoices,
      totalValue,
      pendingCount,
      flaggedOrFailedCount,
    };
  }, [invoices]);

  // Counts per status for filter tabs
  const statusCounts = useMemo(() => {
    const counts: Record<StatusFilter, number> = {
      all: invoices.length,
      pending: 0,
      completed: 0,
      flagged: 0,
      failed: 0,
      skipped: 0,
    };

    for (const inv of invoices) {
      const s = inv.status.toLowerCase();
      if (s === 'pending') counts.pending++;
      else if (s === 'completed' || s === 'approved') counts.completed++;
      else if (s === 'flagged') counts.flagged++;
      else if (s === 'failed' || s === 'rejected') counts.failed++;
      else if (s === 'skipped') counts.skipped++;
    }

    return counts;
  }, [invoices]);

  // Filtered invoices
  const filteredInvoices = useMemo(() => {
    return invoices.filter((inv) => {
      const normalizedStatus = inv.status.toLowerCase();

      // Status check
      if (statusFilter !== 'all') {
        if (statusFilter === 'completed' && normalizedStatus !== 'completed' && normalizedStatus !== 'approved') {
          return false;
        }
        if (statusFilter === 'failed' && normalizedStatus !== 'failed' && normalizedStatus !== 'rejected') {
          return false;
        }
        if (statusFilter !== 'completed' && statusFilter !== 'failed' && normalizedStatus !== statusFilter) {
          return false;
        }
      }

      // Search query check
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const vendorMatch = inv.vendor.toLowerCase().includes(q);
        const poMatch = inv.po_number ? inv.po_number.toLowerCase().includes(q) : false;
        const idMatch = inv.id.toLowerCase().includes(q);
        if (!vendorMatch && !poMatch && !idMatch) {
          return false;
        }
      }

      return true;
    });
  }, [invoices, statusFilter, searchQuery]);

  const hasActiveFilters = searchQuery.trim() !== '' || statusFilter !== 'all';

  const resetFilters = () => {
    setSearchQuery('');
    setStatusFilter('all');
  };

  return (
    <div className="space-y-6">
      {/* Top Header section */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
              Invoices
            </h1>
            <span className="inline-flex items-center rounded-md border border-zinc-200 bg-zinc-100/80 px-2 py-0.5 font-mono text-xs font-medium text-zinc-600 dark:border-zinc-800 dark:bg-zinc-800/60 dark:text-zinc-400">
              {invoices.length} {invoices.length === 1 ? 'record' : 'records'}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            Manage, audit, and track accounts payable settlements and approvals.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            onClick={() => fetchInvoices(true)}
            disabled={isRefreshing || isLoading}
            title="Refresh invoices"
            aria-label="Refresh invoices"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-zinc-200/90 bg-white text-zinc-600 shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:border-zinc-300 hover:bg-zinc-50 active:translate-y-[0.5px] disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:border-zinc-700 dark:hover:bg-zinc-800/80"
          >
            <svg
              className={`h-4 w-4 ${isRefreshing ? 'animate-spin text-zinc-900 dark:text-zinc-100' : ''}`}
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

          <Link
            href="/invoices/new"
            className="inline-flex items-center justify-center gap-2 rounded-lg border-t border-t-zinc-700/70 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_2px_4px_rgba(0,0,0,0.2)] transition-all hover:from-zinc-750 hover:to-zinc-850 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.35)] dark:border-t-white dark:border-x-zinc-200 dark:border-b-zinc-400 dark:from-zinc-100 dark:to-zinc-200 dark:text-zinc-900 dark:shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_2px_4px_rgba(0,0,0,0.1)] dark:hover:from-white dark:hover:to-zinc-100"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth="2.2"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            <span>Create Invoice</span>
          </Link>
        </div>
      </div>

      {/* Summary Stats Bar / Metric Cards */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <div
              key={i}
              className="h-28 animate-pulse rounded-xl border border-zinc-200/70 bg-white/60 p-5 dark:border-zinc-800 dark:bg-zinc-900/40"
            >
              <div className="flex items-center justify-between">
                <div className="h-4 w-24 rounded bg-zinc-200 dark:bg-zinc-800" />
                <div className="h-8 w-8 rounded-lg bg-zinc-200 dark:bg-zinc-800" />
              </div>
              <div className="mt-3 h-7 w-20 rounded bg-zinc-200 dark:bg-zinc-800" />
              <div className="mt-2 h-3 w-32 rounded bg-zinc-100 dark:bg-zinc-800/60" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {/* Total Invoices */}
          <div className="relative overflow-hidden rounded-xl border border-zinc-200/80 border-t-white/90 bg-white/80 p-5 shadow-[0_1px_3px_rgba(0,0,0,0.03)] backdrop-blur-xs transition-all dark:border-zinc-800/80 dark:border-t-zinc-700/50 dark:bg-zinc-900/50">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Total Invoices
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-200/80 bg-zinc-50 text-zinc-700 dark:border-zinc-700/60 dark:bg-zinc-800 dark:text-zinc-300">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50 tabular-nums">
                {metrics.totalInvoices}
              </span>
              <span className="text-xs text-zinc-500 dark:text-zinc-400">records</span>
            </div>
            <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
              Total recorded in ledger
            </p>
          </div>

          {/* Total Value */}
          <div className="relative overflow-hidden rounded-xl border border-zinc-200/80 border-t-white/90 bg-white/80 p-5 shadow-[0_1px_3px_rgba(0,0,0,0.03)] backdrop-blur-xs transition-all dark:border-zinc-800/80 dark:border-t-zinc-700/50 dark:bg-zinc-900/50">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Total Value
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-200/80 bg-zinc-50 text-zinc-700 dark:border-zinc-700/60 dark:bg-zinc-800 dark:text-zinc-300">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50 tabular-nums">
                {formatCurrency(metrics.totalValue)}
              </span>
            </div>
            <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
              Cumulative dollar commitment
            </p>
          </div>

          {/* Pending Invoices */}
          <div className="relative overflow-hidden rounded-xl border border-zinc-200/80 border-t-white/90 bg-white/80 p-5 shadow-[0_1px_3px_rgba(0,0,0,0.03)] backdrop-blur-xs transition-all dark:border-zinc-800/80 dark:border-t-zinc-700/50 dark:bg-zinc-900/50">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400">
                Pending Review
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-amber-200/70 bg-amber-50 text-amber-600 dark:border-amber-800/50 dark:bg-amber-950/40 dark:text-amber-400">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-700 dark:text-amber-300 tabular-nums">
                {metrics.pendingCount}
              </span>
              <span className="text-xs text-zinc-500 dark:text-zinc-400">unsettled</span>
            </div>
            <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
              Awaiting verification & match
            </p>
          </div>

          {/* Flagged / Failed */}
          <div className="relative overflow-hidden rounded-xl border border-zinc-200/80 border-t-white/90 bg-white/80 p-5 shadow-[0_1px_3px_rgba(0,0,0,0.03)] backdrop-blur-xs transition-all dark:border-zinc-800/80 dark:border-t-zinc-700/50 dark:bg-zinc-900/50">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Flagged / Failed
              </span>
              <div
                className={`flex h-8 w-8 items-center justify-center rounded-lg border ${
                  metrics.flaggedOrFailedCount > 0
                    ? 'border-orange-200/80 bg-orange-50 text-orange-600 dark:border-orange-800/50 dark:bg-orange-950/40 dark:text-orange-400'
                    : 'border-zinc-200/80 bg-zinc-50 text-zinc-500 dark:border-zinc-700/60 dark:bg-zinc-800 dark:text-zinc-400'
                }`}
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span
                className={`font-mono text-2xl font-bold tracking-tight tabular-nums ${
                  metrics.flaggedOrFailedCount > 0
                    ? 'text-orange-600 dark:text-orange-400'
                    : 'text-zinc-900 dark:text-zinc-50'
                }`}
              >
                {metrics.flaggedOrFailedCount}
              </span>
              <span className="text-xs text-zinc-500 dark:text-zinc-400">issues</span>
            </div>
            <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
              Discrepancies requiring action
            </p>
          </div>
        </div>
      )}

      {/* Search & Filter Controls */}
      <div className="flex flex-col gap-3 rounded-xl border border-zinc-200/80 bg-white/70 p-3.5 shadow-[0_1px_3px_rgba(0,0,0,0.02)] backdrop-blur-xs sm:flex-row sm:items-center sm:justify-between dark:border-zinc-800/80 dark:bg-zinc-900/40">
        {/* Search input with quick-clear */}
        <div className="relative flex-1 sm:max-w-md">
          <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-zinc-400 dark:text-zinc-500">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </div>

          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by vendor, PO number, or ID..."
            className="block w-full rounded-lg border border-zinc-200 bg-white py-2 pl-9 pr-8 text-sm text-zinc-900 placeholder-zinc-400 shadow-xs transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700/80 dark:bg-zinc-950/60 dark:text-zinc-100 dark:placeholder-zinc-500 dark:focus:border-zinc-400 dark:focus:ring-zinc-400"
          />

          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              title="Clear search"
              aria-label="Clear search"
              className="absolute inset-y-0 right-0 flex items-center pr-2.5 text-zinc-400 hover:text-zinc-600 dark:text-zinc-500 dark:hover:text-zinc-300"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        {/* Filter Pills */}
        <div className="flex flex-wrap items-center gap-1.5 overflow-x-auto pb-0.5 sm:pb-0">
          {(
            [
              { key: 'all', label: 'All' },
              { key: 'pending', label: 'Pending' },
              { key: 'completed', label: 'Completed' },
              { key: 'flagged', label: 'Flagged' },
              { key: 'failed', label: 'Failed' },
              { key: 'skipped', label: 'Skipped' },
            ] as const
          ).map((item) => {
            const count = statusCounts[item.key];
            const isActive = statusFilter === item.key;

            return (
              <button
                key={item.key}
                onClick={() => setStatusFilter(item.key)}
                className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-all ${
                  isActive
                    ? 'border-t border-t-zinc-700/80 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-zinc-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.15)] dark:border-t-white dark:border-x-zinc-200 dark:border-b-zinc-400 dark:bg-zinc-100 dark:text-zinc-900'
                    : 'border border-zinc-200/80 bg-white/70 text-zinc-600 hover:border-zinc-300 hover:bg-zinc-50 hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400 dark:hover:border-zinc-700 dark:hover:bg-zinc-800 dark:hover:text-zinc-200'
                }`}
              >
                <span>{item.label}</span>
                <span
                  className={`rounded-full px-1.5 py-0.2 font-mono text-[10px] ${
                    isActive
                      ? 'bg-zinc-800 text-zinc-200 dark:bg-zinc-200 dark:text-zinc-800'
                      : 'bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400'
                  }`}
                >
                  {count}
                </span>
              </button>
            );
          })}

          {hasActiveFilters && (
            <button
              onClick={resetFilters}
              className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs font-medium text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
              title="Reset all filters"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
              <span>Reset</span>
            </button>
          )}
        </div>
      </div>

      {/* Main Content Area */}
      {isLoading ? (
        /* Skeleton Table Loading State */
        <div className="overflow-hidden rounded-xl border border-zinc-200/80 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02)] dark:border-zinc-800 dark:bg-zinc-900/60">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200/80 bg-zinc-50/75 text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/90 dark:text-zinc-400">
                <tr>
                  <th scope="col" className="px-5 py-3.5">Invoice ID</th>
                  <th scope="col" className="px-5 py-3.5">Vendor</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Amount</th>
                  <th scope="col" className="px-5 py-3.5">Date</th>
                  <th scope="col" className="px-5 py-3.5">PO Number</th>
                  <th scope="col" className="px-5 py-3.5">Status</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/80 dark:divide-zinc-800">
                {[...Array(5)].map((_, i) => (
                  <tr key={i} className="animate-pulse">
                    <td className="px-5 py-4">
                      <div className="h-4 w-20 rounded bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-4 w-32 rounded bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                    <td className="px-5 py-4 text-right">
                      <div className="ml-auto h-4 w-24 rounded bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-4 w-20 rounded bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-4 w-16 rounded bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-5 w-20 rounded-full bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                    <td className="px-5 py-4 text-right">
                      <div className="ml-auto h-4 w-12 rounded bg-zinc-200 dark:bg-zinc-800" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
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
                <p className="text-sm font-semibold">Failed to load invoices</p>
                <p className="mt-0.5 text-xs text-red-700 dark:text-red-400">{error}</p>
              </div>
            </div>
            <button
              onClick={() => fetchInvoices(false)}
              className="rounded-lg border border-red-300 bg-white px-3.5 py-1.5 text-xs font-semibold text-red-800 shadow-xs hover:bg-red-50 dark:border-red-800 dark:bg-red-900/40 dark:text-red-200 dark:hover:bg-red-900/60"
            >
              Retry Connection
            </button>
          </div>
        </div>
      ) : invoices.length === 0 ? (
        /* Empty State: 0 invoices in system */
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
            No invoices recorded yet
          </h3>
          <p className="mt-1.5 max-w-sm text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
            Your ledger is currently empty. Record your first vendor invoice to begin tracking payments and matching purchase orders.
          </p>
          <div className="mt-5">
            <Link
              href="/invoices/new"
              className="inline-flex items-center gap-2 rounded-lg border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 px-4 py-2 text-xs font-semibold text-white shadow-[0_2px_4px_rgba(0,0,0,0.18)] transition-all hover:from-zinc-750 hover:to-zinc-850 active:translate-y-[0.5px] dark:border-t-white dark:border-x-zinc-200 dark:border-b-zinc-400 dark:from-zinc-100 dark:to-zinc-200 dark:text-zinc-900"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
              </svg>
              <span>Add Your First Invoice</span>
            </Link>
          </div>
        </div>
      ) : filteredInvoices.length === 0 ? (
        /* Empty State: 0 results matching filter */
        <div className="flex flex-col items-center justify-center rounded-2xl border border-zinc-200/80 bg-white/70 p-12 text-center shadow-xs backdrop-blur-xs dark:border-zinc-800/80 dark:bg-zinc-900/40">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-zinc-200 bg-zinc-100 text-zinc-400 dark:border-zinc-800 dark:bg-zinc-800 dark:text-zinc-500">
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
              />
            </svg>
          </div>
          <h3 className="mt-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            No matching invoices
          </h3>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            No invoices matched your current search and filter settings.
          </p>
          <div className="mt-4">
            <button
              onClick={resetFilters}
              className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-3.5 py-1.5 text-xs font-medium text-zinc-700 shadow-xs hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-700/80"
            >
              Reset Filters
            </button>
          </div>
        </div>
      ) : (
        /* Elevated Data Table */
        <div className="overflow-hidden rounded-xl border border-zinc-200/80 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02)] dark:border-zinc-800 dark:bg-zinc-900/60">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200/80 bg-zinc-50/75 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/90 dark:text-zinc-400">
                <tr>
                  <th scope="col" className="px-5 py-3.5">
                    Invoice ID
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Vendor
                  </th>
                  <th scope="col" className="px-5 py-3.5 text-right">
                    Amount
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Date
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    PO Number
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Status
                  </th>
                  <th scope="col" className="px-5 py-3.5 text-center">
                    Copy
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/70 dark:divide-zinc-800/80">
                {filteredInvoices.map((inv) => {
                  const isCopied = copiedId === inv.id;
                  const displayId = inv.id.length > 8 ? `${inv.id.slice(0, 8)}...` : inv.id;

                  return (
                    <tr
                      key={inv.id}
                      className="group transition-colors duration-150 hover:bg-zinc-50/70 dark:hover:bg-zinc-800/35"
                    >
                      {/* ID with click-to-copy */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <button
                          type="button"
                          onClick={() => handleCopyId(inv.id)}
                          className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 font-mono text-xs text-zinc-600 transition-colors hover:bg-zinc-100 hover:text-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-400 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
                          title={`Click to copy full ID: ${inv.id}`}
                        >
                          <span className="font-medium">{displayId}</span>
                        </button>
                      </td>

                      {/* Vendor */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <span className="font-medium text-zinc-900 dark:text-zinc-100">
                          {inv.vendor}
                        </span>
                      </td>

                      {/* Amount with tabular formatting */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        <span className="font-mono text-xs font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">
                          {formatCurrency(inv.amount)}
                        </span>
                      </td>

                      {/* Date */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-xs text-zinc-600 dark:text-zinc-400">
                        {inv.date}
                      </td>

                      {/* PO Number */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-xs font-mono">
                        {inv.po_number ? (
                          <span className="inline-flex items-center rounded border border-zinc-200/80 bg-zinc-50 px-1.5 py-0.5 text-[11px] font-medium text-zinc-700 dark:border-zinc-800 dark:bg-zinc-800/60 dark:text-zinc-300">
                            {inv.po_number}
                          </span>
                        ) : (
                          <span className="text-zinc-400 dark:text-zinc-600">—</span>
                        )}
                      </td>

                      {/* Enhanced Status Badge */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <StatusBadge status={inv.status} />
                      </td>

                      {/* Copy Action Column */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-center">
                        <button
                          type="button"
                          onClick={() => handleCopyId(inv.id)}
                          aria-label={`Copy invoice ID ${inv.id}`}
                          className={`inline-flex h-7 w-7 items-center justify-center rounded-md border transition-all ${
                            isCopied
                              ? 'border-emerald-300 bg-emerald-50 text-emerald-600 shadow-xs dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-400'
                              : 'border-zinc-200/70 bg-white text-zinc-400 opacity-60 hover:opacity-100 hover:border-zinc-300 hover:text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-500 dark:hover:border-zinc-700 dark:hover:text-zinc-300'
                          }`}
                          title={isCopied ? 'Copied to clipboard!' : 'Copy full UUID'}
                        >
                          {isCopied ? (
                            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                            </svg>
                          ) : (
                            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                            </svg>
                          )}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Table Footer with Counts & State */}
          <div className="flex flex-col gap-2 border-t border-zinc-200/80 bg-zinc-50/50 px-5 py-3 text-xs text-zinc-500 sm:flex-row sm:items-center sm:justify-between dark:border-zinc-800 dark:bg-zinc-900/50 dark:text-zinc-400">
            <div>
              Showing <span className="font-semibold text-zinc-700 dark:text-zinc-200">{filteredInvoices.length}</span> of{' '}
              <span className="font-semibold text-zinc-700 dark:text-zinc-200">{invoices.length}</span> {invoices.length === 1 ? 'invoice' : 'invoices'}
              {hasActiveFilters && ' (filtered)'}
            </div>

            {hasActiveFilters && (
              <button
                onClick={resetFilters}
                className="text-xs font-medium text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-300"
              >
                Clear filters to show all
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
