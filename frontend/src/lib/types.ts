import { z } from 'zod';

export const InvoiceStatusEnum = z.enum([
  'pending',
  'approved',
  'flagged',
  'completed',
  'rejected',
  'skipped',
]);
export type InvoiceStatus = z.infer<typeof InvoiceStatusEnum>;

export interface InvoiceDTO {
  id: string;
  vendor: string;
  amount: number;
  date: string;
  po_number: string | null;
  status: InvoiceStatus;
  created_at: string;
}

export const CreateInvoiceSchema = z
  .object({
    vendor: z.string().trim().min(1, 'Vendor name is required'),
    amount: z.coerce.number().positive('Amount must be greater than 0'),
    date: z.string().trim().regex(/^\d{4}-\d{2}-\d{2}$/, 'Date must be formatted as YYYY-MM-DD'),
    po_number: z.string().trim().nullable().optional().transform((val) => (val === '' ? null : val ?? null)),
    status: InvoiceStatusEnum.optional().default('pending'),
  })
  .strict();

export type CreateInvoiceInput = z.infer<typeof CreateInvoiceSchema>;

export const UpdateInvoiceStatusSchema = z
  .object({
    status: InvoiceStatusEnum,
  })
  .strict();

export type UpdateInvoiceStatusInput = z.infer<typeof UpdateInvoiceStatusSchema>;

export interface PurchaseOrderDTO {
  po_number: string;
  vendor: string;
  approved_amount: number;
  status: string;
}

export interface VendorDTO {
  id: string;
  name: string;
}

export interface RawInvoiceRow {
  id: string;
  vendor: string;
  amount: string | number;
  date: string | Date;
  po_number: string | null;
  status: string;
  created_at: string | Date;
}

export function formatInvoice(row: RawInvoiceRow): InvoiceDTO {
  let dateStr: string;
  if (typeof row.date === 'string') {
    dateStr = row.date.slice(0, 10);
  } else if (row.date instanceof Date) {
    dateStr = row.date.toISOString().slice(0, 10);
  } else {
    dateStr = String(row.date);
  }

  let createdAtStr: string;
  if (row.created_at instanceof Date) {
    createdAtStr = row.created_at.toISOString();
  } else {
    createdAtStr = String(row.created_at);
  }

  return {
    id: row.id,
    vendor: row.vendor,
    amount: Number(row.amount),
    date: dateStr,
    po_number: row.po_number || null,
    status: (InvoiceStatusEnum.safeParse(row.status).success ? row.status : 'pending') as InvoiceStatus,
    created_at: createdAtStr,
  };
}
