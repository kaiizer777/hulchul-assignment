import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import InvoicesPage from '@/app/invoices/page';

const MOCK_INVOICES = [
  {
    id: 'inv-101',
    vendor: 'Acme Corp',
    amount: 1500.5,
    date: '2026-10-01',
    po_number: 'PO-1001',
    status: 'pending',
    created_at: '2026-10-01T10:00:00.000Z',
  },
  {
    id: 'inv-102',
    vendor: 'Bharat Supplies',
    amount: 3200,
    date: '2026-10-02',
    po_number: 'PO-1002',
    status: 'completed',
    created_at: '2026-10-02T11:00:00.000Z',
  },
  {
    id: 'inv-103',
    vendor: 'Cyberdyne Systems',
    amount: 8900,
    date: '2026-10-03',
    po_number: null,
    status: 'flagged',
    created_at: '2026-10-03T12:00:00.000Z',
  },
  {
    id: 'inv-104',
    vendor: 'Delta Logistics',
    amount: 450,
    date: '2026-10-04',
    po_number: 'PO-1004',
    status: 'failed',
    created_at: '2026-10-04T13:00:00.000Z',
  },
];

describe('InvoicesPage Component', () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockImplementation((url: string | URL | Request) => {
      const urlStr = typeof url === 'string' ? url : url.toString();
      if (urlStr.includes('/api/invoices')) {
        return Promise.resolve(
          new Response(JSON.stringify(MOCK_INVOICES), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        );
      }
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders invoices list and metric cards correctly', async () => {
    render(<InvoicesPage />);

    expect(screen.getByRole('heading', { level: 1, name: /invoices/i })).toBeDefined();

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
      expect(screen.getByText('Bharat Supplies')).toBeDefined();
      expect(screen.getByText('Cyberdyne Systems')).toBeDefined();
      expect(screen.getByText('Delta Logistics')).toBeDefined();
    });

    // Check summary metrics
    expect(screen.getByText('Total Invoices')).toBeDefined();
    expect(screen.getByText('Total Ledger Value')).toBeDefined();
    expect(screen.getByText('Pending Review')).toBeDefined();
  });

  it('filters invoices by search query', async () => {
    render(<InvoicesPage />);

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
    });

    const searchInput = screen.getByPlaceholderText(/search by vendor/i);
    fireEvent.change(searchInput, { target: { value: 'Bharat' } });

    await waitFor(() => {
      expect(screen.getByText('Bharat Supplies')).toBeDefined();
      expect(screen.queryByText('Acme Corp')).toBeNull();
      expect(screen.queryByText('Cyberdyne Systems')).toBeNull();
    });

    // Clear search
    const clearBtn = screen.getByTitle('Clear search');
    fireEvent.click(clearBtn);

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
    });
  });

  it('filters invoices by status filter pills', async () => {
    render(<InvoicesPage />);

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
    });

    // Click Pending filter pill
    const pendingPill = screen.getByRole('button', { name: /^Pending/i });
    fireEvent.click(pendingPill);

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
      expect(screen.queryByText('Bharat Supplies')).toBeNull(); // Completed
      expect(screen.queryByText('Cyberdyne Systems')).toBeNull(); // Flagged
    });
  });

  it('opens slide-over inspection modal on row click and closes on Escape or Done', async () => {
    render(<InvoicesPage />);

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeDefined();
    });

    // Click row or Inspect button
    const inspectButtons = screen.getAllByRole('button', { name: /inspect/i });
    fireEvent.click(inspectButtons[0]);

    // Modal opens
    await waitFor(() => {
      expect(screen.getByText('Invoice Specification')).toBeDefined();
      expect(screen.getByText('Net Settlement Payable')).toBeDefined();
    });

    // Close modal via Done button
    const doneBtn = screen.getByRole('button', { name: /done/i });
    fireEvent.click(doneBtn);

    await waitFor(() => {
      expect(screen.queryByText('Invoice Specification')).toBeNull();
    });
  });

  it('handles empty state when 0 invoices return', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    render(<InvoicesPage />);

    await waitFor(() => {
      expect(screen.getByText(/no invoices recorded yet/i)).toBeDefined();
      expect(screen.getByRole('link', { name: /add your first invoice/i })).toBeDefined();
    });
  });
});
