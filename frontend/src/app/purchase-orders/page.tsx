'use client';

import { useEffect, useMemo, useState, useCallback } from 'react';
import Link from 'next/link';
import { StatusBadge } from '../components/StatusBadge';
import type { PurchaseOrderDTO } from '@/lib/types';

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

type StatusFilter = 'all' | 'approved' | 'open' | 'pending';

export default function PurchaseOrdersPage() {
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrderDTO[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Search, filter, and drawer state
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [selectedPO, setSelectedPO] = useState<PurchaseOrderDTO | null>(null);
  const [copiedPoNumber, setCopiedPoNumber] = useState<string | null>(null);

  const fetchPurchaseOrders = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setIsLoading(true);
    }
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
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchPurchaseOrders();
  }, [fetchPurchaseOrders]);

  // Handle ESC key for modal
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && selectedPO) {
        setSelectedPO(null);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedPO]);

  const handleCopyPO = async (poNumber: string, e?: React.MouseEvent) => {
    if (e) {
      e.stopPropagation();
    }
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard) {
        await navigator.clipboard.writeText(poNumber);
        setCopiedPoNumber(poNumber);
        setTimeout(() => {
          setCopiedPoNumber((curr) => (curr === poNumber ? null : curr));
        }, 1500);
      }
    } catch (err) {
      console.error('Failed to copy PO number to clipboard', err);
    }
  };

  // Metrics computation
  const metrics = useMemo(() => {
    const totalCount = purchaseOrders.length;
    const totalSpend = purchaseOrders.reduce(
      (acc, po) => acc + (po.approved_amount || 0),
      0
    );
    const approvedCount = purchaseOrders.filter((po) => {
      const s = (po.status || '').toLowerCase();
      return s === 'approved' || s === 'completed';
    }).length;
    const openCount = totalCount - approvedCount;
    const avgAmount = totalCount > 0 ? totalSpend / totalCount : 0;

    return {
      totalCount,
      totalSpend,
      approvedCount,
      openCount,
      avgAmount,
    };
  }, [purchaseOrders]);

  // Status counts for filter pills
  const statusCounts = useMemo(() => {
    const counts: Record<StatusFilter, number> = {
      all: purchaseOrders.length,
      approved: 0,
      open: 0,
      pending: 0,
    };

    for (const po of purchaseOrders) {
      const s = (po.status || '').toLowerCase();
      if (s === 'approved' || s === 'completed') {
        counts.approved++;
      } else if (s === 'pending') {
        counts.pending++;
        counts.open++;
      } else {
        counts.open++;
      }
    }

    return counts;
  }, [purchaseOrders]);

  // Filtered dataset
  const filteredPOs = useMemo(() => {
    return purchaseOrders.filter((po) => {
      const s = (po.status || '').toLowerCase();

      // Status filter
      if (statusFilter !== 'all') {
        if (statusFilter === 'approved' && s !== 'approved' && s !== 'completed') {
          return false;
        }
        if (statusFilter === 'open' && (s === 'approved' || s === 'completed')) {
          return false;
        }
        if (statusFilter === 'pending' && s !== 'pending') {
          return false;
        }
      }

      // Search filter
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const poMatch = po.po_number.toLowerCase().includes(q);
        const vendorMatch = po.vendor.toLowerCase().includes(q);
        if (!poMatch && !vendorMatch) {
          return false;
        }
      }

      return true;
    });
  }, [purchaseOrders, statusFilter, searchQuery]);

  const hasActiveFilters = searchQuery.trim() !== '' || statusFilter !== 'all';

  const resetFilters = () => {
    setSearchQuery('');
    setStatusFilter('all');
  };

  return (
    <div className="space-y-6 pb-14">
      {/* Page Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900">
              Purchase Orders
            </h1>
            <span className="inline-flex items-center gap-1.5 rounded-md border border-zinc-200/90 bg-zinc-50 px-2.5 py-0.5 font-mono text-xs font-semibold text-zinc-700 shadow-2xs">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.5)]" />
              {purchaseOrders.length} {purchaseOrders.length === 1 ? 'record' : 'records'}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            Authoritative registry of corporate purchase orders, spending caps, and accounts payable commitments.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            onClick={() => fetchPurchaseOrders(true)}
            disabled={isRefreshing || isLoading}
            title="Refresh purchase orders"
            aria-label="Refresh purchase orders"
            className="inline-flex items-center gap-1.5 rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-3.5 py-2 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)] hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-50 transition-all"
          >
            <svg
              className={`h-3.5 w-3.5 text-zinc-500 ${isRefreshing ? 'animate-spin text-zinc-900' : ''}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth="2.2"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
            <span>{isRefreshing ? 'Syncing...' : 'Sync Registry'}</span>
          </button>

          <Link
            href="/invoices/new"
            className="inline-flex items-center justify-center gap-2 rounded-xl border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-4 py-2 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(0,0,0,0.25)] hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px] transition-all"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            <span>Match with Invoice</span>
          </Link>
        </div>
      </div>

      {/* Tactile Metric Summary Cards */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <div
              key={i}
              className="h-28 animate-pulse rounded-2xl border border-zinc-200/90 bg-white/80 p-5 shadow-[0_1px_3px_rgba(0,0,0,0.02)]"
            >
              <div className="flex items-center justify-between">
                <div className="h-3.5 w-24 rounded bg-zinc-200" />
                <div className="h-8 w-8 rounded-xl bg-zinc-200" />
              </div>
              <div className="mt-3 h-7 w-20 rounded bg-zinc-200" />
              <div className="mt-2 h-3 w-32 rounded bg-zinc-100" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {/* Total POs */}
          <div className="relative overflow-hidden rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50/80 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-500">
                Total Purchase Orders
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-zinc-200/80 bg-zinc-50 text-zinc-700 shadow-2xs">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-zinc-900 tabular-nums">
                {metrics.totalCount}
              </span>
              <span className="text-xs text-zinc-500 font-medium">orders</span>
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-zinc-400 font-medium">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-zinc-400" />
              <span>Master catalog registry</span>
            </div>
          </div>

          {/* Approved POs */}
          <div className="relative overflow-hidden rounded-2xl border border-t-emerald-200 border-x-emerald-300 border-b-emerald-400 bg-gradient-to-b from-emerald-50/60 to-emerald-100/30 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-wider text-emerald-800">
                Approved POs
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-emerald-200/90 bg-emerald-100/80 text-emerald-700 shadow-2xs">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-emerald-800 tabular-nums">
                {metrics.approvedCount}
              </span>
              <span className="text-xs text-emerald-700 font-medium">
                {metrics.totalCount > 0 ? `${Math.round((metrics.approvedCount / metrics.totalCount) * 100)}% verified` : '0%'}
              </span>
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-emerald-700 font-medium">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]" />
              <span>Ready for invoice clearance</span>
            </div>
          </div>

          {/* Open / Pending Review */}
          <div className="relative overflow-hidden rounded-2xl border border-t-amber-200 border-x-amber-300 border-b-amber-400 bg-gradient-to-b from-amber-50/60 to-amber-100/30 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-wider text-amber-800">
                Open / Unsettled
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-amber-200/90 bg-amber-100/80 text-amber-700 shadow-2xs">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {metrics.openCount}
              </span>
              <span className="text-xs text-amber-700 font-medium">pending</span>
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-amber-700 font-medium">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-500" />
              <span>Awaiting fulfillment</span>
            </div>
          </div>

          {/* Total Approved Spend Commitment */}
          <div className="relative overflow-hidden rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50/80 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-500">
                Total Authorized Spend
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-zinc-200/80 bg-zinc-50 text-zinc-700 shadow-2xs">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-zinc-900 tabular-nums">
                {formatCurrency(metrics.totalSpend)}
              </span>
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-zinc-400 font-medium">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-zinc-400" />
              <span>Avg {formatCurrency(metrics.avgAmount)} / PO</span>
            </div>
          </div>
        </div>
      )}

      {/* Search & Filter Toolbar */}
      <div className="flex flex-col gap-3 rounded-2xl border border-zinc-200/90 bg-white p-4 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] sm:flex-row sm:items-center sm:justify-between">
        {/* Search Box */}
        <div className="relative flex-1 sm:max-w-md">
          <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5 text-zinc-400">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </div>

          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by PO number or vendor..."
            className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 py-2 pl-10 pr-9 text-xs text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10 font-sans"
          />

          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              title="Clear search"
              aria-label="Clear search"
              className="absolute inset-y-0 right-0 flex items-center pr-3 text-zinc-400 hover:text-zinc-600"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        {/* Filter Pills with Tactile Elevation */}
        <div className="flex flex-wrap items-center gap-1.5 overflow-x-auto pb-0.5 sm:pb-0">
          {(
            [
              { key: 'all', label: 'All Orders' },
              { key: 'approved', label: 'Approved' },
              { key: 'open', label: 'Open' },
            ] as const
          ).map((item) => {
            const count = statusCounts[item.key];
            const isActive = statusFilter === item.key;

            return (
              <button
                key={item.key}
                type="button"
                onClick={() => setStatusFilter(item.key)}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-all active:translate-y-[0.5px] ${
                  isActive
                    ? 'border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 to-zinc-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_1px_2px_rgba(0,0,0,0.15)]'
                    : 'border-t border-t-white border-x border-x-zinc-200 border-b border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-700 hover:bg-zinc-100/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)]'
                }`}
              >
                <span>{item.label}</span>
                <span
                  className={`rounded-full px-1.5 py-0.2 font-mono text-[10px] font-bold ${
                    isActive
                      ? 'bg-zinc-800 text-zinc-200 border border-zinc-700'
                      : 'bg-zinc-100 text-zinc-500 border border-zinc-200'
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
              className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-zinc-500 hover:text-zinc-900 transition-colors"
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

      {/* Main Ledger Content States */}
      {isLoading ? (
        <div className="overflow-hidden rounded-2xl border border-zinc-200/90 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)]">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs font-mono">
              <thead className="border-b border-zinc-200 bg-zinc-50 text-zinc-700">
                <tr>
                  <th className="p-3.5">PO Number</th>
                  <th className="p-3.5">Vendor Name</th>
                  <th className="p-3.5 text-right">Approved Amount</th>
                  <th className="p-3.5">Status</th>
                  <th className="p-3.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200">
                {[...Array(5)].map((_, i) => (
                  <tr key={i} className="animate-pulse">
                    <td className="p-3.5">
                      <div className="h-4 w-28 rounded bg-zinc-200" />
                    </td>
                    <td className="p-3.5">
                      <div className="h-4 w-36 rounded bg-zinc-200" />
                    </td>
                    <td className="p-3.5 text-right">
                      <div className="ml-auto h-4 w-20 rounded bg-zinc-200" />
                    </td>
                    <td className="p-3.5">
                      <div className="h-5 w-24 rounded-full bg-zinc-200" />
                    </td>
                    <td className="p-3.5 text-right">
                      <div className="ml-auto h-6 w-16 rounded bg-zinc-200" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : error ? (
        <div className="rounded-2xl border border-red-200 bg-red-50/90 p-6 text-red-900 shadow-2xs space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-red-100 text-red-700 border border-red-200">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div>
                <p className="font-bold text-sm">Failed to connect to Purchase Orders registry</p>
                <p className="text-xs text-red-700 mt-0.5">{error}</p>
              </div>
            </div>
            <button
              onClick={() => fetchPurchaseOrders(false)}
              className="rounded-xl border border-red-300 bg-white px-3.5 py-1.5 text-xs font-semibold text-red-800 shadow-xs hover:bg-red-50 active:translate-y-[0.5px] transition-all"
            >
              Retry Connection
            </button>
          </div>
        </div>
      ) : purchaseOrders.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-200/90 bg-zinc-50/40 p-12 text-center shadow-2xs">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-zinc-200 bg-white text-zinc-400 mb-3 shadow-2xs">
            <svg className="h-7 w-7 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <h3 className="text-base font-bold text-zinc-900">No Purchase Orders Recorded</h3>
          <p className="mt-1 text-xs text-zinc-500 max-w-sm">
            Purchase orders will populate automatically when synced with your enterprise ERP database or created during procurement workflows.
          </p>
        </div>
      ) : filteredPOs.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-zinc-200/90 bg-white p-12 text-center shadow-[0_1px_3px_rgba(0,0,0,0.02)]">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-zinc-200 bg-zinc-50 text-zinc-400 mb-3 shadow-2xs">
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </div>
          <h3 className="text-sm font-bold text-zinc-900">No Matching Purchase Orders</h3>
          <p className="mt-1 text-xs text-zinc-500">
            No purchase orders matched your current search and filter settings.
          </p>
          <div className="mt-4">
            <button
              onClick={resetFilters}
              className="inline-flex items-center gap-1.5 rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-3.5 py-2 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)] hover:bg-zinc-100 active:translate-y-[0.5px]"
            >
              Clear Search Filters
            </button>
          </div>
        </div>
      ) : (
        /* Tactile PO Ledger Table */
        <div className="overflow-hidden rounded-2xl border border-zinc-200/90 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)]">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-zinc-200/80 bg-zinc-50/75 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                <tr>
                  <th scope="col" className="px-5 py-3.5">
                    PO Identifier
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Vendor Partner
                  </th>
                  <th scope="col" className="px-5 py-3.5 text-right">
                    Approved Amount
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    Approval State
                  </th>
                  <th scope="col" className="px-5 py-3.5 text-right">
                    Inspection
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/70 bg-white font-sans">
                {filteredPOs.map((po, idx) => {
                  const isCopied = copiedPoNumber === po.po_number;
                  const isEven = idx % 2 === 0;

                  return (
                    <tr
                      key={po.po_number}
                      onClick={() => setSelectedPO(po)}
                      className={`group cursor-pointer transition-colors duration-150 ${
                        isEven ? 'bg-white hover:bg-zinc-50/90' : 'bg-zinc-50/40 hover:bg-zinc-100/70'
                      }`}
                    >
                      {/* PO Number with click to copy */}
                      <td className="whitespace-nowrap px-5 py-3.5 font-mono">
                        <div className="flex items-center gap-2">
                          <span className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-2.5 py-1 text-xs font-semibold text-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] group-hover:border-zinc-300">
                            <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
                            {po.po_number}
                          </span>
                          <button
                            type="button"
                            onClick={(e) => handleCopyPO(po.po_number, e)}
                            title="Copy PO Number"
                            className="opacity-0 group-hover:opacity-100 rounded p-1 text-zinc-400 hover:bg-zinc-200/60 hover:text-zinc-700 transition-all active:translate-y-[0.5px]"
                          >
                            {isCopied ? (
                              <span className="text-[10px] font-bold text-emerald-600 font-sans">Copied!</span>
                            ) : (
                              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                              </svg>
                            )}
                          </button>
                        </div>
                      </td>

                      {/* Vendor */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <div className="flex items-center gap-2.5">
                          <div className="flex h-7 w-7 items-center justify-center rounded-lg border border-zinc-200 bg-zinc-50 font-mono text-[11px] font-bold text-zinc-700 shadow-2xs">
                            {po.vendor.slice(0, 2).toUpperCase()}
                          </div>
                          <span className="font-semibold text-zinc-900 text-sm">
                            {po.vendor}
                          </span>
                        </div>
                      </td>

                      {/* Approved Amount */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-right font-mono text-xs font-bold tabular-nums text-zinc-900">
                        {formatCurrency(po.approved_amount)}
                      </td>

                      {/* Status */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <StatusBadge status={po.status || 'approved'} />
                      </td>

                      {/* Action Trigger */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setSelectedPO(po);
                          }}
                          className="inline-flex items-center gap-1 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-2.5 py-1 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-50 hover:text-zinc-900 active:translate-y-[0.5px] transition-all"
                        >
                          <span>Inspect</span>
                          <svg className="h-3 w-3 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                          </svg>
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Table Footer */}
          <div className="flex flex-col gap-2 border-t border-zinc-200/80 bg-zinc-50/60 px-5 py-3.5 text-xs text-zinc-500 sm:flex-row sm:items-center sm:justify-between font-mono">
            <div>
              Showing <span className="font-bold text-zinc-800">{filteredPOs.length}</span> of{' '}
              <span className="font-bold text-zinc-800">{purchaseOrders.length}</span> purchase orders
              {hasActiveFilters && <span className="text-zinc-400 font-sans ml-1">(filtered)</span>}
            </div>

            {hasActiveFilters && (
              <button
                onClick={resetFilters}
                className="text-xs font-medium text-zinc-600 underline-offset-2 hover:underline font-sans"
              >
                Clear active filters
              </button>
            )}
          </div>
        </div>
      )}

      {/* PO Detail & Approval Workflow Modal */}
      {selectedPO && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/50 backdrop-blur-sm p-4"
          onClick={() => setSelectedPO(null)}
        >
          <div
            className="w-full max-w-xl rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white p-6 shadow-2xl space-y-5"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div className="flex items-start justify-between pb-4 border-b border-zinc-200/80">
              <div className="flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-b from-zinc-800 via-zinc-900 to-black text-white border-t border-t-zinc-600 border-x border-x-zinc-800 border-b border-b-black shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_2px_6px_rgba(0,0,0,0.3)]">
                  <svg className="h-5 w-5 text-zinc-200" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="text-lg font-bold text-zinc-900">
                      Purchase Order Specification
                    </h3>
                    <StatusBadge status={selectedPO.status || 'approved'} />
                  </div>
                  <p className="text-xs text-zinc-500 font-mono mt-0.5">
                    REF: {selectedPO.po_number}
                  </p>
                </div>
              </div>

              <button
                onClick={() => setSelectedPO(null)}
                className="rounded-lg bg-zinc-100 p-1.5 text-zinc-400 hover:bg-zinc-200 hover:text-zinc-800 transition-colors"
                title="Close"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Spec Details Card */}
            <div className="rounded-xl border border-zinc-200/90 bg-zinc-50/70 p-4 space-y-3 font-mono text-xs shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)]">
              <div className="flex justify-between items-center py-1 border-b border-zinc-200/60">
                <span className="text-zinc-500">Purchase Order Number:</span>
                <div className="flex items-center gap-1.5">
                  <span className="font-bold text-zinc-900">{selectedPO.po_number}</span>
                  <button
                    onClick={(e) => handleCopyPO(selectedPO.po_number, e)}
                    className="rounded border border-zinc-200 bg-white px-1.5 py-0.5 text-[10px] text-zinc-600 hover:bg-zinc-100 font-sans"
                  >
                    {copiedPoNumber === selectedPO.po_number ? 'Copied' : 'Copy'}
                  </button>
                </div>
              </div>

              <div className="flex justify-between items-center py-1 border-b border-zinc-200/60">
                <span className="text-zinc-500">Authorized Vendor:</span>
                <span className="font-bold text-zinc-900 font-sans text-sm">{selectedPO.vendor}</span>
              </div>

              <div className="flex justify-between items-center py-1 border-b border-zinc-200/60">
                <span className="text-zinc-500">Approved Spend Cap:</span>
                <span className="font-bold text-emerald-600 text-sm">
                  {formatCurrency(selectedPO.approved_amount)}
                </span>
              </div>

              <div className="flex justify-between items-center py-1 border-b border-zinc-200/60">
                <span className="text-zinc-500">Ledger Verification:</span>
                <span className="inline-flex items-center gap-1 font-semibold text-emerald-700">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                  3-Way Match Enabled
                </span>
              </div>

              <div className="flex justify-between items-center py-1">
                <span className="text-zinc-500">Autonomous Settlement:</span>
                <span className="text-zinc-700 font-medium">Eligible for Auto-Approval</span>
              </div>
            </div>

            {/* Workflow & Match Guide */}
            <div className="rounded-xl border border-sky-200/80 bg-sky-50/60 p-3.5 text-xs text-sky-900 shadow-2xs flex items-start gap-2.5">
              <svg className="h-4 w-4 text-sky-600 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <div className="text-[11px] leading-relaxed text-sky-800">
                When recording an invoice matching this PO, the system checks whether the billed total is strictly within the approved cap of{' '}
                <span className="font-bold font-mono">{formatCurrency(selectedPO.approved_amount)}</span>. Invoices matching both vendor and PO are automatically cleared.
              </div>
            </div>

            {/* Action Buttons */}
            <div className="flex items-center justify-end gap-2.5 pt-2 border-t border-zinc-100">
              <button
                type="button"
                onClick={() => setSelectedPO(null)}
                className="rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-4 py-2.5 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)] hover:bg-zinc-100 active:translate-y-[0.5px] transition-all"
              >
                Dismiss
              </button>

              <Link
                href={`/invoices/new?po_number=${encodeURIComponent(selectedPO.po_number)}&vendor=${encodeURIComponent(selectedPO.vendor)}`}
                className="inline-flex items-center gap-1.5 rounded-xl border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-4 py-2.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(0,0,0,0.25)] hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px] transition-all"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                </svg>
                <span>Create Linked Invoice</span>
              </Link>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
