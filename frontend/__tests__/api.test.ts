import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { InvoiceDTO, PurchaseOrderDTO, VendorDTO } from '@/lib/types';

// Mock DB client
const mockSql = vi.fn();
vi.mock('@/lib/db', () => ({
  getDb: () => mockSql,
}));

// Every handler is session-guarded inside the handler itself. These tests cover
// the query and mapping behaviour behind the guard, so a valid session is
// stubbed; the 401 path is covered in agent-api.test.ts.
vi.mock('@/lib/auth', () => ({
  unauthorizedIfNoSession: async () => null,
}));

const request = (path: string, init?: RequestInit) =>
  new Request(`http://localhost:3051${path}`, init);

describe('API Route Unit Tests', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('GET /api/invoices', () => {
    it('returns status 200 and array of invoices matching InvoiceDTO schema', async () => {
      const mockRows = [
        {
          id: '1',
          vendor: 'Acme Corp',
          amount: '1250.50',
          date: '2026-10-01',
          po_number: 'PO-1001',
          status: 'pending',
          created_at: '2026-10-01T12:00:00.000Z',
        },
        {
          id: '2',
          vendor: 'Bharat Supplies',
          amount: 50000,
          date: '2026-10-02',
          po_number: null,
          status: 'approved',
          created_at: new Date('2026-10-02T08:30:00.000Z'),
        },
      ];

      mockSql.mockResolvedValueOnce(mockRows);

      const { GET } = await import('@/app/api/invoices/route');
      const response = await GET(request('/api/invoices'));

      expect(response.status).toBe(200);

      const data: InvoiceDTO[] = await response.json();
      expect(Array.isArray(data)).toBe(true);
      expect(data).toHaveLength(2);

      // Verify InvoiceDTO shape
      data.forEach((inv) => {
        expect(typeof inv.id).toBe('string');
        expect(typeof inv.vendor).toBe('string');
        expect(typeof inv.amount).toBe('number');
        expect(typeof inv.date).toBe('string');
        expect(inv.po_number === null || typeof inv.po_number === 'string').toBe(true);
        expect(['pending', 'approved', 'flagged', 'completed', 'rejected', 'skipped']).toContain(inv.status);
        expect(typeof inv.created_at).toBe('string');
      });

      expect(data[0]).toEqual({
        id: '1',
        vendor: 'Acme Corp',
        amount: 1250.5,
        date: '2026-10-01',
        po_number: 'PO-1001',
        status: 'pending',
        created_at: '2026-10-01T12:00:00.000Z',
      });
    });
  });

  describe('POST /api/invoices', () => {
    it('returns status 201 and created invoice object with expected shape', async () => {
      const payload = {
        vendor: 'Nova Tech',
        amount: 3450.75,
        date: '2026-10-03',
        po_number: 'PO-2002',
        status: 'pending',
      };

      const createdRow = {
        id: '15',
        vendor: 'Nova Tech',
        amount: 3450.75,
        date: '2026-10-03',
        po_number: 'PO-2002',
        status: 'pending',
        created_at: '2026-10-03T10:00:00.000Z',
      };

      mockSql.mockResolvedValueOnce([createdRow]);

      const { POST } = await import('@/app/api/invoices/route');
      const request = new Request('http://localhost:3051/api/invoices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const response = await POST(request);
      expect(response.status).toBe(201);

      const data: InvoiceDTO = await response.json();
      expect(data).toBeDefined();
      expect(data.id).toBe('15');
      expect(data.vendor).toBe(payload.vendor);
      expect(data.amount).toBe(payload.amount);
      expect(data.date).toBe(payload.date);
      expect(data.po_number).toBe(payload.po_number);
      expect(data.status).toBe('pending');
      expect(data.created_at).toBe('2026-10-03T10:00:00.000Z');
    });

    it('returns status 400 for invalid payload', async () => {
      const { POST } = await import('@/app/api/invoices/route');
      const request = new Request('http://localhost:3051/api/invoices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vendor: '', amount: -50, date: 'invalid-date' }),
      });

      const response = await POST(request);
      expect(response.status).toBe(400);
      const data = await response.json();
      expect(data.error).toBe('Validation failed');
      expect(data.details).toBeDefined();
    });
  });

  describe('POST /api/invoices failure injection gate', () => {
    const payload = {
      vendor: 'Acme Corp',
      amount: 1,
      date: '2026-01-01',
    };

    const createdRow = {
      id: '99',
      vendor: 'Acme Corp',
      amount: 1,
      date: '2026-01-01',
      po_number: null,
      status: 'pending',
      created_at: '2026-01-01T00:00:00.000Z',
    };

    const postInvoice = async (query: string, headers?: Record<string, string>) => {
      const { POST } = await import('@/app/api/invoices/route');
      return POST(
        new Request(`http://localhost:3051/api/invoices${query}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...headers },
          body: JSON.stringify(payload),
        })
      );
    };

    const resetCounter = async () => {
      vi.stubEnv('NODE_ENV', 'test');
      mockSql.mockResolvedValueOnce([createdRow]);
      await postInvoice('?reset_failure=true');
    };

    beforeEach(async () => {
      vi.stubEnv('SIMULATE_FAILURE_AFTER', '');
      await resetCounter();
    });

    afterEach(() => {
      vi.unstubAllEnvs();
    });

    it('ignores x-test-failure-injection in production', async () => {
      vi.stubEnv('NODE_ENV', 'production');
      vi.stubEnv('ENABLE_TEST_FAILURE_INJECTION', '');
      mockSql.mockResolvedValueOnce([createdRow]);

      const response = await postInvoice('?fail_after=1', {
        'x-test-failure-injection': 'true',
      });

      expect(response.status).toBe(201);
      expect(mockSql).toHaveBeenCalledTimes(2);
      expect(await response.json()).toEqual({
        id: '99',
        vendor: 'Acme Corp',
        amount: 1,
        date: '2026-01-01',
        po_number: null,
        status: 'pending',
        created_at: '2026-01-01T00:00:00.000Z',
      });
    });

    it('ignores x-test-mode in production', async () => {
      vi.stubEnv('NODE_ENV', 'production');
      vi.stubEnv('ENABLE_TEST_FAILURE_INJECTION', '');
      mockSql.mockResolvedValueOnce([createdRow]);

      const response = await postInvoice('?fail_after=1', {
        'x-test-mode': 'true',
      });

      expect(response.status).toBe(201);
      expect(mockSql).toHaveBeenCalledTimes(2);
      expect(await response.json()).toEqual({
        id: '99',
        vendor: 'Acme Corp',
        amount: 1,
        date: '2026-01-01',
        po_number: null,
        status: 'pending',
        created_at: '2026-01-01T00:00:00.000Z',
      });
    });

    it('allows injection in production when ENABLE_TEST_FAILURE_INJECTION is set', async () => {
      vi.stubEnv('NODE_ENV', 'production');
      vi.stubEnv('ENABLE_TEST_FAILURE_INJECTION', 'true');

      const response = await postInvoice('?fail_after=1', {
        'x-test-failure-injection': 'true',
      });

      expect(response.status).toBe(500);
      expect(await response.json()).toEqual({
        error: 'Simulated ERP internal server error',
      });
      expect(mockSql).toHaveBeenCalledTimes(1);
    });

    it('honours fail_after with the header outside production', async () => {
      vi.stubEnv('NODE_ENV', 'test');
      vi.stubEnv('ENABLE_TEST_FAILURE_INJECTION', '');

      const response = await postInvoice('?fail_after=1', {
        'x-test-failure-injection': 'true',
      });

      expect(response.status).toBe(500);
      expect(await response.json()).toEqual({
        error: 'Simulated ERP internal server error',
      });
      expect(mockSql).toHaveBeenCalledTimes(1);
    });
  });

  describe('GET /api/purchase-orders', () => {
    it('returns status 200 and array of POs matching PurchaseOrderDTO schema', async () => {
      const mockPoRows = [
        {
          po_number: 'PO-1001',
          vendor: 'Acme Corp',
          approved_amount: '1250.50',
          status: 'approved',
        },
        {
          po_number: 'PO-1002',
          vendor: 'Bharat Supplies',
          approved_amount: 50000,
          status: 'pending',
        },
      ];

      mockSql.mockResolvedValueOnce(mockPoRows);

      const { GET } = await import('@/app/api/purchase-orders/route');
      const response = await GET(request('/api/purchase-orders'));

      expect(response.status).toBe(200);

      const data: PurchaseOrderDTO[] = await response.json();
      expect(Array.isArray(data)).toBe(true);
      expect(data).toHaveLength(2);

      data.forEach((po) => {
        expect(typeof po.po_number).toBe('string');
        expect(typeof po.vendor).toBe('string');
        expect(typeof po.approved_amount).toBe('number');
        expect(typeof po.status).toBe('string');
      });

      expect(data[0]).toEqual({
        po_number: 'PO-1001',
        vendor: 'Acme Corp',
        approved_amount: 1250.5,
        status: 'approved',
      });
    });
  });

  describe('GET /api/vendors', () => {
    it('returns status 200 and array of vendors with { id, name }', async () => {
      const mockVendorRows = [
        { vendor: 'Acme Corp' },
        { vendor: 'Delta Goods' },
        { vendor: 'Custom Supplier Inc' },
      ];

      mockSql.mockResolvedValueOnce(mockVendorRows);

      const { GET } = await import('@/app/api/vendors/route');
      const response = await GET(request('/api/vendors'));

      expect(response.status).toBe(200);

      const data: VendorDTO[] = await response.json();
      expect(Array.isArray(data)).toBe(true);
      expect(data.length).toBeGreaterThan(0);

      data.forEach((v) => {
        expect(typeof v.id).toBe('string');
        expect(typeof v.name).toBe('string');
        expect(v.id.length).toBeGreaterThan(0);
        expect(v.name.length).toBeGreaterThan(0);
      });

      // Verify custom vendor was mapped correctly
      const customVendor = data.find((v) => v.name === 'Custom Supplier Inc');
      expect(customVendor).toBeDefined();
      expect(customVendor?.id).toBe('custom-supplier-inc');
    });

    it('returns status 500 with error body when the database fails', async () => {
      const dbModule = await import('@/lib/db');
      vi.spyOn(dbModule, 'getDb').mockImplementationOnce(() => {
        throw new Error('DATABASE_URL environment variable is missing.');
      });

      const { GET } = await import('@/app/api/vendors/route');
      const response = await GET(request('/api/vendors'));

      expect(response.status).toBe(500);

      const data = await response.json();
      expect(Array.isArray(data)).toBe(false);
      expect(data.error).toBeDefined();
    });
  });
});
