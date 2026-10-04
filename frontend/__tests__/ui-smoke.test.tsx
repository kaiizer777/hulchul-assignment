import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import React from 'react';

import InvoicesPage from '@/app/invoices/page';
import NewInvoicePage from '@/app/invoices/new/page';
import PurchaseOrdersPage from '@/app/purchase-orders/page';
import VendorsPage from '@/app/vendors/page';

describe('UI Smoke Tests', () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    // Default mock fetch for all UI pages
    global.fetch = vi.fn().mockImplementation((url: string | URL | Request) => {
      const urlStr = typeof url === 'string' ? url : url.toString();

      if (urlStr.includes('/api/invoices')) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: 'inv-1',
                vendor: 'Acme Corp',
                amount: 1500,
                date: '2026-10-01',
                po_number: 'PO-1001',
                status: 'pending',
                created_at: '2026-10-01T10:00:00.000Z',
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        );
      }

      if (urlStr.includes('/api/purchase-orders')) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                po_number: 'PO-1001',
                vendor: 'Acme Corp',
                approved_amount: 5000,
                status: 'approved',
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        );
      }

      if (urlStr.includes('/api/vendors')) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              { id: 'acme-corp', name: 'Acme Corp' },
              { id: 'bharat-supplies', name: 'Bharat Supplies' },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        );
      }

      return Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders /invoices page component without throwing or console errors', async () => {
    let renderResult: ReturnType<typeof render> | undefined;

    await act(async () => {
      renderResult = render(<InvoicesPage />);
    });

    expect(renderResult?.container).toBeDefined();
    expect(screen.getByRole('heading', { level: 1, name: /invoices/i })).toBeDefined();

    // Wait for the invoice data row to load
    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
    });

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('renders /invoices/new page component without throwing or console errors', async () => {
    let renderResult: ReturnType<typeof render> | undefined;

    await act(async () => {
      renderResult = render(<NewInvoicePage />);
    });

    expect(renderResult?.container).toBeDefined();
    expect(screen.getByRole('heading', { level: 1 })).toBeDefined();

    // Verify vendor option is loaded into select
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /Acme Corp/i })).toBeDefined();
    });

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('renders /purchase-orders page component without throwing or console errors', async () => {
    let renderResult: ReturnType<typeof render> | undefined;

    await act(async () => {
      renderResult = render(<PurchaseOrdersPage />);
    });

    expect(renderResult?.container).toBeDefined();
    expect(screen.getByRole('heading', { level: 1, name: /purchase orders/i })).toBeDefined();

    // Wait for PO data row to load
    await waitFor(() => {
      expect(screen.getByText('PO-1001')).toBeDefined();
    });

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('renders /vendors page component without throwing or console errors', async () => {
    let renderResult: ReturnType<typeof render> | undefined;

    await act(async () => {
      renderResult = render(<VendorsPage />);
    });

    expect(renderResult?.container).toBeDefined();
    expect(screen.getByRole('heading', { level: 1, name: /vendors/i })).toBeDefined();

    // Wait for vendors list to render
    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
      expect(screen.getByText('Bharat Supplies')).toBeDefined();
    });

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });
});
