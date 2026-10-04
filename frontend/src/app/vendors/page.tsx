'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import type { InvoiceDTO, InvoiceStatus, PurchaseOrderDTO, VendorDTO } from '@/lib/types';

type ActivityFilter = 'all' | 'invoices' | 'open' | 'idle';

const UNSETTLED_STATUSES: readonly InvoiceStatus[] = ['pending', 'approved', 'flagged'];

const FILTERS: readonly { key: ActivityFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'invoices', label: 'With invoices' },
  { key: 'open', label: 'Open balance' },
  { key: 'idle', label: 'No activity' },
];

const STATUS_LABELS: Record<InvoiceStatus, string> = {
  pending: 'Pending',
  approved: 'Approved',
  flagged: 'Flagged',
  completed: 'Completed',
  rejected: 'Rejected',
  skipped: 'Skipped',
};

const STATUS_ORDER: readonly InvoiceStatus[] = [
  'pending',
  'flagged',
  'approved',
  'completed',
  'rejected',
  'skipped',
];

const STATUS_TONE: Record<InvoiceStatus, string> = {
  pending: 'border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-50 to-amber-100/70 text-amber-800',
  approved: 'border-t-sky-200 border-x-sky-300/80 border-b-sky-400 bg-gradient-to-b from-sky-50 to-sky-100/70 text-sky-800',
  flagged: 'border-t-orange-200 border-x-orange-300/80 border-b-orange-400 bg-gradient-to-b from-orange-50 to-orange-100/70 text-orange-800',
  completed: 'border-t-emerald-200 border-x-emerald-300/80 border-b-emerald-400 bg-gradient-to-b from-emerald-50 to-emerald-100/70 text-emerald-800',
  rejected: 'border-t-rose-200 border-x-rose-300/80 border-b-rose-400 bg-gradient-to-b from-rose-50 to-rose-100/70 text-rose-800',
  skipped: 'border-t-zinc-200 border-x-zinc-300/80 border-b-zinc-400 bg-gradient-to-b from-white to-zinc-100/70 text-zinc-600',
};

interface StatusSlice {
  status: InvoiceStatus;
  count: number;
  total: number;
}

interface VendorProfile {
  id: string;
  name: string;
  invoiceCount: number;
  invoicedTotal: number;
  openCount: number;
  openTotal: number;
  statusSlices: StatusSlice[];
  poCount: number;
  poApprovedTotal: number;
  purchaseOrders: PurchaseOrderDTO[];
  lastInvoiceDate: string | null;
}

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

function normalizeVendorId(name: unknown): string {
  if (typeof name !== 'string') return '';
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
}

function matchesVendor(recordVendor: unknown, vendorId: string): boolean {
  if (typeof recordVendor !== 'string') return false;
  return normalizeVendorId(recordVendor) === vendorId;
}

function buildProfile(
  vendor: VendorDTO,
  invoices: InvoiceDTO[] | null,
  purchaseOrders: PurchaseOrderDTO[] | null
): VendorProfile {
  if (!invoices || !purchaseOrders) {
    return {
      id: vendor.id,
      name: vendor.name,
      invoiceCount: 0,
      invoicedTotal: 0,
      openCount: 0,
      openTotal: 0,
      statusSlices: [],
      poCount: 0,
      poApprovedTotal: 0,
      purchaseOrders: [],
      lastInvoiceDate: null,
    };
  }

  const vendorInvoices = invoices.filter((inv) => matchesVendor(inv.vendor, vendor.id));
  const vendorPOs = purchaseOrders.filter((po) => matchesVendor(po.vendor, vendor.id));

  const byStatus = new Map<InvoiceStatus, StatusSlice>();
  for (const inv of vendorInvoices) {
    const amount = inv.amount || 0;
    const slice = byStatus.get(inv.status) ?? { status: inv.status, count: 0, total: 0 };
    slice.count += 1;
    slice.total += amount;
    byStatus.set(inv.status, slice);
  }

  const unsettled = vendorInvoices.filter((inv) => UNSETTLED_STATUSES.includes(inv.status));
  const lastInvoiceDate = vendorInvoices.reduce<string | null>((latest, inv) => {
    if (!inv.date) return latest;
    if (!latest || inv.date > latest) return inv.date;
    return latest;
  }, null);

  return {
    id: vendor.id,
    name: vendor.name,
    invoiceCount: vendorInvoices.length,
    invoicedTotal: vendorInvoices.reduce((acc, inv) => acc + (inv.amount || 0), 0),
    openCount: unsettled.length,
    openTotal: unsettled.reduce((acc, inv) => acc + (inv.amount || 0), 0),
    statusSlices: STATUS_ORDER.map((status) => byStatus.get(status)).filter(
      (slice): slice is StatusSlice => slice !== undefined
    ),
    poCount: vendorPOs.length,
    poApprovedTotal: vendorPOs.reduce((acc, po) => acc + (po.approved_amount || 0), 0),
    purchaseOrders: vendorPOs,
    lastInvoiceDate,
  };
}

