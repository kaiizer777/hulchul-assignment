import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import React from 'react';

import NewInvoicePage from '@/app/invoices/new/page';

/**
 * Regression coverage for the /invoices/new form.
 *
 * These assertions are deliberately behaviour-level rather than DOM-snapshot
 * level. The existing ui-smoke test asserts `getByText('Acme Corp')` on this
 * page, but the vendor options render as "Acme Corp (acme-corp)", so that
 * assertion is satisfied only by markup left behind by the previous test and
 * cannot detect a regression here. Everything below targets state that is
 * actually observable on this page.
 */

const VENDORS = [
  { id: 'acme-corp', name: 'Acme Corp' },
  { id: 'bharat-supplies', name: 'Bharat Supplies' },
];

const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;

function renderPage() {
  return render(<NewInvoicePage />);
}

async function waitForVendors() {
  await waitFor(() => {
    expect(document.querySelectorAll('#vendor-input option').length).toBeGreaterThan(1);
  });
}

function makePdf(name: string, sizeBytes: number) {
  return new File([new Uint8Array(sizeBytes)], name, { type: 'application/pdf' });
}

/**
 * Resolves the dropzone without depending on its accessibility wiring, so the
 * attachment-validation cases fail on the missing validation rather than on the
 * dropzone's role. The accessibility case below queries by role on purpose.
 */
function getDropzone(): HTMLElement {
  const byRole = screen.queryByRole('button', { name: /Attach an invoice document/i });
  if (byRole) return byRole;
  return screen.getByText(/drag & drop invoice document/i).parentElement as HTMLElement;
}

