'use client';

import { useEffect, useMemo, useState, useRef, useCallback } from 'react';
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
type SortField = 'date' | 'amount' | 'vendor' | 'id' | 'status';
type SortOrder = 'asc' | 'desc';

export default function InvoicesPage() {
  const [invoices, setInvoices] = useState<InvoiceDTO[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Search and filter states
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [sortBy, setSortBy] = useState<SortField>('date');
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc');

  // Pagination states
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  // Clipboard copy state
  const [copiedId, setCopiedId] = useState<string | null>(null);

  // Slide-over / Modal detail view
  const [selectedInvoice, setSelectedInvoice] = useState<InvoiceDTO | null>(null);
  const [isRawJsonExpanded, setIsRawJsonExpanded] = useState(false);

  // Search input ref for keyboard shortcuts
  const searchInputRef = useRef<HTMLInputElement>(null);

  const fetchInvoices = useCallback(async (isManualRefresh = false) => {
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
  }, []);

  useEffect(() => {
    fetchInvoices();
  }, [fetchInvoices]);

  // Global keyboard shortcuts: "/" or "Ctrl+K" / "Cmd+K" to search, "Escape" to dismiss modal / blur search
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Close modal on Escape
      if (e.key === 'Escape') {
        if (selectedInvoice) {
          setSelectedInvoice(null);
          return;
        }
        if (document.activeElement === searchInputRef.current) {
          searchInputRef.current?.blur();
        }
        return;
      }

      // Shortcut to focus search: "/" (when not inside an input) or "Ctrl/Cmd + K"
      const isInput =
        document.activeElement instanceof HTMLInputElement ||
        document.activeElement instanceof HTMLTextAreaElement ||
        document.activeElement instanceof HTMLSelectElement;

      if ((e.key === '/' && !isInput) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
        e.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedInvoice]);

  const handleCopyId = async (id: string, e?: React.MouseEvent) => {
    if (e) {
      e.stopPropagation();
    }
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

  // Counts per status for filter pills
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

  // Filtered and Sorted invoices
  const filteredInvoices = useMemo(() => {
    const list = invoices.filter((inv) => {
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

    // Sorting
    list.sort((a, b) => {
      let comparison = 0;
      switch (sortBy) {
        case 'amount':
          comparison = a.amount - b.amount;
          break;
        case 'vendor':
          comparison = a.vendor.localeCompare(b.vendor);
          break;
        case 'id':
          comparison = a.id.localeCompare(b.id);
          break;
        case 'status':
          comparison = a.status.localeCompare(b.status);
          break;
        case 'date':
        default:
          comparison = new Date(a.date).getTime() - new Date(b.date).getTime();
          break;
      }
      return sortOrder === 'asc' ? comparison : -comparison;
    });

    return list;
  }, [invoices, statusFilter, searchQuery, sortBy, sortOrder]);

  // Pagination calculation
  const totalPages = Math.max(1, Math.ceil(filteredInvoices.length / pageSize));
  const validCurrentPage = Math.min(currentPage, totalPages);

  const paginatedInvoices = useMemo(() => {
    const startIdx = (validCurrentPage - 1) * pageSize;
    return filteredInvoices.slice(startIdx, startIdx + pageSize);
  }, [filteredInvoices, validCurrentPage, pageSize]);

  const hasActiveFilters = searchQuery.trim() !== '' || statusFilter !== 'all';

  const resetFilters = () => {
    setSearchQuery('');
    setStatusFilter('all');
    setCurrentPage(1);
  };

  const handleSort = (field: SortField) => {
    if (sortBy === field) {
      setSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortBy(field);
      setSortOrder('desc');
    }
  };

  return (
    <div className="space-y-6 pb-12">
      {/* Top Header section */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900">
              Invoices
            </h1>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-0.5 font-mono text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)]">
              <span className="h-1.5 w-1.5 rounded-full bg-zinc-400" />
              {invoices.length} {invoices.length === 1 ? 'record' : 'records'}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            Manage, audit, and track accounts payable settlements and approvals with tactile ledger precision.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            onClick={() => fetchInvoices(true)}
            disabled={isRefreshing || isLoading}
            title="Refresh invoices"
            aria-label="Refresh invoices"
            className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-3 py-2 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] transition-all hover:bg-zinc-100/80 active:translate-y-[0.5px] disabled:opacity-50"
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
            <span>{isRefreshing ? 'Syncing...' : 'Sync'}</span>
          </button>

          <Link
            href="/invoices/new"
            className="inline-flex items-center justify-center gap-2 rounded-lg border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-4 py-2 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(0,0,0,0.25)] transition-all hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.35)]"
          >
            <svg
              className="h-3.5 w-3.5"
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
              className="h-28 animate-pulse rounded-2xl border border-zinc-200/90 bg-white/60 p-5 shadow-[0_1px_3px_rgba(0,0,0,0.02)]"
            >
              <div className="flex items-center justify-between">
                <div className="h-4 w-24 rounded bg-zinc-200" />
                <div className="h-8 w-8 rounded-xl bg-zinc-200" />
              </div>
              <div className="mt-3 h-7 w-20 rounded bg-zinc-200" />
              <div className="mt-2 h-3 w-32 rounded bg-zinc-100" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {/* Total Invoices */}
          <div className="relative overflow-hidden rounded-2xl border border-t-amber-100 border-x-amber-200/70 border-b-amber-300/80 bg-gradient-to-b from-amber-50/40 to-white p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_4px_12px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold uppercase tracking-wider text-amber-800">
                Total Invoices
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400/80 bg-amber-50 text-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)]">
                <svg className="h-4 w-4 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {metrics.totalInvoices}
              </span>
              <span className="text-xs font-medium text-amber-600">records</span>
            </div>
            <p className="mt-1 text-[11px] text-amber-700/80">
              Total recorded in AP ledger
            </p>
          </div>

          {/* Total Value */}
          <div className="relative overflow-hidden rounded-2xl border border-t-emerald-100 border-x-emerald-200/70 border-b-emerald-300/80 bg-gradient-to-b from-emerald-50/40 to-white p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_4px_12px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold uppercase tracking-wider text-emerald-800">
                Total Ledger Value
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-t-emerald-200 border-x-emerald-300/80 border-b-emerald-400/80 bg-emerald-50 text-emerald-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)]">
                <svg className="h-4 w-4 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-emerald-800 tabular-nums">
                {formatCurrency(metrics.totalValue)}
              </span>
            </div>
            <p className="mt-1 text-[11px] text-emerald-700/80">
              Cumulative dollar commitment
            </p>
          </div>

          {/* Pending Invoices */}
          <div className="relative overflow-hidden rounded-2xl border border-t-amber-100 border-x-amber-200/70 border-b-amber-300/80 bg-gradient-to-b from-amber-50/40 to-white p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_4px_12px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold uppercase tracking-wider text-amber-800">
                Pending Review
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400/80 bg-amber-50 text-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)]">
                <svg className="h-4 w-4 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {metrics.pendingCount}
              </span>
              <span className="text-xs font-medium text-amber-600">unsettled</span>
            </div>
            <p className="mt-1 text-[11px] text-amber-700/80">
              Awaiting verification & match
            </p>
          </div>

          {/* Flagged / Failed */}
          <div className="relative overflow-hidden rounded-2xl border border-t-amber-100 border-x-amber-200/70 border-b-amber-300/80 bg-gradient-to-b from-amber-50/40 to-white p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02),0_4px_12px_rgba(0,0,0,0.02)] transition-all">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold uppercase tracking-wider text-amber-800">
                Discrepancies
              </span>
              <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400/80 bg-amber-50 text-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)]">
                <svg className="h-4 w-4 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {metrics.flaggedOrFailedCount}
              </span>
              <span className="text-xs font-medium text-amber-600">flagged/failed</span>
            </div>
            <p className="mt-1 text-[11px] text-amber-700/80">
              Exceptions requiring manual action
            </p>
          </div>
        </div>
      )}

      {/* Tactile Search & Filter Control Bar */}
      <div className="rounded-2xl border border-zinc-200/90 bg-white p-4 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] space-y-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          {/* Tactile Surface-Inset Search Box */}
          <div className="relative flex-1">
            <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5 text-zinc-400">
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </div>

            <input
              ref={searchInputRef}
              type="text"
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                setCurrentPage(1);
              }}
              placeholder="Search by vendor, PO number, or invoice ID..."
              className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 py-2.5 pl-10 pr-20 text-sm text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
            />

            <div className="absolute inset-y-0 right-0 flex items-center pr-3 gap-1.5">
              {searchQuery ? (
                <button
                  type="button"
                  onClick={() => {
                    setSearchQuery('');
                    setCurrentPage(1);
                  }}
                  title="Clear search"
                  aria-label="Clear search"
                  className="rounded-md p-1 text-zinc-400 hover:bg-zinc-200/60 hover:text-zinc-700 transition-colors"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              ) : (
                <kbd className="hidden sm:inline-flex items-center gap-0.5 rounded border border-zinc-200 bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px] font-medium text-zinc-500 shadow-2xs">
                  <span className="text-[9px]">/</span> or <span className="text-[9px]">⌘K</span>
                </kbd>
              )}
            </div>
          </div>

          {/* Sort & Order Controls */}
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1.5 rounded-xl border border-zinc-200/80 bg-zinc-50/70 p-1 shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)]">
              <span className="pl-2 text-[11px] font-bold uppercase tracking-wider text-zinc-400">Sort:</span>
              <select
                value={sortBy}
                onChange={(e) => {
                  setSortBy(e.target.value as SortField);
                  setCurrentPage(1);
                }}
                className="rounded-lg border-0 bg-transparent py-1 pl-1 pr-6 text-xs font-semibold text-zinc-700 focus:outline-none focus:ring-0 cursor-pointer"
              >
                <option value="date">Date</option>
                <option value="amount">Amount</option>
                <option value="vendor">Vendor</option>
                <option value="id">Invoice ID</option>
                <option value="status">Status</option>
              </select>

              <button
                type="button"
                onClick={() => setSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'))}
                title={`Sort ${sortOrder === 'asc' ? 'Ascending' : 'Descending'}`}
                aria-label={`Sort ${sortOrder === 'asc' ? 'Ascending' : 'Descending'}`}
                className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white text-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-50 active:translate-y-[0.5px]"
              >
                {sortOrder === 'asc' ? (
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3 4h13M3 8h9m-9 4h6m4 0l4-4m0 0l4 4m-4-4v12" />
                  </svg>
                ) : (
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3 4h13M3 8h9m-9 4h9m5-4v12m0 0l-4-4m4 4l4-4" />
                  </svg>
                )}
              </button>
            </div>
          </div>
        </div>

        {/* Filter Pills */}
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-zinc-100">
          <div className="flex flex-wrap items-center gap-1.5">
            {(
              [
                { key: 'all', label: 'All Invoices' },
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
                  type="button"
                  onClick={() => {
                    setStatusFilter(item.key);
                    setCurrentPage(1);
                  }}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-semibold transition-all active:translate-y-[0.5px] ${
                    isActive
                      ? 'border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-zinc-900 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_3px_rgba(0,0,0,0.2)]'
                      : 'border-t border-t-white border-x border-x-zinc-200 border-b border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-700 hover:bg-zinc-100/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)]'
                  }`}
                >
                  <span>{item.label}</span>
                  <span
                    className={`rounded-full px-1.5 py-0.2 font-mono text-[10px] ${
                      isActive
                        ? 'bg-zinc-800 text-zinc-200'
                        : 'bg-zinc-100 text-zinc-600 border border-zinc-200/60'
                    }`}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>

          {hasActiveFilters && (
            <button
              type="button"
              onClick={resetFilters}
              className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs font-semibold text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100 transition-colors"
              title="Reset all search and filters"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
              <span>Reset Filters</span>
            </button>
          )}
        </div>
      </div>

      {/* Main Table Content Area */}
      {isLoading ? (
        /* Skeleton Table Loading State */
        <div className="overflow-hidden rounded-2xl border border-zinc-200/90 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)]">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200/90 bg-zinc-50/80 text-[11px] font-bold uppercase tracking-wider text-zinc-600">
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
              <tbody className="divide-y divide-zinc-200/80">
                {[...Array(5)].map((_, i) => (
                  <tr key={i} className="animate-pulse">
                    <td className="px-5 py-4">
                      <div className="h-4 w-20 rounded bg-zinc-200" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-4 w-32 rounded bg-zinc-200" />
                    </td>
                    <td className="px-5 py-4 text-right">
                      <div className="ml-auto h-4 w-24 rounded bg-zinc-200" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-4 w-20 rounded bg-zinc-200" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-4 w-16 rounded bg-zinc-200" />
                    </td>
                    <td className="px-5 py-4">
                      <div className="h-5 w-20 rounded-full bg-zinc-200" />
                    </td>
                    <td className="px-5 py-4 text-right">
                      <div className="ml-auto h-4 w-12 rounded bg-zinc-200" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : error ? (
        <div className="rounded-2xl border border-rose-200 bg-rose-50/90 p-6 text-rose-900 shadow-2xs">
          <div className="flex items-center justify-between">
            <div className="flex items-start gap-3">
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-rose-100 text-rose-600 border border-rose-200">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div>
                <p className="text-sm font-semibold">Failed to load invoices</p>
                <p className="mt-0.5 text-xs text-rose-700">{error}</p>
              </div>
            </div>
            <button
              onClick={() => fetchInvoices(false)}
              className="rounded-lg border border-t-white border-x-rose-200 border-b-rose-300 bg-white px-3.5 py-1.5 text-xs font-semibold text-rose-800 shadow-xs hover:bg-rose-50 active:translate-y-[0.5px]"
            >
              Retry Connection
            </button>
          </div>
        </div>
      ) : invoices.length === 0 ? (
        /* Empty State: 0 invoices in system */
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white p-12 text-center shadow-[0_1px_3px_rgba(0,0,0,0.02)]">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-zinc-50 text-zinc-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_2px_4px_rgba(0,0,0,0.04)] mb-3">
            <svg className="h-7 w-7 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
          </div>
          <h3 className="text-base font-bold text-zinc-900">
            No invoices recorded yet
          </h3>
          <p className="mt-1 max-w-sm text-xs leading-relaxed text-zinc-500">
            Your ledger is currently empty. Record your first vendor invoice to begin tracking payments and matching purchase orders.
          </p>
          <div className="mt-5">
            <Link
              href="/invoices/new"
              className="inline-flex items-center gap-2 rounded-lg border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-4 py-2 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_4px_rgba(0,0,0,0.2)] transition-all hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px]"
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
        <div className="flex flex-col items-center justify-center rounded-2xl border border-zinc-200/90 bg-white p-12 text-center shadow-[0_1px_3px_rgba(0,0,0,0.02)]">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-zinc-50 text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)] mb-3">
            <svg className="h-6 w-6 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
              />
            </svg>
          </div>
          <h3 className="text-sm font-bold text-zinc-900">
            No matching invoices
          </h3>
          <p className="mt-1 text-xs text-zinc-500">
            No invoices matched your current search query and filter criteria.
          </p>
          <div className="mt-4">
            <button
              type="button"
              onClick={resetFilters}
              className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-3.5 py-1.5 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px]"
            >
              Reset Filters
            </button>
          </div>
        </div>
      ) : (
        /* Elevated Modern Data Table with Split Borders */
        <div className="overflow-hidden rounded-2xl border border-zinc-200/90 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)]">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200/90 bg-zinc-50/80 text-[11px] font-bold uppercase tracking-wider text-zinc-600 select-none">
                <tr>
                  <th
                    scope="col"
                    onClick={() => handleSort('id')}
                    className="px-5 py-3.5 cursor-pointer hover:text-zinc-900 transition-colors"
                  >
                    <div className="flex items-center gap-1">
                      <span>Invoice ID</span>
                      {sortBy === 'id' && (
                        <span className="text-zinc-800 font-bold">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                      )}
                    </div>
                  </th>
                  <th
                    scope="col"
                    onClick={() => handleSort('vendor')}
                    className="px-5 py-3.5 cursor-pointer hover:text-zinc-900 transition-colors"
                  >
                    <div className="flex items-center gap-1">
                      <span>Vendor</span>
                      {sortBy === 'vendor' && (
                        <span className="text-zinc-800 font-bold">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                      )}
                    </div>
                  </th>
                  <th
                    scope="col"
                    onClick={() => handleSort('amount')}
                    className="px-5 py-3.5 text-right cursor-pointer hover:text-zinc-900 transition-colors"
                  >
                    <div className="flex items-center justify-end gap-1">
                      <span>Amount</span>
                      {sortBy === 'amount' && (
                        <span className="text-zinc-800 font-bold">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                      )}
                    </div>
                  </th>
                  <th
                    scope="col"
                    onClick={() => handleSort('date')}
                    className="px-5 py-3.5 cursor-pointer hover:text-zinc-900 transition-colors"
                  >
                    <div className="flex items-center gap-1">
                      <span>Date</span>
                      {sortBy === 'date' && (
                        <span className="text-zinc-800 font-bold">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                      )}
                    </div>
                  </th>
                  <th scope="col" className="px-5 py-3.5">
                    PO Number
                  </th>
                  <th
                    scope="col"
                    onClick={() => handleSort('status')}
                    className="px-5 py-3.5 cursor-pointer hover:text-zinc-900 transition-colors"
                  >
                    <div className="flex items-center gap-1">
                      <span>Status</span>
                      {sortBy === 'status' && (
                        <span className="text-zinc-800 font-bold">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                      )}
                    </div>
                  </th>
                  <th scope="col" className="px-5 py-3.5 text-right">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/80">
                {paginatedInvoices.map((inv) => {
                  const isCopied = copiedId === inv.id;
                  const displayId = inv.id.length > 8 ? `${inv.id.slice(0, 8)}...` : inv.id;

                  return (
                    <tr
                      key={inv.id}
                      onClick={() => setSelectedInvoice(inv)}
                      className="group cursor-pointer transition-colors duration-150 hover:bg-zinc-50/90"
                    >
                      {/* ID with click-to-copy */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <button
                          type="button"
                          onClick={(e) => handleCopyId(inv.id, e)}
                          className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 font-mono text-xs text-zinc-600 transition-colors hover:bg-zinc-200/70 hover:text-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-400"
                          title={`Click to copy full ID: ${inv.id}`}
                        >
                          <span className="font-medium text-zinc-800">{displayId}</span>
                        </button>
                      </td>

                      {/* Vendor */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <div className="flex items-center gap-2">
                          <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-zinc-200 bg-zinc-100 text-[10px] font-bold text-zinc-600">
                            {inv.vendor.slice(0, 2).toUpperCase()}
                          </div>
                          <span className="font-semibold text-zinc-900">
                            {inv.vendor}
                          </span>
                        </div>
                      </td>

                      {/* Amount with tabular numbers */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        <span className="font-mono text-xs font-bold tabular-nums text-zinc-900">
                          {formatCurrency(inv.amount)}
                        </span>
                      </td>

                      {/* Date */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-xs font-medium text-zinc-600">
                        {inv.date}
                      </td>

                      {/* PO Number */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-xs font-mono">
                        {inv.po_number ? (
                          <span className="inline-flex items-center rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2 py-0.5 text-[11px] font-semibold text-zinc-700 shadow-2xs">
                            {inv.po_number}
                          </span>
                        ) : (
                          <span className="text-zinc-400">—</span>
                        )}
                      </td>

                      {/* Tactile Status Badge */}
                      <td className="whitespace-nowrap px-5 py-3.5">
                        <StatusBadge status={inv.status} />
                      </td>

                      {/* Action buttons */}
                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        <div className="flex items-center justify-end gap-1.5" onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            onClick={() => setSelectedInvoice(inv)}
                            className="inline-flex items-center gap-1 rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2 py-1 text-[11px] font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)] hover:bg-zinc-100 active:translate-y-[0.5px]"
                          >
                            <span>Inspect</span>
                          </button>

                          <button
                            type="button"
                            onClick={(e) => handleCopyId(inv.id, e)}
                            aria-label={`Copy invoice ID ${inv.id}`}
                            className={`inline-flex h-6 w-6 items-center justify-center rounded-md border transition-all active:translate-y-[0.5px] ${
                              isCopied
                                ? 'border-emerald-300 bg-emerald-50 text-emerald-600 shadow-xs'
                                : 'border-t-white border-x-zinc-200 border-b-zinc-300 bg-white text-zinc-400 hover:text-zinc-700 hover:bg-zinc-50 shadow-2xs'
                            }`}
                            title={isCopied ? 'Copied to clipboard!' : 'Copy full UUID'}
                          >
                            {isCopied ? (
                              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                              </svg>
                            ) : (
                              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                              </svg>
                            )}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Table Footer with Tactile Pagination Controls */}
          <div className="flex flex-col gap-3 border-t border-zinc-200/90 bg-zinc-50/70 px-5 py-3.5 text-xs text-zinc-600 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-3">
              <span>
                Showing <span className="font-bold text-zinc-900">{Math.min(filteredInvoices.length, (validCurrentPage - 1) * pageSize + 1)}</span>–
                <span className="font-bold text-zinc-900">{Math.min(filteredInvoices.length, validCurrentPage * pageSize)}</span> of{' '}
                <span className="font-bold text-zinc-900">{filteredInvoices.length}</span> {filteredInvoices.length === 1 ? 'entry' : 'entries'}
                {hasActiveFilters && ` (filtered from ${invoices.length})`}
              </span>

              {/* Rows per page selector */}
              <div className="hidden sm:flex items-center gap-1.5 pl-2 border-l border-zinc-200">
                <span className="text-[11px] text-zinc-400">Rows:</span>
                <select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value));
                    setCurrentPage(1);
                  }}
                  className="rounded-md border border-zinc-200 bg-white py-0.5 pl-1.5 pr-5 text-xs font-semibold text-zinc-700 shadow-2xs focus:outline-none cursor-pointer"
                >
                  <option value={10}>10</option>
                  <option value={25}>25</option>
                  <option value={50}>50</option>
                </select>
              </div>
            </div>

            {/* Pagination Button Group */}
            <div className="flex items-center gap-1.5 self-end sm:self-auto">
              <button
                type="button"
                onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                disabled={validCurrentPage <= 1}
                className="inline-flex items-center gap-1 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-1 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)] hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-40 disabled:pointer-events-none"
              >
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
                </svg>
                <span>Prev</span>
              </button>

              {/* Page Number Chips */}
              <div className="flex items-center gap-1">
                {[...Array(totalPages)].map((_, idx) => {
                  const pageNum = idx + 1;
                  // Show limited pages if many
                  if (
                    totalPages > 5 &&
                    Math.abs(pageNum - validCurrentPage) > 1 &&
                    pageNum !== 1 &&
                    pageNum !== totalPages
                  ) {
                    if (pageNum === 2 || pageNum === totalPages - 1) {
                      return <span key={pageNum} className="px-1 text-zinc-400">...</span>;
                    }
                    return null;
                  }

                  const isActive = pageNum === validCurrentPage;
                  return (
                    <button
                      key={pageNum}
                      type="button"
                      onClick={() => setCurrentPage(pageNum)}
                      className={`h-7 w-7 rounded-lg text-xs font-bold transition-all active:translate-y-[0.5px] ${
                        isActive
                          ? 'border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-zinc-900 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_2px_rgba(0,0,0,0.2)]'
                          : 'border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white text-zinc-700 hover:bg-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)]'
                      }`}
                    >
                      {pageNum}
                    </button>
                  );
                })}
              </div>

              <button
                type="button"
                onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                disabled={validCurrentPage >= totalPages}
                className="inline-flex items-center gap-1 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-1 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)] hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-40 disabled:pointer-events-none"
              >
                <span>Next</span>
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Slide-over / Modal Detail Drawer with Frosted Glass Backdrop */}
      {selectedInvoice && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-end bg-zinc-950/40 backdrop-blur-sm transition-opacity p-0 sm:p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => setSelectedInvoice(null)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="relative flex h-full sm:h-auto sm:max-h-[90vh] w-full max-w-xl flex-col rounded-none sm:rounded-2xl border border-zinc-200/90 bg-white shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150"
          >
            {/* Modal Header */}
            <div className="flex items-center justify-between border-b border-zinc-200 bg-zinc-50/90 px-6 py-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-b from-zinc-800 via-zinc-900 to-black text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_2px_6px_rgba(0,0,0,0.3)] border-t border-t-zinc-600 border-x border-x-zinc-800 border-b border-b-black">
                  <svg className="h-4 w-4 text-zinc-100" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-sm font-bold text-zinc-900">
                      Invoice Specification
                    </h2>
                    <StatusBadge status={selectedInvoice.status} />
                  </div>
                  <p className="font-mono text-xs text-zinc-500 mt-0.5">
                    ID: {selectedInvoice.id}
                  </p>
                </div>
              </div>

              <button
                type="button"
                onClick={() => setSelectedInvoice(null)}
                aria-label="Close invoice specification"
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white text-zinc-400 hover:text-zinc-700 hover:bg-zinc-50 shadow-2xs transition-colors active:translate-y-[0.5px]"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Modal Body */}
            <div className="flex-1 overflow-y-auto p-6 space-y-5">
              {/* Financial Key Highlights Card */}
              <div className="rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02)]">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold uppercase tracking-wider text-zinc-400">
                    Net Settlement Payable
                  </span>
                  <span className="font-mono text-xs text-zinc-500 font-semibold">USD</span>
                </div>
                <div className="mt-2 flex items-baseline justify-between">
                  <span className="font-mono text-3xl font-black tracking-tight text-zinc-900 tabular-nums">
                    {formatCurrency(selectedInvoice.amount)}
                  </span>
                  <span className="text-xs font-medium text-zinc-500">
                    Billing Date: <span className="font-semibold text-zinc-800">{selectedInvoice.date}</span>
                  </span>
                </div>
              </div>

              {/* Vendor & PO Association Details */}
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className="rounded-xl border border-zinc-200 bg-zinc-50/60 p-4 shadow-2xs">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-400">
                    Vendor Entity
                  </span>
                  <div className="mt-2 flex items-center gap-2">
                    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-zinc-200 bg-white text-xs font-bold text-zinc-700">
                      {selectedInvoice.vendor.slice(0, 2).toUpperCase()}
                    </div>
                    <div>
                      <p className="text-sm font-bold text-zinc-900">{selectedInvoice.vendor}</p>
                      <p className="text-[11px] text-zinc-500">Authorized Vendor Account</p>
                    </div>
                  </div>
                </div>

                <div className="rounded-xl border border-zinc-200 bg-zinc-50/60 p-4 shadow-2xs">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-400">
                    PO 3-Way Match
                  </span>
                  <div className="mt-2">
                    {selectedInvoice.po_number ? (
                      <div className="flex items-center gap-2">
                        <span className="inline-flex items-center rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-2 py-0.5 font-mono text-xs font-bold text-zinc-800 shadow-2xs">
                          {selectedInvoice.po_number}
                        </span>
                        <span className="text-xs font-semibold text-emerald-700">Linked to PO</span>
                      </div>
                    ) : (
                      <div className="flex items-center gap-1.5 text-xs text-amber-700">
                        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                        </svg>
                        <span>No Purchase Order Associated</span>
                      </div>
                    )}
                    <p className="text-[11px] text-zinc-400 mt-1">Status: {selectedInvoice.status.toUpperCase()}</p>
                  </div>
                </div>
              </div>

              {/* Technical Ledger Metadata */}
              <div className="rounded-xl border border-zinc-200 bg-zinc-50/50 p-4 space-y-2.5 shadow-2xs">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-zinc-500 font-medium">Record Created:</span>
                  <span className="font-mono text-zinc-700">
                    {selectedInvoice.created_at ? new Date(selectedInvoice.created_at).toLocaleString() : selectedInvoice.date}
                  </span>
                </div>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-zinc-500 font-medium">System UUID:</span>
                  <div className="flex items-center gap-1.5 font-mono text-zinc-700">
                    <span>{selectedInvoice.id}</span>
                    <button
                      type="button"
                      onClick={() => handleCopyId(selectedInvoice.id)}
                      className="rounded p-1 text-zinc-400 hover:text-zinc-700 hover:bg-zinc-200/50"
                      title="Copy full UUID"
                    >
                      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>

              {/* Collapsible JSON Viewer */}
              <div className="rounded-xl border border-zinc-200 overflow-hidden shadow-2xs">
                <button
                  type="button"
                  onClick={() => setIsRawJsonExpanded((prev) => !prev)}
                  className="flex w-full items-center justify-between bg-zinc-50 px-4 py-2.5 text-xs font-semibold text-zinc-700 hover:bg-zinc-100 transition-colors"
                >
                  <span className="flex items-center gap-2">
                    <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                    </svg>
                    Raw Ledger DTO Payload
                  </span>
                  <span>{isRawJsonExpanded ? '▲ Hide' : '▼ Expand'}</span>
                </button>
                {isRawJsonExpanded && (
                  <pre className="overflow-x-auto bg-zinc-950 p-4 font-mono text-[11px] text-emerald-400 leading-relaxed max-h-44">
                    {JSON.stringify(selectedInvoice, null, 2)}
                  </pre>
                )}
              </div>
            </div>

            {/* Modal Footer with Tactile Action Buttons */}
            <div className="flex items-center justify-between border-t border-zinc-200 bg-zinc-50/90 px-6 py-3.5">
              <button
                type="button"
                onClick={() => handleCopyId(selectedInvoice.id)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-3 py-1.5 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-50 active:translate-y-[0.5px]"
              >
                {copiedId === selectedInvoice.id ? (
                  <>
                    <svg className="h-3.5 w-3.5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    <span className="text-emerald-700">Copied!</span>
                  </>
                ) : (
                  <>
                    <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    <span>Copy UUID</span>
                  </>
                )}
              </button>

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setSelectedInvoice(null)}
                  className="rounded-lg border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-4 py-1.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_4px_rgba(0,0,0,0.2)] hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px]"
                >
                  Done
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