export default function VendorsPage() {
  const [vendors, setVendors] = useState<VendorDTO[]>([]);
  const [invoices, setInvoices] = useState<InvoiceDTO[] | null>(null);
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrderDTO[] | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ledgerError, setLedgerError] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState('');
  const [activityFilter, setActivityFilter] = useState<ActivityFilter>('all');
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [selectedVendorId, setSelectedVendorId] = useState<string | null>(null);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  const fetchAllData = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setIsLoading(true);
    }
    setError(null);
    setLedgerError(null);

    try {
      const resVendors = await fetch('/api/vendors', { cache: 'no-store' });
      if (!resVendors.ok) {
        throw new Error(`Failed to load vendors (${resVendors.status})`);
      }
      const dataVendors: unknown = await resVendors.json();
      if (!Array.isArray(dataVendors)) {
        throw new Error('Vendor response was not a list of vendors.');
      }
      const validVendors = dataVendors.filter(
        (entry): entry is VendorDTO =>
          typeof entry === 'object' &&
          entry !== null &&
          typeof (entry as VendorDTO).id === 'string' &&
          typeof (entry as VendorDTO).name === 'string'
      );
      setVendors(validVendors);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred');
      setInvoices(null);
      setPurchaseOrders(null);
      setIsLoading(false);
      setIsRefreshing(false);
      return;
    }

    // Ledger rollups are best-effort: they enrich the directory but must never
    // take it down. A failed rollup is reported instead of silently reading as
    // zero activity.
    const [invoicesRes, poRes] = await Promise.allSettled([
      fetch('/api/invoices', { cache: 'no-store' }),
      fetch('/api/purchase-orders', { cache: 'no-store' }),
    ]);

    const failures: string[] = [];
    if (invoicesRes.status === 'fulfilled' && invoicesRes.value.ok) {
      let parsed: unknown = null;
      try {
        parsed = await invoicesRes.value.json();
      } catch {
        parsed = null;
      }
      if (Array.isArray(parsed)) {
        setInvoices(parsed as InvoiceDTO[]);
      } else {
        setInvoices(null);
        failures.push('invoice ledger returned invalid data');
      }
    } else {
      setInvoices(null);
      failures.push(
        invoicesRes.status === 'rejected'
          ? 'invoice ledger unreachable'
          : `invoice ledger HTTP ${invoicesRes.value.status}`
      );
    }

    if (poRes.status === 'fulfilled' && poRes.value.ok) {
      let parsed: unknown = null;
      try {
        parsed = await poRes.value.json();
      } catch {
        parsed = null;
      }
      if (Array.isArray(parsed)) {
        setPurchaseOrders(parsed as PurchaseOrderDTO[]);
      } else {
        setPurchaseOrders(null);
        failures.push('purchase-order ledger returned invalid data');
      }
    } else {
      setPurchaseOrders(null);
      failures.push(
        poRes.status === 'rejected'
          ? 'purchase-order ledger unreachable'
          : `purchase-order ledger HTTP ${poRes.value.status}`
      );
    }

    setLedgerError(failures.length > 0 ? failures.join('; ') : null);
    setIsLoading(false);
    setIsRefreshing(false);
  }, []);

  useEffect(() => {
    fetchAllData();
  }, [fetchAllData]);

  // Move focus into the details dialog and hand it back on close.
  useEffect(() => {
    if (!selectedVendorId) {
      return;
    }
    restoreFocusRef.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    return () => restoreFocusRef.current?.focus();
  }, [selectedVendorId]);

  useEffect(() => {
    if (!selectedVendorId) {
      return;
    }
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setSelectedVendorId(null);
        return;
      }
      if (e.key === 'Tab') {
        const container = dialogRef.current;
        if (!container) return;
        const focusables = Array.from(
          container.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
          )
        );
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !container.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (active === last || !container.contains(active)) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedVendorId]);

  const ledgerAvailable = invoices !== null && purchaseOrders !== null;

  const profiles = useMemo(
    () => vendors.map((vendor) => buildProfile(vendor, invoices, purchaseOrders)),
    [vendors, invoices, purchaseOrders]
  );

  // Resolve the open dialog against live profiles so a refresh never leaves a
  // stale snapshot on screen; a vendor that no longer exists closes the dialog.
  const selectedVendor = useMemo(
    () =>
      selectedVendorId ? (profiles.find((p) => p.id === selectedVendorId) ?? null) : null,
    [profiles, selectedVendorId]
  );

  // Drop a stale selection once loading settles so a removed vendor cannot
  // reopen the dialog on a later refresh. Preserved while loading/refreshing.
  useEffect(() => {
    if (selectedVendorId !== null && selectedVendor === null && !isLoading && !isRefreshing) {
      setSelectedVendorId(null);
    }
  }, [selectedVendor, selectedVendorId, isLoading, isRefreshing]);

  const metrics = useMemo(() => {
    const openTotal = profiles.reduce((acc, profile) => acc + profile.openTotal, 0);
    const openCount = profiles.reduce((acc, profile) => acc + profile.openCount, 0);
    return {
      totalVendors: profiles.length,
      invoicedTotal: invoices ? invoices.reduce((acc, inv) => acc + (inv.amount || 0), 0) : 0,
      openTotal,
      openCount,
      poApprovedTotal: purchaseOrders
        ? purchaseOrders.reduce((acc, po) => acc + (po.approved_amount || 0), 0)
        : 0,
      activeVendors: profiles.filter(
        (profile) => profile.invoiceCount > 0 || profile.poCount > 0
      ).length,
    };
  }, [profiles, invoices, purchaseOrders]);

  const filterCounts = useMemo(() => {
    const counts: Record<ActivityFilter, number> = { all: profiles.length, invoices: 0, open: 0, idle: 0 };
    for (const profile of profiles) {
      if (profile.invoiceCount > 0) counts.invoices += 1;
      if (profile.openCount > 0) counts.open += 1;
      if (profile.invoiceCount === 0 && profile.poCount === 0) counts.idle += 1;
    }
    return counts;
  }, [profiles]);

  const filteredProfiles = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return profiles.filter((profile) => {
      if (activityFilter === 'invoices' && profile.invoiceCount === 0) return false;
      if (activityFilter === 'open' && profile.openCount === 0) return false;
      if (activityFilter === 'idle' && (profile.invoiceCount > 0 || profile.poCount > 0)) {
        return false;
      }
      if (query) {
        const nameMatch = profile.name.toLowerCase().includes(query);
        const idMatch = profile.id.toLowerCase().includes(query);
        if (!nameMatch && !idMatch) return false;
      }
      return true;
    });
  }, [profiles, activityFilter, searchQuery]);

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
      console.error('Failed to copy vendor ID to clipboard', err);
    }
  };

  const hasActiveFilters = searchQuery.trim() !== '' || activityFilter !== 'all';

  const resetFilters = () => {
    setSearchQuery('');
    setActivityFilter('all');
  };

  const openProfile = (profile: VendorProfile) => {
    setSelectedVendorId(profile.id);
  };

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900">Vendors</h1>
            <span className="inline-flex items-center rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2 py-0.5 font-mono text-xs font-medium text-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)]">
              {vendors.length} {vendors.length === 1 ? 'counterparty' : 'counterparties'}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            Vendor directory with recorded invoice and purchase-order exposure from the ledger.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() => fetchAllData(true)}
            disabled={isRefreshing || isLoading}
            title="Refresh vendors"
            aria-label="Refresh vendors"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-50"
          >
            <svg
              className={`h-4 w-4 ${isRefreshing ? 'animate-spin text-zinc-900 motion-reduce:animate-none' : ''}`}
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
            className="inline-flex items-center justify-center gap-2 rounded-lg border-t border-t-zinc-700/80 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 px-4 py-2 text-sm font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_4px_rgba(0,0,0,0.2)] transition-all hover:from-zinc-700 hover:to-zinc-800 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.35)]"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            <span>Create Invoice</span>
          </Link>
        </div>
      </div>

      {/* Ledger rollups are enrichment, not the directory itself — say so when they fail. */}
      {ledgerError && !isLoading && (
        <div
          role="status"
          className="flex flex-col gap-3 rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-50/80 to-white p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] sm:flex-row sm:items-center sm:justify-between"
        >
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-100 to-amber-200/70 text-amber-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.7)]">
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </span>
            <div>
              <p className="text-sm font-semibold text-amber-900">Ledger rollups unavailable</p>
              <p className="mt-0.5 text-xs text-amber-800">
                {ledgerError}. The vendor directory is accurate, but invoice and purchase-order
                totals are hidden rather than reported as zero.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => fetchAllData(false)}
            className="inline-flex shrink-0 items-center justify-center rounded-lg border border-t-white border-x-amber-300 border-b-amber-400 bg-gradient-to-b from-white to-amber-50 px-3.5 py-1.5 text-xs font-semibold text-amber-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-amber-100 active:translate-y-[0.5px]"
          >
            Retry ledger
          </button>
        </div>
      )}

      {/* Directory metrics */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <div
              key={i}
              className="h-28 animate-pulse rounded-xl border border-zinc-200/70 bg-white/70 p-5"
            >
              <div className="h-3.5 w-24 rounded bg-zinc-200" />
              <div className="mt-3 h-7 w-20 rounded bg-zinc-200" />
              <div className="mt-2 h-3 w-32 rounded bg-zinc-100" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-50/60 via-white to-amber-50/20 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.03)]">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-amber-800">
                Vendors
              </span>
              <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-100 to-amber-200/60 text-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
                </svg>
              </span>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {metrics.totalVendors}
              </span>
              <span className="text-xs text-zinc-500">registered</span>
            </div>
            <p className="mt-1 text-xs text-zinc-400">
              {ledgerAvailable
                ? `${metrics.activeVendors} with ledger activity`
                : 'Ledger activity unknown'}
            </p>
          </div>

          <div className="rounded-xl border border-t-emerald-200 border-x-emerald-300/80 border-b-emerald-400 bg-gradient-to-b from-emerald-50/60 via-white to-emerald-50/20 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.03)]">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-emerald-800">
                Invoiced Volume
              </span>
              <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-t-emerald-200 border-x-emerald-300/80 border-b-emerald-400 bg-gradient-to-b from-emerald-100 to-emerald-200/60 text-emerald-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </span>
            </div>
            <div className="mt-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-emerald-800 tabular-nums">
                {ledgerAvailable ? formatCurrency(metrics.invoicedTotal) : '—'}
              </span>
            </div>
            <p className="mt-1 text-xs text-zinc-400">
              {ledgerAvailable ? 'Across every recorded invoice' : 'Invoice ledger unavailable'}
            </p>
          </div>

          <div className="rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-50/60 via-white to-amber-50/20 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.03)]">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-amber-800">
                Open Balance
              </span>
              <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-100 to-amber-200/60 text-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </span>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {ledgerAvailable ? formatCurrency(metrics.openTotal) : '—'}
              </span>
              {ledgerAvailable && (
                <span className="text-xs text-zinc-500">
                  {metrics.openCount} {metrics.openCount === 1 ? 'invoice' : 'invoices'}
                </span>
              )}
            </div>
            <p className="mt-1 text-xs text-zinc-400">Pending, approved, or flagged</p>
          </div>

          <div className="rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-50/60 via-white to-amber-50/20 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.03)]">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-amber-800">
                PO Commitments
              </span>
              <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-100 to-amber-200/60 text-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              </span>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold tracking-tight text-amber-800 tabular-nums">
                {ledgerAvailable ? formatCurrency(metrics.poApprovedTotal) : '—'}
              </span>
            </div>
            <p className="mt-1 text-xs text-zinc-400">
              {ledgerAvailable ? 'Approved purchase-order value' : 'PO ledger unavailable'}
            </p>
          </div>
        </div>
      )}

      {/* Search and activity filters */}
      <div className="flex flex-col gap-3 rounded-xl border border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white/80 p-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02)] sm:flex-row sm:items-center sm:justify-between">
        <div className="relative flex-1 sm:max-w-sm">
          <label htmlFor="vendor-search" className="sr-only">
            Search vendors by name or ID
          </label>
          <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-zinc-400">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </span>
          <input
            id="vendor-search"
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search vendors by name or ID..."
            className="block w-full rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50/60 py-2 pl-9 pr-8 text-sm text-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.02)] transition-colors placeholder-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900/40"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery('')}
              title="Clear search"
              aria-label="Clear search"
              className="absolute inset-y-0 right-0 flex items-center pr-2.5 text-zinc-400 transition-colors hover:text-zinc-700"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        {ledgerAvailable && (
          <div className="flex flex-wrap items-center gap-1.5">
            {FILTERS.map((filter) => {
              const isActive = activityFilter === filter.key;
              const count = filterCounts[filter.key];
              return (
                <button
                  key={filter.key}
                  type="button"
                  onClick={() => setActivityFilter(filter.key)}
                  aria-pressed={isActive}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-all active:translate-y-[0.5px] ${
                    isActive
                      ? 'border-t border-t-zinc-700/80 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_2px_rgba(0,0,0,0.2)]'
                      : 'border border-t-white border-x-zinc-200 border-b-zinc-300/80 bg-gradient-to-b from-white to-zinc-50 text-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.02)] hover:border-zinc-300 hover:bg-zinc-100 hover:text-zinc-900'
                  }`}
                >
                  <span>{filter.label}</span>
                  <span
                    className={`rounded-full px-1.5 py-0.2 font-mono text-[10px] ${
                      isActive ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-100 text-zinc-500'
                    }`}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Directory */}
      {isLoading ? (
        <div className="overflow-hidden rounded-xl border border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02)]">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200/80 bg-zinc-50/75 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                <tr>
                  <th scope="col" className="px-5 py-3.5">Vendor</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Invoices</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Invoiced</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Open Balance</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Purchase Orders</th>
                  <th scope="col" className="px-5 py-3.5">Last Invoice</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/70">
                {[...Array(5)].map((_, i) => (
                  <tr key={i} className="animate-pulse">
                    <td className="px-5 py-4"><div className="h-4 w-36 rounded bg-zinc-200" /></td>
                    <td className="px-5 py-4"><div className="ml-auto h-4 w-8 rounded bg-zinc-200" /></td>
                    <td className="px-5 py-4"><div className="ml-auto h-4 w-24 rounded bg-zinc-200" /></td>
                    <td className="px-5 py-4"><div className="ml-auto h-4 w-24 rounded bg-zinc-200" /></td>
                    <td className="px-5 py-4"><div className="ml-auto h-4 w-20 rounded bg-zinc-200" /></td>
                    <td className="px-5 py-4"><div className="h-4 w-20 rounded bg-zinc-200" /></td>
                    <td className="px-5 py-4"><div className="ml-auto h-7 w-20 rounded bg-zinc-200" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : error ? (
        <div className="rounded-xl border border-t-red-200 border-x-red-300/80 border-b-red-400 bg-gradient-to-b from-red-50/80 to-white p-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.03)]">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-t-red-200 border-x-red-300 border-b-red-400 bg-gradient-to-b from-red-100 to-red-200/70 text-red-700">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </span>
              <div>
                <p className="text-sm font-semibold text-red-900">Failed to load vendors</p>
                <p className="mt-0.5 text-xs text-red-700">{error}</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => fetchAllData(false)}
              className="inline-flex shrink-0 items-center justify-center rounded-lg border border-t-white border-x-red-300 border-b-red-400 bg-gradient-to-b from-white to-red-50 px-3.5 py-1.5 text-xs font-semibold text-red-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-red-100 active:translate-y-[0.5px]"
            >
              Retry Connection
            </button>
          </div>
        </div>
      ) : vendors.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/70 p-12 text-center">
          <span className="flex h-14 w-14 items-center justify-center rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.9)]">
            <svg className="h-7 w-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
            </svg>
          </span>
          <h3 className="mt-4 text-base font-semibold text-zinc-900">No vendors registered</h3>
          <p className="mt-1.5 max-w-sm text-xs leading-relaxed text-zinc-500">
            Vendors appear here once they exist in the ledger. Record an invoice to start tracking
            counterparty exposure.
          </p>
          <Link
            href="/invoices/new"
            className="mt-5 inline-flex items-center gap-2 rounded-lg border-t border-t-zinc-700/80 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 px-4 py-2 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_4px_rgba(0,0,0,0.2)] transition-all hover:from-zinc-700 hover:to-zinc-800 active:translate-y-[0.5px]"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            <span>Record an Invoice</span>
          </Link>
        </div>
      ) : filteredProfiles.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white/80 p-12 text-center shadow-[0_1px_3px_rgba(0,0,0,0.02)]">
          <span className="flex h-12 w-12 items-center justify-center rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-400">
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </span>
          <h3 className="mt-3 text-sm font-semibold text-zinc-900">No matching vendors</h3>
          <p className="mt-1 text-xs text-zinc-500">No vendor matched your current search and filter.</p>
          <button
            type="button"
            onClick={resetFilters}
            className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-3.5 py-1.5 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px]"
          >
            Reset Filters
          </button>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.03)]">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">
                Vendors with their recorded invoice and purchase-order totals
              </caption>
              <thead className="border-b border-zinc-200/80 bg-zinc-50/75 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                <tr>
                  <th scope="col" className="px-5 py-3.5">Vendor</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Invoices</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Invoiced</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Open Balance</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Purchase Orders</th>
                  <th scope="col" className="px-5 py-3.5">Last Invoice</th>
                  <th scope="col" className="px-5 py-3.5 text-right">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/70">
                {filteredProfiles.map((profile) => {
                  const isCopied = copiedId === profile.id;
                  const hasActivity = profile.invoiceCount > 0 || profile.poCount > 0;

                  return (
                    <tr key={profile.id} className="transition-colors duration-150 hover:bg-zinc-50/70">
                      <td className="px-5 py-3.5">
                        <div className="flex items-center gap-2.5">
                          <span className="font-semibold text-zinc-900">{profile.name}</span>
                          {ledgerAvailable && (
                            <span
                              className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.2 text-[10px] font-semibold ${
                                hasActivity
                                  ? 'border-t-emerald-200 border-x-emerald-300/80 border-b-emerald-400 bg-gradient-to-b from-emerald-50 to-emerald-100/60 text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]'
                                  : 'border-t-zinc-200 border-x-zinc-300/80 border-b-zinc-400 bg-gradient-to-b from-white to-zinc-100/70 text-zinc-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.9)]'
                              }`}
                            >
                              {hasActivity ? 'In ledger' : 'No activity'}
                            </span>
                          )}
                          <button
                            type="button"
                            onClick={() => handleCopyId(profile.id)}
                            title={`Click to copy full ID: ${profile.id}`}
                            aria-label={`Copy vendor ID ${profile.id}`}
                            className="inline-flex items-center gap-1.5 rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300/80 bg-gradient-to-b from-white to-zinc-50 px-1.5 py-0.5 font-mono text-[11px] text-zinc-500 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.02)] transition-colors hover:text-zinc-900 active:translate-y-[0.5px]"
                          >
                            <span>{profile.id}</span>
                            {isCopied ? (
                              <span className="font-sans text-[10px] font-bold text-emerald-700">
                                Copied
                              </span>
                            ) : (
                              <svg className="h-3 w-3 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                              </svg>
                            )}
                          </button>
                        </div>
                      </td>

                      <td className="whitespace-nowrap px-5 py-3.5 text-right font-mono text-xs tabular-nums text-zinc-600">
                        {ledgerAvailable ? profile.invoiceCount : '—'}
                      </td>

                      <td className="whitespace-nowrap px-5 py-3.5 text-right font-mono text-xs font-semibold tabular-nums text-zinc-900">
                        {ledgerAvailable ? formatCurrency(profile.invoicedTotal) : '—'}
                      </td>

                      <td className="whitespace-nowrap px-5 py-3.5 text-right font-mono text-xs font-semibold tabular-nums">
                        {!ledgerAvailable ? (
                          <span className="text-zinc-300">—</span>
                        ) : profile.openCount > 0 ? (
                          <span className="text-amber-700">{formatCurrency(profile.openTotal)}</span>
                        ) : (
                          <span className="text-zinc-300">—</span>
                        )}
                      </td>

                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        {!ledgerAvailable ? (
                          <span className="font-mono text-xs text-zinc-300">—</span>
                        ) : profile.poCount > 0 ? (
                          <span className="font-mono text-xs tabular-nums text-zinc-600">
                            {profile.poCount} ·{' '}
                            <span className="font-semibold text-zinc-900">
                              {formatCurrency(profile.poApprovedTotal)}
                            </span>
                          </span>
                        ) : (
                          <span className="text-xs text-zinc-300">No POs</span>
                        )}
                      </td>

                      <td className="whitespace-nowrap px-5 py-3.5 text-xs text-zinc-600">
                        {ledgerAvailable && profile.lastInvoiceDate ? (
                          <span className="font-mono">{profile.lastInvoiceDate}</span>
                        ) : (
                          <span className="text-zinc-300">—</span>
                        )}
                      </td>

                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        <button
                          type="button"
                          onClick={() => openProfile(profile)}
                          className="inline-flex items-center gap-1 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-1 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] transition-all hover:bg-zinc-100 active:translate-y-[0.5px]"
                        >
                          <span>Details</span>
                          <svg className="h-3 w-3 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
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

          <div className="flex flex-col gap-2 border-t border-zinc-200/80 bg-zinc-50/50 px-5 py-3 text-xs text-zinc-500 sm:flex-row sm:items-center sm:justify-between">
            <div>
              Showing <span className="font-semibold text-zinc-700">{filteredProfiles.length}</span> of{' '}
              <span className="font-semibold text-zinc-700">{vendors.length}</span>{' '}
              {vendors.length === 1 ? 'vendor' : 'vendors'}
              {hasActiveFilters && ' (filtered)'}
            </div>
            {hasActiveFilters && (
              <button
                type="button"
                onClick={resetFilters}
                className="self-start text-xs font-medium text-zinc-600 underline-offset-2 transition-colors hover:text-zinc-900 hover:underline"
              >
                Clear filters to show all
              </button>
            )}
          </div>
        </div>
      )}

      {/* Vendor details dialog */}
      {selectedVendor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/40 p-4"
          onClick={() => setSelectedVendorId(null)}
        >
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="vendor-details-title"
            tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
            className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white shadow-[0_24px_48px_-12px_rgba(0,0,0,0.18),inset_0_1px_0_rgba(255,255,255,0.9)] focus:outline-none"
          >
            <div className="sticky top-0 flex items-start justify-between gap-4 border-b border-zinc-200/90 bg-gradient-to-b from-white to-zinc-50/80 px-6 py-4">
              <div className="flex items-center gap-3">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-t-zinc-700 border-x-zinc-800 border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_4px_rgba(0,0,0,0.2)]">
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
                  </svg>
                </span>
                <div>
                  <h2 id="vendor-details-title" className="text-base font-bold text-zinc-900">
                    {selectedVendor.name}
                  </h2>
                  <p className="mt-0.5 font-mono text-xs text-zinc-500">{selectedVendor.id}</p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setSelectedVendorId(null)}
                aria-label="Close vendor details"
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] transition-all hover:bg-zinc-100 hover:text-zinc-700 active:translate-y-[0.5px]"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="space-y-5 p-6 text-sm">
              {!ledgerAvailable ? (
                <p className="rounded-xl border border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-amber-50/70 p-4 text-xs text-amber-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.9)]">
                  The invoice and purchase-order ledgers did not load, so this profile shows only
                  what the vendor directory knows. Totals are omitted rather than shown as zero.
                </p>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50/70 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)]">
                      <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                        Invoiced Total
                      </span>
                      <div className="mt-1 font-mono text-xl font-bold tabular-nums text-zinc-900">
                        {formatCurrency(selectedVendor.invoicedTotal)}
                      </div>
                      <p className="mt-0.5 text-xs text-zinc-400">
                        {selectedVendor.invoiceCount}{' '}
                        {selectedVendor.invoiceCount === 1 ? 'invoice' : 'invoices'}
                      </p>
                    </div>
                    <div
                      className={`rounded-xl border p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] ${
                        selectedVendor.openCount > 0
                          ? 'border-t-amber-200 border-x-amber-300/80 border-b-amber-400 bg-gradient-to-b from-amber-50/80 to-amber-50/30'
                          : 'border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50/70'
                      }`}
                    >
                      <span
                        className={`text-[11px] font-semibold uppercase tracking-wider ${
                          selectedVendor.openCount > 0 ? 'text-amber-800' : 'text-zinc-500'
                        }`}
                      >
                        Open Balance
                      </span>
                      <div
                        className={`mt-1 font-mono text-xl font-bold tabular-nums ${
                          selectedVendor.openCount > 0 ? 'text-amber-800' : 'text-zinc-400'
                        }`}
                      >
                        {formatCurrency(selectedVendor.openTotal)}
                      </div>
                      <p className="mt-0.5 text-xs text-zinc-400">
                        {selectedVendor.openCount}{' '}
                        {selectedVendor.openCount === 1 ? 'invoice' : 'invoices'} not settled
                      </p>
                    </div>
                  </div>

                  <section>
                    <h3 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                      Invoice Status Breakdown
                    </h3>
                    {selectedVendor.statusSlices.length === 0 ? (
                      <p className="mt-2 rounded-xl border border-dashed border-zinc-300 bg-zinc-50/60 p-4 text-xs text-zinc-500">
                        No invoices recorded for this vendor.
                      </p>
                    ) : (
                      <div className="mt-2 divide-y divide-zinc-200/70 overflow-hidden rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)]">
                        {selectedVendor.statusSlices.map((slice) => (
                          <div
                            key={slice.status}
                            className="flex items-center justify-between gap-3 px-4 py-2.5 text-xs"
                          >
                            <span
                              className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-semibold ${STATUS_TONE[slice.status]}`}
                            >
                              <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
                              {STATUS_LABELS[slice.status]}
                            </span>
                            <span className="flex items-center gap-4">
                              <span className="font-mono text-zinc-500">
                                {slice.count} {slice.count === 1 ? 'record' : 'records'}
                              </span>
                              <span className="font-mono font-semibold tabular-nums text-zinc-900">
                                {formatCurrency(slice.total)}
                              </span>
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </section>

                  <section>
                    <h3 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                      Purchase Orders ({selectedVendor.poCount})
                    </h3>
                    {selectedVendor.purchaseOrders.length === 0 ? (
                      <p className="mt-2 rounded-xl border border-dashed border-zinc-300 bg-zinc-50/60 p-4 text-xs text-zinc-500">
                        No purchase orders recorded for this vendor.
                      </p>
                    ) : (
                      <div className="mt-2 divide-y divide-zinc-200/70 overflow-hidden rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)]">
                        {selectedVendor.purchaseOrders.map((po) => (
                          <div
                            key={po.po_number}
                            className="flex items-center justify-between gap-3 px-4 py-2.5 text-xs"
                          >
                            <span className="font-mono text-zinc-700">{po.po_number}</span>
                            <span className="flex items-center gap-4">
                              <span className="rounded border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-600">
                                {po.status || 'unknown'}
                              </span>
                              <span className="font-mono font-semibold tabular-nums text-zinc-900">
                                {formatCurrency(po.approved_amount)}
                              </span>
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </section>
                </>
              )}
            </div>

            <div className="flex flex-col gap-2 border-t border-zinc-200/90 bg-zinc-50/70 px-6 py-3.5 sm:flex-row sm:items-center sm:justify-end">
              <button
                type="button"
                onClick={() => setSelectedVendorId(null)}
                className="inline-flex items-center justify-center rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-4 py-2 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px]"
              >
                Close
              </button>
              <Link
                href="/invoices"
                className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-t-zinc-700/80 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 px-4 py-2 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_4px_rgba(0,0,0,0.2)] transition-all hover:from-zinc-700 hover:to-zinc-800 active:translate-y-[0.5px]"
              >
                <span>View Invoices</span>
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                </svg>
              </Link>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