describe('New invoice form', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockImplementation((url: string | URL | Request) => {
      const urlStr = typeof url === 'string' ? url : url.toString();

      if (urlStr.includes('/api/vendors')) {
        return Promise.resolve(
          new Response(JSON.stringify(VENDORS), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        );
      }

      if (urlStr.includes('/api/purchase-orders')) {
        return Promise.resolve(
          new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        );
      }

      return Promise.resolve(
        new Response(JSON.stringify({ id: 'inv-1' }), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        })
      );
    });

    global.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('fresh form carries no fabricated invoice data', () => {
    it('opens with a single blank estimate row and no preset amount', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      const descriptions = screen.getByPlaceholderText(
        'e.g. Database replication compute instance'
      ) as HTMLInputElement;
      expect(descriptions.value).toBe('');

      // One editable row, and no pre-seeded second row.
      expect(screen.getAllByPlaceholderText('e.g. Database replication compute instance')).toHaveLength(1);
    });

    it('refuses to submit the zero estimate the blank grid produces', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      const amountInput = document.querySelector('#amount-input') as HTMLInputElement;
      expect(Number(amountInput.value)).toBe(0);

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Create Invoice' }));
      });

      expect(await screen.findByText(/Amount must be a positive number/i)).toBeDefined();

      const posted = fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/invoices'));
      expect(posted).toHaveLength(0);
    });
  });

  describe('attachment validation', () => {
    async function dropFile(file: File) {
      const dropzone = getDropzone();
      await act(async () => {
        fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });
      });
    }

    it('rejects a dropped file whose type is not supported', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      await dropFile(new File(['<script>'], 'payload.exe', { type: 'application/octet-stream' }));

      expect(await screen.findByRole('alert')).toBeDefined();
      expect(screen.getByRole('alert').textContent).toContain('payload.exe');
      expect(screen.queryByText('Document Attached')).toBeNull();
    });

    it('rejects a dropped file over the advertised 10 MB limit', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      await dropFile(makePdf('huge.pdf', MAX_FILE_SIZE_BYTES + 1));

      expect(await screen.findByRole('alert')).toBeDefined();
      expect(screen.getByRole('alert').textContent).toContain('huge.pdf');
      expect(screen.queryByText('Document Attached')).toBeNull();
    });

    it('accepts a supported file within the limit', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      await dropFile(makePdf('receipt.pdf', 1024));

      expect(screen.getByText('Document Attached')).toBeDefined();
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });

  describe('dropzone accessibility', () => {
    it('is reachable and activatable from the keyboard', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      const dropzone = screen.getByRole('button', { name: /Attach an invoice document/i });
      expect(dropzone.getAttribute('tabindex')).toBe('0');

      const fileInput = document.querySelector('#invoice-file-upload') as HTMLInputElement;
      const clickSpy = vi.spyOn(fileInput, 'click');

      await act(async () => {
        fireEvent.keyDown(dropzone, { key: 'Enter' });
      });
      expect(clickSpy).toHaveBeenCalledTimes(1);

      await act(async () => {
        fireEvent.keyDown(dropzone, { key: ' ' });
      });
      expect(clickSpy).toHaveBeenCalledTimes(2);
    });

    it('keeps focus on the attachment panel once the dropzone is replaced', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      await act(async () => {
        fireEvent.drop(getDropzone(), { dataTransfer: { files: [makePdf('receipt.pdf', 1024)] } });
      });

      const panel = screen.getByLabelText('Attached invoice document');
      expect(document.activeElement).toBe(panel);
    });
  });

  describe('OCR auto-fill', () => {
    it('does not let a pending run auto-fill after the document is removed', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      const vendorSelect = document.querySelector('#vendor-input') as HTMLSelectElement;
      await act(async () => {
        fireEvent.change(vendorSelect, { target: { value: '' } });
      });

      const dropzone = getDropzone();
      await act(async () => {
        fireEvent.drop(dropzone, { dataTransfer: { files: [makePdf('receipt.pdf', 1024)] } });
      });

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /Run OCR Auto-fill/i }));
      });

      // Detach before the 900ms callback is due: a callback that outlives the
      // document would still auto-fill the vendor from stale state.
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Remove attached file' }));
      });

      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 1400));
      });

      expect(screen.queryByText('Document Attached')).toBeNull();
      expect(vendorSelect.value).toBe('');
    }, 10000);

    it('leaves a vendor the user picked before the callback fired', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      const vendorSelect = document.querySelector('#vendor-input') as HTMLSelectElement;
      await act(async () => {
        fireEvent.change(vendorSelect, { target: { value: '' } });
      });

      const dropzone = getDropzone();
      await act(async () => {
        fireEvent.drop(dropzone, { dataTransfer: { files: [makePdf('receipt.pdf', 1024)] } });
      });

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /Run OCR Auto-fill/i }));
      });

      // The user chooses a vendor while OCR is in flight.
      await act(async () => {
        fireEvent.change(vendorSelect, { target: { value: 'Bharat Supplies' } });
      });

      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 1400));
      });

      expect((document.querySelector('#vendor-input') as HTMLSelectElement).value).toBe(
        'Bharat Supplies'
      );
    }, 10000);
  });

  describe('fields the API cannot store', () => {
    it('presents invoice reference and payment terms as not persisted', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      expect(screen.getByText(/Invoice Reference #/i)).toBeDefined();
      expect(screen.getByText(/Payment Terms/i)).toBeDefined();

      // No editable control remains for either value.
      expect(document.querySelector('#ref-input')).toBeNull();
      expect(document.querySelector('#terms-select')).toBeNull();
      expect(screen.getAllByText('Not persisted')).toHaveLength(2);
    });
  });

  describe('estimate grid labelling', () => {
    it('states that line items are an estimate and not stored', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      expect(screen.getByText('Estimate — not stored')).toBeDefined();
      expect(
        screen.getByText(/are not\s+stored with the\s+invoice/i)
      ).toBeDefined();
    });

    it('names every field that is submitted alongside the amount', async () => {
      await act(async () => {
        renderPage();
      });
      await waitForVendors();

      expect(
        screen.getByText(/Vendor, date and PO number are submitted/i)
      ).toBeDefined();
    });
  });
});