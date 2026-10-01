'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import type { VendorDTO } from '@/lib/types';

export default function NewInvoicePage() {
  const router = useRouter();

  const [vendors, setVendors] = useState<VendorDTO[]>([]);
  const [loadingVendors, setLoadingVendors] = useState(true);

  // Form states
  const [vendor, setVendor] = useState('');
  const [amount, setAmount] = useState('');
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [poNumber, setPoNumber] = useState('');

  // Submission feedback states
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({});
  const [generalError, setGeneralError] = useState<string | null>(null);

  useEffect(() => {
    async function loadVendors() {
      try {
        const res = await fetch('/api/vendors');
        if (res.ok) {
          const data: VendorDTO[] = await res.json();
          setVendors(data);
          if (data.length > 0) {
            setVendor(data[0].name);
          }
        }
      } catch (err) {
        console.error('Failed to load vendors:', err);
      } finally {
        setLoadingVendors(false);
      }
    }
    loadVendors();
  }, []);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setIsSubmitting(true);
    setFieldErrors({});
    setGeneralError(null);

    // Client-side quick validation
    const errors: Record<string, string[]> = {};
    if (!vendor.trim()) {
      errors.vendor = ['Vendor is required'];
    }
    const numAmount = Number(amount);
    if (!amount || isNaN(numAmount) || numAmount <= 0) {
      errors.amount = ['Amount must be a positive number'];
    }
    if (!date.trim() || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      errors.date = ['A valid date in YYYY-MM-DD format is required'];
    }

    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors);
      setIsSubmitting(false);
      return;
    }

    try {
      const payload = {
        vendor: vendor.trim(),
        amount: numAmount,
        date: date.trim(),
        po_number: poNumber.trim() ? poNumber.trim() : null,
      };

      const res = await fetch('/api/invoices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const data = await res.json();

      if (!res.ok) {
        if (data.details) {
          setFieldErrors(data.details);
        }
        setGeneralError(data.error || 'Failed to create invoice');
        return;
      }

      // Success: redirect to /invoices
      router.push('/invoices');
      router.refresh();
    } catch (err: unknown) {
      setGeneralError(err instanceof Error ? err.message : 'Network error submitting invoice');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div className="flex items-center justify-between border-b border-zinc-200 pb-5 dark:border-zinc-800">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
            Create Invoice
          </h1>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            Record a new invoice into the operations ledger.
          </p>
        </div>
        <Link
          href="/invoices"
          className="rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-xs font-medium text-zinc-700 shadow-sm transition-colors hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          Cancel
        </Link>
      </div>

      {generalError && (
        <div
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300"
        >
          <div className="flex items-center gap-2 font-medium">
            <svg className="h-4 w-4 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Submission Error
          </div>
          <p className="mt-1 text-xs text-red-700 dark:text-red-400">{generalError}</p>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6 rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60">
        {/* Vendor Field */}
        <div>
          <label htmlFor="vendor" className="block text-xs font-semibold uppercase tracking-wider text-zinc-700 dark:text-zinc-300">
            Vendor <span className="text-red-500">*</span>
          </label>
          <div className="mt-1.5">
            {loadingVendors ? (
              <div className="h-10 w-full animate-pulse rounded-lg bg-zinc-100 dark:bg-zinc-800" />
            ) : (
              <select
                id="vendor"
                value={vendor}
                onChange={(e) => setVendor(e.target.value)}
                aria-invalid={!!fieldErrors.vendor}
                aria-describedby={fieldErrors.vendor ? 'vendor-error' : undefined}
                className="block w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 shadow-sm focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:focus:border-zinc-400 dark:focus:ring-zinc-400"
              >
                <option value="" disabled>Select a vendor</option>
                {vendors.map((v) => (
                  <option key={v.id} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </select>
            )}
          </div>
          {fieldErrors.vendor && (
            <p id="vendor-error" role="alert" className="mt-1.5 text-xs text-red-600 dark:text-red-400">
              {fieldErrors.vendor[0]}
            </p>
          )}
        </div>

        {/* Amount Field */}
        <div>
          <label htmlFor="amount" className="block text-xs font-semibold uppercase tracking-wider text-zinc-700 dark:text-zinc-300">
            Amount (USD) <span className="text-red-500">*</span>
          </label>
          <div className="relative mt-1.5 rounded-lg shadow-sm">
            <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3">
              <span className="text-sm font-medium text-zinc-500">$</span>
            </div>
            <input
              type="number"
              step="0.01"
              min="0.01"
              id="amount"
              placeholder="0.00"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              aria-invalid={!!fieldErrors.amount}
              aria-describedby={fieldErrors.amount ? 'amount-error' : undefined}
              className="block w-full rounded-lg border border-zinc-300 bg-white py-2 pl-7 pr-3 text-sm text-zinc-900 shadow-sm focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:focus:border-zinc-400 dark:focus:ring-zinc-400 font-mono"
            />
          </div>
          {fieldErrors.amount && (
            <p id="amount-error" role="alert" className="mt-1.5 text-xs text-red-600 dark:text-red-400">
              {fieldErrors.amount[0]}
            </p>
          )}
        </div>

        {/* Date Field */}
        <div>
          <label htmlFor="date" className="block text-xs font-semibold uppercase tracking-wider text-zinc-700 dark:text-zinc-300">
            Invoice Date <span className="text-red-500">*</span>
          </label>
          <div className="mt-1.5">
            <input
              type="date"
              id="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              aria-invalid={!!fieldErrors.date}
              aria-describedby={fieldErrors.date ? 'date-error' : undefined}
              className="block w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 shadow-sm focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:focus:border-zinc-400 dark:focus:ring-zinc-400"
            />
          </div>
          {fieldErrors.date && (
            <p id="date-error" role="alert" className="mt-1.5 text-xs text-red-600 dark:text-red-400">
              {fieldErrors.date[0]}
            </p>
          )}
        </div>

        {/* PO Number Field */}
        <div>
          <label htmlFor="po_number" className="block text-xs font-semibold uppercase tracking-wider text-zinc-700 dark:text-zinc-300">
            PO Number <span className="text-zinc-400 font-normal normal-case">(optional)</span>
          </label>
          <div className="mt-1.5">
            <input
              type="text"
              id="po_number"
              placeholder="e.g. PO-1001"
              value={poNumber}
              onChange={(e) => setPoNumber(e.target.value)}
              aria-invalid={!!fieldErrors.po_number}
              aria-describedby={fieldErrors.po_number ? 'po-error' : undefined}
              className="block w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm font-mono text-zinc-900 shadow-sm focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:focus:border-zinc-400 dark:focus:ring-zinc-400"
            />
          </div>
          {fieldErrors.po_number && (
            <p id="po-error" role="alert" className="mt-1.5 text-xs text-red-600 dark:text-red-400">
              {fieldErrors.po_number[0]}
            </p>
          )}
        </div>

        {/* Submit Actions */}
        <div className="flex items-center justify-end gap-3 pt-4 border-t border-zinc-100 dark:border-zinc-800">
          <Link
            href="/invoices"
            className="rounded-lg px-4 py-2 text-xs font-medium text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            Cancel
          </Link>
          <button
            type="submit"
            disabled={isSubmitting}
            className="inline-flex items-center justify-center rounded-lg bg-zinc-900 px-5 py-2 text-sm font-medium text-white shadow-sm transition-all hover:bg-zinc-800 active:translate-y-px disabled:opacity-50 disabled:pointer-events-none dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
          >
            {isSubmitting ? (
              <span className="flex items-center gap-2">
                <svg className="h-4 w-4 animate-spin text-current" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                </svg>
                Creating...
              </span>
            ) : (
              'Create Invoice'
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
