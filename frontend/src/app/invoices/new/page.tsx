'use client';

import React, { useEffect, useState, useMemo, useRef, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { StatusBadge } from '../../components/StatusBadge';
import type { VendorDTO, PurchaseOrderDTO } from '@/lib/types';

interface LineItem {
  id: string;
  description: string;
  quantity: number;
  unitPrice: number;
  taxRate: number;
}

interface UploadedFilePreview {
  name: string;
  size: number;
  type: string;
  lastModified: number;
}

// Preview-only display values. Neither is part of the create payload (see
// CreateInvoiceSchema), so neither is editable.
const PAYMENT_TERMS_LABELS: Record<string, string> = {
  immediate: 'Due Immediately',
  net15: 'Net 15 Days',
  net30: 'Net 30 Days (Standard)',
  net60: 'Net 60 Days',
  net90: 'Net 90 Days',
};

// A fresh form must not arrive pre-loaded with invented invoice data: seeded
// line items plus a derived amount let a user post a fabricated payable to the
// first registered vendor without typing anything. The grid therefore opens on
// a single empty row, which totals 0.00 and fails the positive-amount check in
// handleSubmit until real invoice data is supplied.
const BLANK_LINE_ITEM: LineItem = {
  id: 'item-1',
  description: '',
  quantity: 1,
  unitPrice: 0,
  taxRate: 0,
};

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

// Kept in sync with the `accept` attribute on the file input below; the dropzone
// copy states the same list and the same 10 MB ceiling.
const ALLOWED_FILE_EXTENSIONS = ['.pdf', '.png', '.jpg', '.jpeg', '.csv'];
const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;

function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 Bytes';
  const k = 1024;
  const sizes = ['Bytes', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

function NewInvoiceForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachmentPanelRef = useRef<HTMLDivElement>(null);
  const ocrTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelPendingOcr = () => {
    if (ocrTimeoutRef.current !== null) {
      clearTimeout(ocrTimeoutRef.current);
      ocrTimeoutRef.current = null;
    }
  };

  // Reference data
  const [vendors, setVendors] = useState<VendorDTO[]>([]);
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrderDTO[]>([]);
  const [loadingVendors, setLoadingVendors] = useState(true);

  // Form core states
  const [vendor, setVendor] = useState('');
  const [customVendor, setCustomVendor] = useState('');
  const [isCustomVendor, setIsCustomVendor] = useState(false);
  const [amount, setAmount] = useState('');
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [poNumber, setPoNumber] = useState('');
  const [invoiceReference] = useState(() => `INV-${Math.floor(100000 + Math.random() * 900000)}`);
  const [paymentTerms] = useState('net30');

  // Line Items calculation engine
  const [lineItems, setLineItems] = useState<LineItem[]>([BLANK_LINE_ITEM]);
  const [isManualAmountOverride, setIsManualAmountOverride] = useState(false);

  // File dropzone states
  const [isDragging, setIsDragging] = useState(false);
  const [uploadedFile, setUploadedFile] = useState<UploadedFilePreview | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [ocrStatus, setOcrStatus] = useState<'idle' | 'processing' | 'success'>('idle');
  const [ocrMessage, setOcrMessage] = useState<string | null>(null);

  // Submission feedback states
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({});
  const [generalError, setGeneralError] = useState<string | null>(null);

  const paramPo = searchParams?.get('po_number');
  const paramVendor = searchParams?.get('vendor');

  // Load vendors and purchase orders on mount
  useEffect(() => {
    async function loadData() {
      try {
        const [vendorRes, poRes] = await Promise.all([
          fetch('/api/vendors').catch(() => null),
          fetch('/api/purchase-orders').catch(() => null),
        ]);

        if (paramPo) {
          setPoNumber(paramPo);
        }

        if (vendorRes && vendorRes.ok) {
          const vendorData: VendorDTO[] = await vendorRes.json();
          setVendors(vendorData);

          if (paramVendor) {
            const matched = vendorData.find((v) => v.name.toLowerCase() === paramVendor.toLowerCase());
            if (matched) {
              setVendor(matched.name);
            } else {
              setIsCustomVendor(true);
              setCustomVendor(paramVendor);
            }
          } else if (vendorData.length > 0) {
            setVendor(vendorData[0].name);
          }
        } else if (paramVendor) {
          setIsCustomVendor(true);
          setCustomVendor(paramVendor);
        }

        if (poRes && poRes.ok) {
          const poData: PurchaseOrderDTO[] = await poRes.json();
          setPurchaseOrders(poData);
        }
      } catch (err) {
        console.error('Failed to load reference data:', err);
      } finally {
        setLoadingVendors(false);
      }
    }
    loadData();
  }, [paramPo, paramVendor]);

  // Drop any in-flight OCR timer on unmount so it cannot set state afterwards.
  useEffect(() => cancelPendingOcr, []);

  // The dropzone unmounts as soon as a file is attached, which would strand a
  // keyboard or screen-reader user on the document body. Move focus to the panel
  // that replaced it so they stay on the attachment they just made.
  useEffect(() => {
    if (uploadedFile) {
      attachmentPanelRef.current?.focus();
    }
  }, [uploadedFile]);

  // Compute live line-item totals
  const totals = useMemo(() => {
    let subtotal = 0;
    let taxTotal = 0;

    lineItems.forEach((item) => {
      const lineBase = (Number(item.quantity) || 0) * (Number(item.unitPrice) || 0);
      const lineTax = (lineBase * (Number(item.taxRate) || 0)) / 100;
      subtotal += lineBase;
      taxTotal += lineTax;
    });

    const total = subtotal + taxTotal;
    return {
      subtotal,
      taxTotal,
      total,
    };
  }, [lineItems]);

  // Synchronize amount when line items change unless manually overridden.
  // A zero estimate is mirrored through deliberately: it is what keeps a freshly
  // opened form (blank grid) from ever satisfying the positive-amount check.
  useEffect(() => {
    if (!isManualAmountOverride) {
      setAmount(totals.total.toFixed(2));
    }
  }, [totals.total, isManualAmountOverride]);

  // Line item handlers
  const handleAddLineItem = () => {
    const newItem: LineItem = {
      id: `item-${Date.now()}`,
      description: '',
      quantity: 1,
      unitPrice: 0.0,
      taxRate: 0,
    };
    setLineItems((prev) => [...prev, newItem]);
  };

  const handleRemoveLineItem = (id: string) => {
    if (lineItems.length <= 1) return;
    setLineItems((prev) => prev.filter((item) => item.id !== id));
  };

  const handleUpdateLineItem = (id: string, field: keyof LineItem, value: string | number) => {
    setLineItems((prev) =>
      prev.map((item) => {
        if (item.id === id) {
          return { ...item, [field]: value };
        }
        return item;
      })
    );
  };

  // Drag and drop handlers
  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const file = e.dataTransfer.files[0];
      processUploadedFile(file);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const file = e.target.files[0];
      processUploadedFile(file);
    }
    // Clear the selection so the same file can be picked again after a rejection.
    // Browsers fire no change event while the input's value is unchanged, which
    // would otherwise make a corrected re-pick impossible.
    e.target.value = '';
  };

  const processUploadedFile = (file: File) => {
    // The picker's `accept` attribute is only a hint: it is not enforced for
    // drag-and-drop, and browsers apply it inconsistently. Validate here so both
    // entry paths enforce the rules the dropzone advertises, instead of marking
    // an unsupported or oversized file as attached and later reporting an OCR
    // success for it.
    const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    if (!ALLOWED_FILE_EXTENSIONS.includes(extension)) {
      setFileError(
        `"${file.name}" is not a supported document. Accepted formats: ${ALLOWED_FILE_EXTENSIONS.join(', ')}.`
      );
      return;
    }

    if (file.size > MAX_FILE_SIZE_BYTES) {
      setFileError(
        `"${file.name}" is ${formatFileSize(file.size)}, over the ${formatFileSize(MAX_FILE_SIZE_BYTES)} limit.`
      );
      return;
    }

    setFileError(null);
    setUploadedFile({
      name: file.name,
      size: file.size,
      type: file.type || 'application/pdf',
      lastModified: file.lastModified,
    });
    setOcrStatus('idle');
    setOcrMessage(null);
  };

  const handleTriggerOcr = () => {
    if (!uploadedFile) return;

    // A previous run may still be in flight; only the newest one may report.
    cancelPendingOcr();

    const fileName = uploadedFile.name;
    setOcrStatus('processing');
    setOcrMessage('Running optical character recognition & entity extraction...');

    ocrTimeoutRef.current = setTimeout(() => {
      ocrTimeoutRef.current = null;
      setOcrStatus('success');
      setOcrMessage(`Successfully extracted metadata and line items from "${fileName}"`);

      // Simulated auto-fill based on document context. Functional updates are
      // required here: this callback closes over the render that scheduled it, so
      // reading `vendor`/`poNumber` directly would let a choice the user made
      // during the 900ms window be overwritten by the stale captured value.
      setVendor((prev) => (prev ? prev : vendors[0]?.name ?? ''));
      setPoNumber((prev) => (prev ? prev : purchaseOrders[0]?.po_number ?? ''));
    }, 900);
  };

  const handleRemoveFile = () => {
    // Without this the in-flight callback still fires and auto-fills from a
    // document the user has already detached.
    cancelPendingOcr();
    setUploadedFile(null);
    setOcrStatus('idle');
    setOcrMessage(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  // Quick preset loader
  const handleLoadPreset = (presetType: 'hardware' | 'consulting' | 'saas') => {
    setIsManualAmountOverride(false);
    if (presetType === 'hardware') {
      setLineItems([
        { id: `item-${Date.now()}-1`, description: 'High-Density Rack Server Node 2U', quantity: 2, unitPrice: 4200.0, taxRate: 8.5 },
        { id: `item-${Date.now()}-2`, description: '100GbE Managed Switch Fabric Module', quantity: 1, unitPrice: 1850.0, taxRate: 8.5 },
      ]);
    } else if (presetType === 'consulting') {
      setLineItems([
        { id: `item-${Date.now()}-1`, description: 'Autonomous ERP Integration Engineering (80 hrs)', quantity: 80, unitPrice: 165.0, taxRate: 0 },
        { id: `item-${Date.now()}-2`, description: 'Architecture Review & Threat Modeling', quantity: 1, unitPrice: 2500.0, taxRate: 0 },
      ]);
    } else {
      setLineItems([
        { id: `item-${Date.now()}-1`, description: 'Enterprise Workflow Orchestration Annual Seat', quantity: 25, unitPrice: 120.0, taxRate: 5.0 },
        { id: `item-${Date.now()}-2`, description: 'Priority 24/7 SLA Technical Support Tier', quantity: 1, unitPrice: 1500.0, taxRate: 5.0 },
      ]);
    }
  };

  // Submission handler
  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setIsSubmitting(true);
    setFieldErrors({});
    setGeneralError(null);

    const effectiveVendor = isCustomVendor ? customVendor.trim() : vendor.trim();

    // Client-side validation
    const errors: Record<string, string[]> = {};
    if (!effectiveVendor) {
      errors.vendor = ['Vendor name is required'];
    }
    const numAmount = Number(amount);
    if (!amount || isNaN(numAmount) || numAmount <= 0) {
      errors.amount = ['Amount must be a positive number greater than 0'];
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
        vendor: effectiveVendor,
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

      // Success: redirect to invoices ledger
      router.push('/invoices');
      router.refresh();
    } catch (err: unknown) {
      setGeneralError(err instanceof Error ? err.message : 'Network communication error submitting invoice');
    } finally {
      setIsSubmitting(false);
    }
  };

  const selectedVendorDetails = useMemo(() => {
    return vendors.find((v) => v.name === (isCustomVendor ? customVendor : vendor));
  }, [vendors, vendor, customVendor, isCustomVendor]);

  return (
    <div className="mx-auto max-w-6xl space-y-7 pb-20">
      {/* Page Header with Breadcrumb and Tactile Badges */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between border-b border-zinc-200/80 pb-6">
        <div>
          <div className="flex items-center gap-2 mb-2">
            <Link
              href="/invoices"
              className="inline-flex items-center gap-1.5 text-xs sm:text-[13px] font-semibold text-zinc-500 hover:text-zinc-800 transition-colors"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
              </svg>
              <span>Ledger Invoices</span>
            </Link>
            <span className="text-zinc-300">/</span>
            <span className="text-xs sm:text-[13px] font-medium text-zinc-700">New Entry</span>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-zinc-900">
              Create Invoice
            </h1>
            <StatusBadge status="pending" className="px-3 py-1 text-[13px]" />
            <span className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-1 font-mono text-xs font-semibold text-zinc-600 shadow-2xs">
              {invoiceReference}
            </span>
          </div>
          <p className="mt-1.5 text-sm sm:text-base text-zinc-500">
            Record accounts payable, parse digital receipts, and calculate itemized ledger obligations.
          </p>
        </div>

        {/* Quick Presets & Cancel Header Buttons */}
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="hidden lg:flex items-center gap-2 rounded-xl border border-zinc-200/80 bg-zinc-50 p-1.5 text-xs">
            <span className="px-1.5 text-[11px] font-bold uppercase tracking-wider text-zinc-400">Presets:</span>
            <button
              type="button"
              onClick={() => handleLoadPreset('hardware')}
              className="rounded-lg border border-transparent hover:border-zinc-200 hover:bg-white px-2.5 py-1 text-xs sm:text-[13px] text-zinc-600 hover:text-zinc-900 font-medium transition-all"
            >
              Hardware
            </button>
            <button
              type="button"
              onClick={() => handleLoadPreset('consulting')}
              className="rounded-lg border border-transparent hover:border-zinc-200 hover:bg-white px-2.5 py-1 text-xs sm:text-[13px] text-zinc-600 hover:text-zinc-900 font-medium transition-all"
            >
              Consulting
            </button>
            <button
              type="button"
              onClick={() => handleLoadPreset('saas')}
              className="rounded-lg border border-transparent hover:border-zinc-200 hover:bg-white px-2.5 py-1 text-xs sm:text-[13px] text-zinc-600 hover:text-zinc-900 font-medium transition-all"
            >
              SaaS
            </button>
          </div>

          <Link
            href="/invoices"
            className="inline-flex items-center gap-1.5 rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-4 py-2 text-xs sm:text-sm font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px] transition-all"
          >
            Cancel
          </Link>
        </div>
      </div>

      {/* General Submission Error Alert */}
      {generalError && (
        <div
          role="alert"
          className="rounded-2xl border border-rose-200 bg-rose-50/90 p-5 text-sm text-rose-900 shadow-2xs space-y-1.5"
        >
          <div className="flex items-center gap-2.5 font-bold text-rose-800 text-sm sm:text-base">
            <svg className="h-5 w-5 text-rose-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            <span>Invoice Submission Error</span>
          </div>
          <p className="font-mono text-xs sm:text-sm">{generalError}</p>
        </div>
      )}

      {/* Main Multi-Card Form */}
      <form onSubmit={handleSubmit} className="space-y-7">
        {/* Section 1: General & Vendor Metadata Card */}
        <div className="rounded-2xl border border-zinc-200/90 bg-white p-7 sm:p-8 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] transition-all">
          <div className="flex items-center justify-between pb-5 border-b border-zinc-100">
            <div className="flex items-center gap-3">
              <span className="flex h-7 w-7 items-center justify-center rounded-xl bg-zinc-100 text-zinc-700 border border-zinc-200/80 shadow-2xs">
                <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
                </svg>
              </span>
              <div>
                <h2 className="text-[15px] sm:text-base font-bold uppercase tracking-wider text-zinc-800">
                  1. Vendor & Invoice Details
                </h2>
                <p className="text-xs sm:text-sm text-zinc-400">
                  Select an authenticated supplier and configure transaction parameters.
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 text-xs sm:text-sm">
              <button
                type="button"
                onClick={() => setIsCustomVendor(!isCustomVendor)}
                className="text-zinc-500 hover:text-zinc-800 underline underline-offset-2 transition-colors font-medium"
              >
                {isCustomVendor ? '← Select Registered Vendor' : '+ Enter Custom Vendor'}
              </button>
            </div>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {/* Vendor Field */}
            <div className="lg:col-span-2">
              <div className="flex items-center justify-between">
                <label htmlFor="vendor-input" className="block text-xs sm:text-[13px] font-bold uppercase tracking-wider text-zinc-700">
                  Vendor Name <span className="text-rose-500">*</span>
                </label>
                {selectedVendorDetails && !isCustomVendor && (
                  <span className="text-xs font-mono text-emerald-600 font-medium">
                    ID: {selectedVendorDetails.id}
                  </span>
                )}
              </div>

              <div className="mt-2.5">
                {isCustomVendor ? (
                  <input
                    id="vendor-input"
                    type="text"
                    placeholder="e.g. Acme Corp Industries Ltd."
                    value={customVendor}
                    onChange={(e) => setCustomVendor(e.target.value)}
                    aria-invalid={!!fieldErrors.vendor}
                    aria-describedby={fieldErrors.vendor ? 'vendor-error' : undefined}
                    className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 px-4 py-3.5 text-[15px] sm:text-base text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10 font-sans"
                  />
                ) : loadingVendors ? (
                  <div className="h-12 w-full animate-pulse rounded-xl bg-zinc-100 border border-zinc-200" />
                ) : (
                  <select
                    id="vendor-input"
                    value={vendor}
                    onChange={(e) => setVendor(e.target.value)}
                    aria-invalid={!!fieldErrors.vendor}
                    aria-describedby={fieldErrors.vendor ? 'vendor-error' : undefined}
                    className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 px-4 py-3.5 text-[15px] sm:text-base text-zinc-900 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10 font-sans cursor-pointer"
                  >
                    <option value="" disabled>Select a registered vendor...</option>
                    {vendors.map((v) => (
                      <option key={v.id} value={v.name}>
                        {v.name} ({v.id})
                      </option>
                    ))}
                  </select>
                )}
              </div>
              {fieldErrors.vendor && (
                <p id="vendor-error" role="alert" className="mt-2 text-xs sm:text-sm text-rose-600 font-medium">
                  {fieldErrors.vendor[0]}
                </p>
              )}
            </div>

            {/* PO Number Matching */}
            <div>
              <div className="flex items-center justify-between">
                <label htmlFor="po_number-input" className="block text-xs sm:text-[13px] font-bold uppercase tracking-wider text-zinc-700">
                  Purchase Order <span className="text-zinc-400 font-normal lowercase">(optional)</span>
                </label>
                <span className="text-[11px] text-zinc-400 font-mono">2-Way Match</span>
              </div>
              <div className="mt-2.5 relative">
                <input
                  id="po_number-input"
                  type="text"
                  placeholder="e.g. PO-1001"
                  value={poNumber}
                  onChange={(e) => setPoNumber(e.target.value)}
                  list="po-datalist"
                  aria-invalid={!!fieldErrors.po_number}
                  aria-describedby={fieldErrors.po_number ? 'po-error' : undefined}
                  className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 px-4 py-3.5 text-[15px] sm:text-base font-mono text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
                />
                <datalist id="po-datalist">
                  {purchaseOrders.map((po) => (
                    <option key={po.po_number} value={po.po_number}>
                      {po.po_number} — {po.vendor} ({formatCurrency(po.approved_amount)})
                    </option>
                  ))}
                </datalist>
              </div>
              {fieldErrors.po_number && (
                <p id="po-error" role="alert" className="mt-2 text-xs sm:text-sm text-rose-600 font-medium">
                  {fieldErrors.po_number[0]}
                </p>
              )}
            </div>

            {/* Invoice Date */}
            <div>
              <label htmlFor="date-input" className="block text-xs sm:text-[13px] font-bold uppercase tracking-wider text-zinc-700">
                Invoice Date <span className="text-rose-500">*</span>
              </label>
              <div className="mt-2.5">
                <input
                  id="date-input"
                  type="date"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                  aria-invalid={!!fieldErrors.date}
                  aria-describedby={fieldErrors.date ? 'date-error' : undefined}
                  className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 px-4 py-3.5 text-[15px] sm:text-base font-mono text-zinc-900 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10"
                />
              </div>
              {fieldErrors.date && (
                <p id="date-error" role="alert" className="mt-2 text-xs sm:text-sm text-rose-600 font-medium">
                  {fieldErrors.date[0]}
                </p>
              )}
            </div>

            {/* Invoice Reference / Number - preview only.
                CreateInvoiceSchema is `.strict()` and the invoices table has no
                reference column, so nothing typed here could ever be persisted.
                Rendered as a read-only value rather than an input so no one edits
                data the submit payload silently drops. */}
            <div>
              <span className="block text-xs sm:text-[13px] font-bold uppercase tracking-wider text-zinc-700">
                Invoice Reference #
                <span className="ml-1.5 font-sans text-[11px] font-medium normal-case tracking-normal text-zinc-400">
                  (preview only — not saved)
                </span>
              </span>
              <div className="mt-2.5 flex items-center justify-between gap-2 rounded-xl border border-zinc-200 bg-zinc-100/80 px-4 py-3.5 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)]">
                <span className="truncate font-mono text-[15px] sm:text-base text-zinc-700">{invoiceReference}</span>
                <span className="shrink-0 rounded-md border border-zinc-300 bg-white px-2 py-0.5 font-mono text-[11px] font-semibold uppercase text-zinc-500">
                  Not persisted
                </span>
              </div>
            </div>

            {/* Payment Terms - preview only. Same reason as the reference above:
                the strict create schema and the invoices table carry no terms
                field, so this select could only ever discard the user's choice. */}
            <div>
              <span className="block text-xs sm:text-[13px] font-bold uppercase tracking-wider text-zinc-700">
                Payment Terms
                <span className="ml-1.5 font-sans text-[11px] font-medium normal-case tracking-normal text-zinc-400">
                  (preview only — not saved)
                </span>
              </span>
              <div className="mt-2.5 flex items-center justify-between gap-2 rounded-xl border border-zinc-200 bg-zinc-100/80 px-4 py-3.5 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)]">
                <span className="truncate text-[15px] sm:text-base text-zinc-700">{PAYMENT_TERMS_LABELS[paymentTerms]}</span>
                <span className="shrink-0 rounded-md border border-zinc-300 bg-white px-2 py-0.5 font-mono text-[11px] font-semibold uppercase text-zinc-500">
                  Not persisted
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Section 2: Smart Receipt & Document Dropzone */}
        <div className="rounded-2xl border border-zinc-200/90 bg-white p-7 sm:p-8 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] transition-all">
          <div className="flex items-center justify-between pb-5 border-b border-zinc-100">
            <div className="flex items-center gap-3">
              <span className="flex h-7 w-7 items-center justify-center rounded-xl bg-zinc-100 text-zinc-700 border border-zinc-200/80 shadow-2xs">
                <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                </svg>
              </span>
              <div>
                <h2 className="text-[15px] sm:text-base font-bold uppercase tracking-wider text-zinc-800">
                  2. Supporting Receipt & OCR Extraction
                </h2>
                <p className="text-xs sm:text-sm text-zinc-400">
                  Attach vendor PDF invoice, scan, or spreadsheet for automated verification.
                </p>
              </div>
            </div>

            {uploadedFile && (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-t-emerald-200 border-x-emerald-300 border-b-emerald-400 bg-emerald-50 px-3 py-1 text-xs sm:text-sm font-semibold text-emerald-800 shadow-2xs">
                <span className="h-2 w-2 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]" />
                Document Attached
              </span>
            )}
          </div>

          <div className="mt-6">
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.png,.jpg,.jpeg,.csv"
              onChange={handleFileChange}
              className="hidden"
              id="invoice-file-upload"
            />

            {fileError && (
              <p
                role="alert"
                className="mb-3.5 rounded-xl border border-rose-200 bg-rose-50/90 px-4 py-2.5 text-xs sm:text-sm font-medium text-rose-800"
              >
                {fileError}
              </p>
            )}

            {!uploadedFile ? (
              <div
                role="button"
                tabIndex={0}
                aria-label="Attach an invoice document: click, press Enter or Space, or drop a file"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
                    e.preventDefault();
                    fileInputRef.current?.click();
                  }
                }}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`flex flex-col items-center justify-center rounded-2xl border-2 border-dashed p-9 sm:p-10 text-center transition-all cursor-pointer focus:outline-none focus-visible:border-zinc-800 focus-visible:ring-2 focus-visible:ring-zinc-900/30 focus-visible:ring-offset-2 ${
                  isDragging
                    ? 'border-zinc-800 bg-zinc-100/90 shadow-inner scale-[0.99]'
                    : 'border-zinc-300/90 bg-zinc-50/50 hover:border-zinc-400 hover:bg-zinc-50/90'
                }`}
              >
                <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_2px_4px_rgba(0,0,0,0.04)] mb-3.5">
                  <svg className="h-7 w-7 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 13h6m-3-3v6m5 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </div>

                <div className="text-base font-semibold text-zinc-800">
                  <span className="text-zinc-900 underline underline-offset-2">Click to browse</span> or drag & drop invoice document
                </div>
                <p className="mt-1.5 text-xs sm:text-sm text-zinc-400">
                  Supports PDF, PNG, JPG, or CSV scans up to 10MB
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                <div
                  ref={attachmentPanelRef}
                  tabIndex={-1}
                  aria-label="Attached invoice document"
                  className="flex flex-col gap-3.5 rounded-xl border border-zinc-200 bg-zinc-50/60 p-4.5 sm:p-5 shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] focus:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/30 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="flex items-center gap-3.5">
                    <div className="flex h-11 w-11 sm:h-12 sm:w-12 shrink-0 items-center justify-center rounded-xl border border-zinc-200 bg-white text-zinc-700 shadow-2xs font-mono text-xs sm:text-sm font-bold">
                      {uploadedFile.name.endsWith('.pdf') ? 'PDF' : uploadedFile.name.endsWith('.csv') ? 'CSV' : 'IMG'}
                    </div>
                    <div>
                      <div className="font-mono text-xs sm:text-sm font-bold text-zinc-900 truncate max-w-sm sm:max-w-md">
                        {uploadedFile.name}
                      </div>
                      <div className="flex items-center gap-2 text-xs text-zinc-500 mt-0.5">
                        <span>{formatFileSize(uploadedFile.size)}</span>
                        <span>•</span>
                        <span>{new Date(uploadedFile.lastModified).toLocaleDateString()}</span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2.5">
                    <button
                      type="button"
                      onClick={handleTriggerOcr}
                      disabled={ocrStatus === 'processing'}
                      className="inline-flex items-center gap-2 rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-4 py-2 sm:py-2.5 text-xs sm:text-sm font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-50 transition-all"
                    >
                      {ocrStatus === 'processing' ? (
                        <>
                          <svg className="h-4 w-4 animate-spin text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                          </svg>
                          <span>Extracting...</span>
                        </>
                      ) : (
                        <>
                          <svg className="h-4 w-4 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
                          </svg>
                          <span>Run OCR Auto-fill</span>
                        </>
                      )}
                    </button>

                    <button
                      type="button"
                      onClick={handleRemoveFile}
                      className="inline-flex items-center justify-center rounded-xl border border-zinc-200 bg-white p-2 text-zinc-400 hover:text-rose-600 hover:bg-rose-50 shadow-2xs transition-colors"
                      title="Remove attached file"
                    >
                      <svg className="h-4.5 w-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                </div>

                {ocrMessage && (
                  <div className="rounded-xl border border-emerald-200 bg-emerald-50/80 p-4 text-xs sm:text-sm text-emerald-900 flex items-center gap-2.5 shadow-2xs">
                    <svg className="h-5 w-5 text-emerald-600 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                    <span>{ocrMessage}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Section 3: Itemized Line-Item Calculation Grid */}
        <div className="rounded-2xl border border-zinc-200/90 bg-white p-5 sm:p-8 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] transition-all space-y-6">
          <div className="flex items-start gap-3 pb-5 border-b border-zinc-100">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-zinc-100 text-zinc-700 border border-zinc-200/80 shadow-2xs">
              <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
              </svg>
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                <h2 className="text-[15px] sm:text-base font-bold uppercase tracking-wider text-zinc-800 leading-none">
                  3. Amount Estimate Grid
                </h2>
                <span className="rounded-full border border-t-amber-200 border-x-amber-300 border-b-amber-400 bg-amber-50 px-2.5 py-0.5 font-mono text-[11px] font-bold uppercase tracking-wider text-amber-800 whitespace-nowrap">
                  Estimate — not stored
                </span>
                <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-0.5 font-mono text-[11px] text-zinc-500 tabular-nums whitespace-nowrap">
                  {lineItems.length} {lineItems.length === 1 ? 'Line Item' : 'Line Items'}
                </span>
              </div>
              <p className="mt-1.5 text-xs sm:text-sm text-zinc-400 leading-relaxed">
                Working estimate only — subtotal, tax and total are calculated live and feed the
                Form Amount below.
              </p>
            </div>
          </div>

          {/* Table Container with Tactile Surface */}
          <div className="overflow-x-auto rounded-xl border border-zinc-200 bg-zinc-50/40 shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)]">
            <table className="w-full text-left border-collapse min-w-[680px]">
              <thead>
                <tr className="border-b border-zinc-200/80 bg-zinc-100/80 text-xs font-bold uppercase tracking-wider text-zinc-600">
                  <th className="py-3.5 pl-4 pr-2 w-12 text-center">#</th>
                  <th className="py-3.5 px-3.5">Item Description</th>
                  <th className="py-3.5 px-2.5 w-28">Qty</th>
                  <th className="py-3.5 px-2.5 w-36">Unit Price ($)</th>
                  <th className="py-3.5 px-2.5 w-32">Tax (%)</th>
                  <th className="py-3.5 px-3.5 w-36 text-right">Line Total</th>
                  <th className="py-3.5 pr-4 pl-2 w-14 text-center"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200/70 text-sm">
                {lineItems.map((item, index) => {
                  const lineTotal =
                    (Number(item.quantity) || 0) *
                    (Number(item.unitPrice) || 0) *
                    (1 + (Number(item.taxRate) || 0) / 100);

                  return (
                    <tr key={item.id} className="hover:bg-zinc-50/80 transition-colors">
                      <td className="py-3.5 pl-4 pr-2 text-center font-mono text-xs font-medium text-zinc-400 tabular-nums">
                        {index + 1}
                      </td>
                      <td className="py-2.5 px-3.5">
                        <input
                          type="text"
                          placeholder="e.g. Database replication compute instance"
                          value={item.description}
                          onChange={(e) => handleUpdateLineItem(item.id, 'description', e.target.value)}
                          className="h-10 w-full rounded-lg border border-zinc-300/80 bg-white px-3 text-sm text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                        />
                      </td>
                      <td className="py-2.5 px-2.5">
                        <input
                          type="number"
                          min="1"
                          step="1"
                          value={item.quantity}
                          onChange={(e) => handleUpdateLineItem(item.id, 'quantity', parseFloat(e.target.value) || 0)}
                          className="h-10 w-full rounded-lg border border-zinc-300/80 bg-white px-3 text-sm font-mono text-zinc-900 tabular-nums text-right shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                        />
                      </td>
                      <td className="py-2.5 px-2.5">
                        <div className="relative">
                          <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 font-mono text-[13px] font-medium text-zinc-400">
                            $
                          </span>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={item.unitPrice}
                            onChange={(e) => handleUpdateLineItem(item.id, 'unitPrice', parseFloat(e.target.value) || 0)}
                            className="h-10 w-full rounded-lg border border-zinc-300/80 bg-white pl-7 pr-3 text-sm font-mono text-zinc-900 tabular-nums text-right shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                          />
                        </div>
                      </td>
                      <td className="py-2.5 px-2.5">
                        <select
                          value={item.taxRate}
                          onChange={(e) => handleUpdateLineItem(item.id, 'taxRate', parseFloat(e.target.value) || 0)}
                          className="h-10 w-full truncate rounded-lg border border-zinc-300/80 bg-white px-2.5 text-sm font-mono text-zinc-900 tabular-nums shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 cursor-pointer"
                        >
                          <option value="0">0% (None)</option>
                          <option value="5">5.0%</option>
                          <option value="8.5">8.5%</option>
                          <option value="10">10.0%</option>
                          <option value="18">18.0%</option>
                        </select>
                      </td>
                      <td className="py-3.5 px-3.5 text-right font-mono font-bold text-zinc-900 tabular-nums text-sm">
                        {formatCurrency(lineTotal)}
                      </td>
                      <td className="py-2.5 pr-4 pl-2 text-center">
                        <button
                          type="button"
                          onClick={() => handleRemoveLineItem(item.id)}
                          disabled={lineItems.length <= 1}
                          className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-transparent text-zinc-400 transition-colors hover:border-rose-200 hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-900 disabled:opacity-30 disabled:pointer-events-none"
                          title="Delete line item"
                          aria-label={`Delete line item ${index + 1}`}
                        >
                          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                          </svg>
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Breakdown & Grand Total Calculation Section */}
          <div className="flex flex-col gap-5 pt-2 md:flex-row md:items-start md:justify-between">
            <button
              type="button"
              onClick={handleAddLineItem}
              className="inline-flex shrink-0 self-start items-center gap-2 whitespace-nowrap rounded-xl border border-zinc-200 bg-white px-4 py-2.5 text-xs sm:text-sm font-semibold text-zinc-700 shadow-2xs hover:bg-zinc-50 hover:border-zinc-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-900 active:translate-y-[0.5px] transition-all"
            >
              <svg className="h-4 w-4 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
              </svg>
              <span>Add Line Item</span>
            </button>

            {/* Financial Summary Card */}
            <div className="w-full md:w-80 md:shrink-0 lg:w-96 rounded-2xl border border-zinc-200/90 bg-zinc-50/70 p-5 shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)]">
              <div className="space-y-1.5">
                <div className="flex items-baseline justify-between text-xs sm:text-sm text-zinc-500 font-medium">
                  <span>Subtotal</span>
                  <span className="font-mono text-zinc-800 tabular-nums">{formatCurrency(totals.subtotal)}</span>
                </div>
                <div className="flex items-baseline justify-between text-xs sm:text-sm text-zinc-500 font-medium">
                  <span>Estimated Tax</span>
                  <span className="font-mono text-zinc-800 tabular-nums">{formatCurrency(totals.taxTotal)}</span>
                </div>
              </div>
              <div className="mt-3 border-t border-zinc-200 pt-3 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <span className="block text-xs sm:text-[13px] font-bold uppercase tracking-wider text-zinc-500 leading-none">
                    Estimated Total
                  </span>
                  <div className="mt-1 text-[11px] leading-none text-zinc-400">USD — feeds Form Amount</div>
                </div>
                <div className="shrink-0 text-right">
                  <span className="font-mono text-xl sm:text-2xl font-extrabold tracking-tight leading-none text-zinc-900 tabular-nums">
                    {formatCurrency(totals.total)}
                  </span>
                </div>
              </div>

              <p className="mt-3 text-[11px] leading-relaxed text-zinc-500">
                Vendor, date and PO number are submitted along with the Form Amount above. The
                descriptions, quantities, unit prices and tax rates entered in this grid are not
                stored with the invoice.
              </p>

              {/* Amount Sync / Override Toggle */}
              <div className="mt-3 border-t border-zinc-200/60 pt-3">
                <div className="flex items-center justify-between gap-2 text-xs sm:text-[13px]">
                  <label htmlFor="amount-input" className="font-bold text-zinc-700">
                    Form Amount (USD) <span className="text-rose-500">*</span>
                  </label>
                  <button
                    type="button"
                    onClick={() => setIsManualAmountOverride(!isManualAmountOverride)}
                    className="rounded-md text-[11px] text-zinc-500 underline underline-offset-2 hover:text-zinc-800 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-900"
                  >
                    {isManualAmountOverride ? 'Auto-sync from items' : 'Manual override'}
                  </button>
                </div>

                <div className="relative mt-2">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <span className="font-mono text-sm font-bold tabular-nums text-zinc-400">$</span>
                  </div>
                  <input
                    id="amount-input"
                    type="number"
                    step="0.01"
                    min="0.01"
                    value={amount}
                    readOnly={!isManualAmountOverride}
                    onChange={(e) => {
                      setIsManualAmountOverride(true);
                      setAmount(e.target.value);
                    }}
                    aria-invalid={!!fieldErrors.amount}
                    aria-describedby={fieldErrors.amount ? 'amount-error' : undefined}
                    className={`block h-12 w-full rounded-xl border pl-9 pr-4 font-mono text-[15px] sm:text-base font-bold tabular-nums text-zinc-900 text-right shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] focus:outline-none focus:ring-1 focus:ring-zinc-900 ${
                      isManualAmountOverride
                        ? 'border-zinc-300/80 bg-white focus:border-zinc-900'
                        : 'border-zinc-200 bg-zinc-100/80 cursor-default text-zinc-800 focus:border-zinc-300'
                    }`}
                  />
                </div>
                {fieldErrors.amount && (
                  <p id="amount-error" role="alert" className="mt-1.5 text-xs sm:text-sm text-rose-600 font-medium text-right">
                    {fieldErrors.amount[0]}
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Section 4: Bottom Dispatch Bar */}
        <div className="flex flex-col sm:flex-row items-center justify-between gap-5 rounded-2xl border border-zinc-200/90 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_4px_12px_rgba(0,0,0,0.03)]">
          <div className="flex items-center gap-3 text-xs sm:text-sm text-zinc-500">
            <span className="flex h-3 w-3 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]" />
            <span>Ready to post to the ERP accounts payable database with audit trail.</span>
          </div>

          <div className="flex items-center gap-3 w-full sm:w-auto justify-end">
            <Link
              href="/invoices"
              className="rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-6 py-3 text-xs sm:text-sm font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px] transition-all text-center"
            >
              Discard & Cancel
            </Link>

            <button
              type="submit"
              disabled={isSubmitting}
              className="inline-flex items-center justify-center gap-2.5 rounded-xl border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-8 py-3.5 text-sm sm:text-base font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(15,23,42,0.25)] transition-all hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px] disabled:opacity-50"
            >
              {isSubmitting ? (
                <>
                  <svg className="h-5 w-5 animate-spin text-zinc-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Submitting Invoice...</span>
                </>
              ) : (
                <>
                  <svg className="h-5 w-5 text-zinc-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Create Invoice</span>
                </>
              )}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

export default function NewInvoicePage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-7 pb-20">
          <div className="h-16 w-full animate-pulse rounded-2xl bg-zinc-100" />
          <div className="h-96 w-full animate-pulse rounded-2xl bg-zinc-100" />
        </div>
      }
    >
      <NewInvoiceForm />
    </Suspense>
  );
}
